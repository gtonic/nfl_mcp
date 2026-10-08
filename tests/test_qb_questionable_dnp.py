"""A Questionable starting QB who has not practised all week (Lamar Jackson,
week 5 2026: DNP Wed + Thu) cuts his receivers like a Doubtful one."""
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import news_signals, opportunity_tools, practice_reports, qb_coupling, ros
from nfl_mcp import sleeper_projections as sp
from tests.test_qb_coupling_news_signals import (
    _VALUES,
    _depth,
    _engine_with,
    _sleeper,
    _tb_logs,
)


def _practice(*statuses, start=date(2026, 10, 7)):
    """A `practice_reports.summarize` line from Wednesday on."""
    from datetime import timedelta
    rows = [{"date": (start + timedelta(days=i)).isoformat(), "status": s, "source": "nfl.com"}
            for i, s in enumerate(statuses)]
    return practice_reports.summarize(rows)


def _flag(name, weight=1.0):
    return {"flag": name, "weight": weight, "snippet": name}


class TestStarterSitWeight:
    def test_questionable_dnp_dnp_is_the_dnp_weight(self):
        sit = qb_coupling.starter_sit_weight("Questionable", _practice("DNP", "DNP"))
        assert sit["weight"] == qb_coupling.QUESTIONABLE_DNP_WEIGHT < qb_coupling.DOUBTFUL_WEIGHT
        assert sit["basis"] == "practice" and sit["detail"] == "DNP Wed/Thu"

    @pytest.mark.parametrize("statuses, weight", [
        (("LP",), "QUESTIONABLE_LIMITED_WEIGHT"), (("DNP", "LP"), "QUESTIONABLE_LIMITED_WEIGHT"),
        (("DNP", "FP"), "QUESTIONABLE_FULL_WEIGHT"), (("FP", "FP"), "QUESTIONABLE_FULL_WEIGHT"),
        (("REST",), "QUESTIONABLE_FULL_WEIGHT"),
        (("DNP",), "QUESTIONABLE_WEIGHT"), (("LP", "DNP"), "QUESTIONABLE_WEIGHT"),
        ((), "QUESTIONABLE_WEIGHT")])
    def test_questionable_otherwise(self, statuses, weight):
        # 2023-25: 42% of questionable starters sat, 47% with a limited
        # latest day, none of six with a full one (practice_backtest).
        sit = qb_coupling.starter_sit_weight("Questionable", _practice(*statuses))
        assert sit["weight"] == getattr(qb_coupling, weight)

    def test_calibrated_weights(self):
        assert qb_coupling.QUESTIONABLE_FULL_WEIGHT == 0.0
        assert 0 < qb_coupling.QUESTIONABLE_WEIGHT <= qb_coupling.QUESTIONABLE_LIMITED_WEIGHT \
            < qb_coupling.QUESTIONABLE_DNP_WEIGHT <= qb_coupling.DOUBTFUL_WEIGHT < 1.0

    def test_dnp_on_the_latest_day_after_an_earlier_dnp(self):
        sit = qb_coupling.starter_sit_weight("Questionable", _practice("DNP", "LP", "DNP"))
        assert sit["weight"] == qb_coupling.QUESTIONABLE_DNP_WEIGHT
        assert sit["detail"] == "DNP Wed/Fri"

    def test_doubtful_and_out_unchanged(self):
        assert qb_coupling.starter_sit_weight("Doubtful")["weight"] == qb_coupling.DOUBTFUL_WEIGHT
        # Practice does not move a Doubtful starter either way.
        assert qb_coupling.starter_sit_weight(
            "Doubtful", _practice("FP", "FP"))["weight"] == qb_coupling.DOUBTFUL_WEIGHT
        assert qb_coupling.starter_sit_weight("Out")["weight"] == qb_coupling.OUT_WEIGHT
        assert qb_coupling.starter_sit_weight("IR")["weight"] == qb_coupling.OUT_WEIGHT
        assert qb_coupling.starter_sit_weight(None)["weight"] == 0.0

    def test_report_says_unlikely_to_play(self):
        sit = qb_coupling.starter_sit_weight("Questionable", _practice("DNP"),
                                             [_flag("unlikely_to_play")])
        assert sit["weight"] == qb_coupling.QUESTIONABLE_NEWS_WEIGHT and sit["basis"] == "news"
        # A stale blurb, or a limited practice since, does not count.
        assert qb_coupling.starter_sit_weight(
            "Questionable", None, [_flag("unlikely_to_play", 0.2)])["weight"] \
            == qb_coupling.QUESTIONABLE_WEIGHT
        assert qb_coupling.starter_sit_weight(
            "Questionable", _practice("DNP", "LP"), [_flag("ruled_out")])["weight"] \
            == qb_coupling.QUESTIONABLE_LIMITED_WEIGHT

    def test_expected_to_play_keeps_him_uncut(self):
        sit = qb_coupling.starter_sit_weight("Questionable", _practice("DNP", "DNP"),
                                             [_flag("expected_to_play")])
        assert sit["weight"] == 0.0


def _with_practice(status_of, practice):
    status_of.practice_week = lambda name, team: practice if name == "Starter Qb" else None
    return status_of


class TestReceiverContext:
    def test_questionable_dnp_dnp_is_a_partial_cut(self):
        depth, status_of = _depth("Questionable")
        ctx = qb_coupling.receiver_context(
            depth, "TB", "WR", "Wide One", _with_practice(status_of, _practice("DNP", "DNP")))
        m = qb_coupling.RECEIVER_MULT["WR"]["low"]
        assert ctx["applied"]
        assert ctx["sleeper_mult"] == round(1 - (1 - m) * qb_coupling.QUESTIONABLE_DNP_WEIGHT, 3)
        assert ctx["starter_sit_weight"] == 0.75 and ctx["starter_practice"] == "DNP-DNP"
        assert ctx["reason"].startswith(
            "Starter Qb Questionable, DNP Wed/Thu — 75% weight: Backup Qb (low backup")

    def test_questionable_limited_is_a_smaller_cut(self):
        depth, status_of = _depth("Questionable")
        ctx = qb_coupling.receiver_context(
            depth, "TB", "WR", "Wide One", _with_practice(status_of, _practice("DNP", "LP")))
        m = qb_coupling.RECEIVER_MULT["WR"]["low"]
        assert ctx["applied"] and ctx["starter_sit_weight"] == \
            qb_coupling.QUESTIONABLE_LIMITED_WEIGHT
        assert ctx["sleeper_mult"] == round(
            1 - (1 - m) * qb_coupling.QUESTIONABLE_LIMITED_WEIGHT, 3)
        # Without any practice data: the questionable weight.
        depth, status_of = _depth("Questionable")
        ctx = qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", status_of)
        assert ctx["starter_sit_weight"] == qb_coupling.QUESTIONABLE_WEIGHT

    def test_questionable_full_practice_is_no_context(self):
        depth, status_of = _depth("Questionable")
        assert qb_coupling.receiver_context(
            depth, "TB", "WR", "Wide One",
            _with_practice(status_of, _practice("LP", "FP"))) is None

    def test_doubtful_unchanged(self):
        depth, status_of = _depth("Doubtful")
        ctx = qb_coupling.receiver_context(
            depth, "TB", "WR", "Wide One", _with_practice(status_of, _practice("FP", "FP")))
        m = qb_coupling.RECEIVER_MULT["WR"]["low"]
        assert ctx["sleeper_mult"] == round(1 - (1 - m) * qb_coupling.DOUBTFUL_WEIGHT, 3)
        assert ctx["starter_sit_basis"] == "status"


class TestMultiGame:
    @pytest.mark.parametrize("text,phrase", [
        ("Rapoport reports Jackson could miss multiple games.", "multiple games"),
        ("Jackson is considered week-to-week.", "week-to-week"),
        ("He is facing a multi-week absence.", "multiple games"),
    ])
    def test_phrase(self, text, phrase):
        assert ros.multi_game_phrase(text) == phrase

    def test_no_phrase(self):
        assert ros.multi_game_phrase("Jackson missed practice Thursday.") is None
        assert ros.multi_game_phrase(None) is None

    def test_out_with_multiple_games_is_two(self):
        games, reason = ros.expected_absence("Out", "could miss multiple games")
        assert games == ros.WEEK_TO_WEEK_GAMES and "multiple games" in reason

    def test_questionable_multi_game_absence(self):
        assert qb_coupling.multi_game_absence(
            {"description": "could miss multiple games"}) == (ros.WEEK_TO_WEEK_GAMES,
                                                              "multiple games")
        assert qb_coupling.multi_game_absence(
            {"description": "ankle"}, [_flag("week_to_week")]) == (ros.WEEK_TO_WEEK_GAMES,
                                                                    "week-to-week")
        assert qb_coupling.multi_game_absence({"description": "ankle"}) == (0, None)


class TestTextReads:
    def test_remained_absent_from_practice_is_a_dnp(self):
        note = practice_reports.parse_espn_practice_note(
            "Jackson (ankle) remained absent from practice Thursday, Sam Cohn of the "
            "Baltimore Sun reports.", "2026-10-08T17:09Z")
        assert note == {"status": "DNP", "date": "2026-10-08", "estimated": False}

    def test_news_flags(self):
        flags = {h["flag"] for h in news_signals.classify(
            "Rapoport said Jackson has only an outside chance to play and could miss "
            "multiple games.")}
        assert {"unlikely_to_play", "week_to_week"} <= flags

    def test_stored_days_plus_the_current_blurb(self):
        class _Db:
            def get_practice_reports(self, name, team):
                return [{"date": "2026-10-07", "status": "DNP", "source": "nfl.com"}]
        summary = practice_reports.lookup_practice_with_note(
            _Db(), "Lamar Jackson", "BAL",
            "Jackson (ankle) remained absent from practice Thursday.", "2026-10-08T17:09Z",
            today=date(2026, 10, 8))
        assert summary["pattern"] == "DNP-DNP"
        # A blurb from an earlier week is not this week's practice.
        summary = practice_reports.lookup_practice_with_note(
            _Db(), "Lamar Jackson", "BAL",
            "Jackson did not practice Thursday.", "2026-09-24T17:09Z", today=date(2026, 10, 8))
        assert summary["pattern"] == "DNP"
        # A stored row for the same day wins over the blurb.
        summary = practice_reports.lookup_practice_with_note(
            _Db(), "Lamar Jackson", "BAL", "Jackson was limited at practice Wednesday.",
            "2026-10-07T20:00Z", today=date(2026, 10, 8))
        assert summary["pattern"] == "DNP"


class _PracticeDb:
    def __init__(self, rows, practice):
        self.rows, self.practice = rows, practice

    def get_all_current_injuries(self):
        return self.rows

    def get_practice_reports(self, name, team, season=None, week=None):
        return self.practice if name == "Starter Qb" else []


class TestOnTheProjection:
    @pytest.mark.asyncio
    async def test_questionable_dnp_week_cuts_and_multi_game_persists(self, monkeypatch):
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs",
                            AsyncMock(return_value=_tb_logs()))
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_sleeper())))
        rows = [{"player_name": "Starter Qb", "team_id": "TB", "injury_status": "Questionable",
                 "injury_description": "Starter Qb could miss multiple games.",
                 "date_reported": datetime.now(UTC).isoformat()}]
        player = {"name": "Wide One", "position": "WR", "team": "TB", "opponent": "DAL",
                  "player_id": "1"}
        eng = _engine_with([], _VALUES)
        eng.db = _PracticeDb(rows, [
            {"date": "2026-10-07", "status": "DNP", "source": "nfl.com"},
            {"date": "2026-10-08", "status": "DNP", "source": "nfl.com"}])
        p = (await eng.project_many([player], season=2026, week=5))["projections"][0]
        ctx = p["qb_context"]
        m = qb_coupling.RECEIVER_MULT["WR"]["low"]
        assert ctx["applied"]
        assert ctx["sleeper_mult"] == round(1 - (1 - m) * qb_coupling.QUESTIONABLE_DNP_WEIGHT, 3)
        assert ctx["games_out"] == ros.WEEK_TO_WEEK_GAMES
        assert "multiple games per the report" in ctx["reason"]

        # Limited on the latest day: the limited weight.
        eng.db.practice = [{"date": "2026-10-07", "status": "DNP", "source": "nfl.com"},
                           {"date": "2026-10-08", "status": "LP", "source": "nfl.com"}]
        rows[0]["injury_description"] = "Starter Qb was a limited participant Thursday."
        p = (await eng.project_many([player], season=2026, week=5))["projections"][0]
        assert p["qb_context"]["sleeper_mult"] == round(
            1 - (1 - m) * qb_coupling.QUESTIONABLE_LIMITED_WEIGHT, 3)
        # Full on the latest day: no context at all.
        eng.db.practice = [{"date": "2026-10-07", "status": "DNP", "source": "nfl.com"},
                           {"date": "2026-10-08", "status": "FP", "source": "nfl.com"}]
        rows[0]["injury_description"] = "Starter Qb was a full participant Thursday."
        p = (await eng.project_many([player], season=2026, week=5))["projections"][0]
        assert "qb_context" not in p
