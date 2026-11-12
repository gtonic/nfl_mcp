"""Availability on game day, from week 9 2026.

- Diggs: DNP-DNP-DNP with no designation. The briefing's late-game block read
  no practice at all (the report sat on the input, not the injury), so it
  called him safe while `get_lineup_contingencies` flagged him; and after
  the Commanders' game roster was published he stayed "at risk" there, since
  `PlayerAnalysis` dropped the gameday decision.
- The final report carries every game designation: no injury and no game
  status on it means rest/personal, he plays.
- Michael Wilson: Q on Sleeper since Monday, off the Cardinals' final report
  Friday ("not on the final injury report"), still priced as questionable.
"""
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from nfl_mcp import late_risk as lr
from nfl_mcp import news_signals
from nfl_mcp import practice_reports as pr
from nfl_mcp.database import NFLDatabase
from nfl_mcp.injury_match import build_injury_index, designation_cleared_by, resolve_injury
from nfl_mcp.lineup_optimizer_tools import PlayerAnalysis, _gameday_reason

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
WEEK = {"season": 2026, "week": 9, "source": "nfl.com", "game_date": "2026-11-08"}


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield NFLDatabase(str(Path(tmp) / "t.db"))


def _row(name, team, day, status, injury=None, game_status=None):
    return {**WEEK, "player_name": name, "team": team, "date": day, "status": status,
            "injury": injury, "game_status": game_status}


def _wsh_week(db, friday=True):
    rows = [_row("Stefon Diggs", "WSH", "2026-11-04", "DNP"),
            _row("Stefon Diggs", "WSH", "2026-11-05", "DNP"),
            _row("Terry McLaurin", "WSH", "2026-11-04", "DNP"),
            _row("Terry McLaurin", "WSH", "2026-11-05", "DNP")]
    if friday:
        rows += [_row("Stefon Diggs", "WSH", "2026-11-06", "DNP"),
                 _row("Terry McLaurin", "WSH", "2026-11-06", "DNP", "Knee", "Out")]
    db.upsert_practice_status(rows)


def _no_next_week():
    return patch.object(pr, "reads_next_week", lambda _db, s, w, now=None: False)


class TestFinalReport:
    def test_none_before_the_final_report(self, db):
        _wsh_week(db, friday=False)
        with _no_next_week():
            assert db.get_team_final_report("WSH", 2026, 9) is None
            got = pr.lookup_practice(db, "Stefon Diggs", "WSH", season=2026, week=9)
        assert got["pattern"] == "DNP-DNP" and "final_report" not in got
        # Midweek every DNP reads "no injury listed": no note yet.
        assert "note" not in got

    def test_final_report_found_by_its_game_statuses(self, db):
        _wsh_week(db)
        with _no_next_week():
            report = db.get_team_final_report("WAS", 2026, 9)  # team alias
        assert report["date"] == "2026-11-06"
        assert report["players"]["terry mclaurin"]["game_status"] == "Out"
        assert report["players"]["stefon diggs"] == {"status": "DNP", "injury": None, "game_status": None}

    def test_dnp_week_with_nothing_listed_reads_as_rest(self, db):
        _wsh_week(db)
        with _no_next_week():
            got = pr.lookup_practice(db, "Stefon Diggs", "WSH", season=2026, week=9)
            mclaurin = pr.lookup_practice(db, "Terry McLaurin", "WSH", season=2026, week=9)
        assert got["final_report"] == {"date": "2026-11-06", "listed": True,
                                       "game_status": None, "injury": None}
        assert pr.no_designation_on_final(got)
        assert "expected to play" in got["note"]
        assert "expected to play" in pr.practice_fields(got)["practice_note"]
        # An injured DNP week has its injury named: no note, a designation.
        assert mclaurin["injury"] == "Knee" and "note" not in mclaurin
        assert not pr.no_designation_on_final(mclaurin)

    def test_an_early_ruling_is_not_the_final_report(self, db):
        """Patriots, week 10 2026 (Sunday 11-15): three players ruled out on
        Wednesday. Read as the final report, that cleared every Sleeper
        "Questionable" on the team by Thursday."""
        ne = {"season": 2026, "week": 10, "source": "nfl.com", "game_date": "2026-11-15"}
        db.upsert_practice_status([
            {**ne, "player_name": "Ben Brown", "team": "NE", "date": "2026-11-11",
             "status": "DNP", "injury": "Knee", "game_status": "Out"},
            {**ne, "player_name": "AJ Brown", "team": "NE", "date": "2026-11-11", "status": "LP"},
            {**ne, "player_name": "AJ Brown", "team": "NE", "date": "2026-11-12", "status": "LP"},
        ])
        with _no_next_week():
            assert db.get_team_final_report("NE", 2026, 10) is None
            assert pr.final_designation(db, "AJ Brown", "NE", season=2026, week=10) is None
            got = pr.lookup_practice(db, "AJ Brown", "NE", season=2026, week=10)
        assert "final_report" not in got and not pr.no_designation_on_final(got)
        # Friday's report: now it is out, and AJ Brown is on it without a designation.
        db.upsert_practice_status([
            {**ne, "player_name": "Ben Brown", "team": "NE", "date": "2026-11-13",
             "status": "DNP", "injury": "Knee", "game_status": "Out"},
            {**ne, "player_name": "AJ Brown", "team": "NE", "date": "2026-11-13", "status": "FP"},
        ])
        with _no_next_week():
            report = db.get_team_final_report("NE", 2026, 10)
        assert report["date"] == "2026-11-13"
        assert report["players"]["aj brown"]["game_status"] is None
        assert report["players"]["ben brown"]["game_status"] == "Out"

    def test_a_thursday_teams_final_report_is_wednesdays(self, db):
        wsh = {"season": 2026, "week": 10, "source": "nfl.com", "game_date": "2026-11-12"}
        db.upsert_practice_status([
            {**wsh, "player_name": "Lucas Patrick", "team": "WSH", "date": "2026-11-10", "status": "DNP"},
            {**wsh, "player_name": "Lucas Patrick", "team": "WSH", "date": "2026-11-11",
             "status": "DNP", "injury": "Concussion", "game_status": "Out"},
        ])
        with _no_next_week():
            report = db.get_team_final_report("WSH", 2026, 10)
        assert report["date"] == "2026-11-11"

    def test_game_date_from_the_schedule_when_the_rows_have_none(self, db):
        db.upsert_schedule_games([{"season": 2026, "week": 10, "team": "NE", "opponent": "DET",
                                   "is_home": 1, "kickoff": "2026-11-15T14:30Z"}])
        ne = {"season": 2026, "week": 10, "source": "nfl.com"}
        db.upsert_practice_status([
            {**ne, "player_name": "Ben Brown", "team": "NE", "date": "2026-11-11",
             "status": "DNP", "injury": "Knee", "game_status": "Out"},
        ])
        with _no_next_week():
            assert db.get_team_final_report("NE", 2026, 10) is None
        db.upsert_practice_status([
            {**ne, "player_name": "Ben Brown", "team": "NE", "date": "2026-11-13",
             "status": "DNP", "injury": "Knee", "game_status": "Out"},
        ])
        with _no_next_week():
            assert db.get_team_final_report("NE", 2026, 10)["date"] == "2026-11-13"

    def test_without_a_game_date_only_a_friday_or_saturday_counts(self, db):
        rows = [{"season": 2026, "week": 10, "source": "nfl.com", "player_name": "Ben Brown",
                 "team": "NE", "date": "2026-11-11", "status": "DNP", "injury": "Knee",
                 "game_status": "Out"}]
        db.upsert_practice_status(rows)
        with _no_next_week():
            assert db.get_team_final_report("NE", 2026, 10) is None
        db.upsert_practice_status([{**rows[0], "date": "2026-11-13"}])
        with _no_next_week():
            assert db.get_team_final_report("NE", 2026, 10)["date"] == "2026-11-13"

    def test_a_news_note_on_the_final_day_is_not_the_report(self, db):
        db.upsert_practice_status([
            _row("Michael Wilson", "ARI", "2026-11-05", "LP"),
            {**_row("Michael Wilson", "ARI", "2026-11-06", "LP"), "source": "espn_news"},
        ])
        with _no_next_week():
            assert db.get_team_final_report("ARI", 2026, 9) is None

    def test_left_off_the_final_report(self, db):
        db.upsert_practice_status([
            _row("Michael Wilson", "ARI", "2026-11-04", "LP"),
            _row("Michael Wilson", "ARI", "2026-11-05", "LP"),
            _row("Bam Knight", "ARI", "2026-11-06", "DNP", "Ankle", "Out"),
        ])
        with _no_next_week():
            final = pr.final_designation(db, "Michael Wilson", "ARI", season=2026, week=9)
        assert final == {"date": "2026-11-06", "listed": False, "game_status": None, "injury": None}


class TestStaleDesignation:
    def _wilson(self, status="Questionable"):
        return {"full_name": "Michael Wilson", "team_id": "ARI", "position": "WR",
                "raw": json.dumps({"injury_status": status})}

    def test_final_report_without_designation_clears_sleeper_q(self):
        final = {"date": "2026-11-06", "listed": False, "game_status": None}
        assert resolve_injury(self._wilson(), {}, "ARI", None, final) is None
        assert designation_cleared_by("Questionable", None, final) == "final_report"

    def test_designation_on_the_final_report_stands(self):
        final = {"date": "2026-11-06", "listed": True, "game_status": "Questionable"}
        assert resolve_injury(self._wilson(), {}, "ARI", None, final)["status"] == "Questionable"
        # Nor before the final report is out.
        assert resolve_injury(self._wilson(), {}, "ARI", None, None)["status"] == "Questionable"

    def test_espn_report_designation_is_not_overridden(self):
        report = {"player_name": "Michael Wilson", "team_id": "ARI", "position": "WR",
                  "injury_status": "Questionable", "injury_description": "ribs"}
        final = {"date": "2026-11-06", "listed": False, "game_status": None}
        injury = resolve_injury(self._wilson(), build_injury_index([report]), "ARI", None, final)
        assert injury["status"] == "Questionable"

    def test_out_and_reserve_are_not_cleared_by_absence(self):
        final = {"date": "2026-11-06", "listed": False, "game_status": None}
        assert designation_cleared_by("Out", None, final) is None
        assert designation_cleared_by("IR", None, final) is None

    @pytest.mark.parametrize("text", [
        "Michael Wilson (NIR-rest/ribs) is not on the final injury report for Week 9 against the Seahawks.",
        "Wilson (rest/ribs) doesn't have a designation for Sunday's contest in Seattle.",
        "Alvin Kamara has no injury designation for Week 9 against the Browns.",
    ])
    def test_news_saying_no_designation(self, text):
        flags = [{**h, "date_reported": "2026-11-06T23:53:00+00:00"}
                 for h in news_signals.classify(text, "michael wilson", None, "wilson")
                 or news_signals.classify(text, "alvin kamara", None, "kamara")]
        assert news_signals.designation_cleared(flags, NOW)
        # `designation_cleared_by` reads the clock: the note must be fresh.
        fresh = [{**f, "date_reported": (datetime.now(UTC) - timedelta(hours=2)).isoformat()}
                 for f in flags]
        assert designation_cleared_by("Questionable", None, None, fresh) == "news"

    def test_news_expected_to_play_despite_tag_does_not_clear(self):
        text = "Wilson is expected to play despite his questionable tag."
        flags = [{**h, "date_reported": "2026-11-06T20:00:00+00:00"}
                 for h in news_signals.classify(text, "michael wilson", None, "wilson")]
        assert any(f["flag"] == "expected_to_play" for f in flags)
        assert not news_signals.designation_cleared(flags, NOW)

    def test_later_doubt_and_stale_news_do_not_clear(self):
        cleared = {"flag": "expected_to_play", "date_reported": "2026-11-06T20:00:00+00:00",
                   "snippet": "Wilson is not on the final injury report."}
        doubt = {"flag": "game_time_decision", "date_reported": "2026-11-07T10:00:00+00:00",
                 "snippet": "Wilson is a game-time decision."}
        assert not news_signals.designation_cleared([cleared, doubt], NOW)
        old = {**cleared, "date_reported": "2026-10-30T20:00:00+00:00"}
        assert not news_signals.designation_cleared([old], NOW)


class TestLateRiskPractice:
    def test_briefing_row_reads_the_inputs_practice(self):
        # No designation: the projection row and the input's injury carry no
        # practice, the input's own report does.
        practice = {"latest": "DNP", "pattern": "DNP-DNP"}
        row = lr.from_projection({"player": "Stefon Diggs", "team": "WSH", "position": "WR",
                                  "projected_points": 10.1},
                                 {"player_id": "2449", "practice": practice}, {}, NOW)
        assert row["practice_pattern"] == "DNP-DNP" and row["practice_status"] == "DNP"
        assert lr.risk_reasons(row) == ["practice DNP-DNP"]
        assert lr.play_probability(row)[0] == lr.PLAY_PROBABILITY["healthy_dnp"]

    def test_no_designation_on_the_final_report_is_no_risk(self):
        practice = {"latest": "DNP", "pattern": "DNP-DNP-DNP",
                    "final_report": {"date": "2026-11-06", "listed": True, "game_status": None},
                    "note": "DNP with no injury listed ..."}
        row = lr.from_projection({"player": "Stefon Diggs", "team": "WSH", "position": "WR",
                                  "projected_points": 10.1},
                                 {"practice": practice}, {}, NOW)
        assert row["no_designation_on_final"] and row["practice_note"]
        assert lr.risk_reasons(row) == []
        p, basis = lr.play_probability(row)
        assert p == lr.PLAY_PROBABILITY["healthy_dnp_final"] and "final report" in basis
        # Unless the news says otherwise.
        assert lr.risk_reasons({**row, "news_flags": [{"flag": "game_time_decision"}]})

    def test_analysis_rows_carry_the_gameday_decision(self):
        a = PlayerAnalysis(player_name="Stefon Diggs", player_id="2449", position="WR",
                           team="WSH", opponent="LAR", practice_status="DNP",
                           practice_pattern="DNP-DNP-DNP")
        assert _gameday_reason(a, {"gameday_status": "active"})
        row = lr.from_analysis(a.to_dict(), "WR")
        assert row["gameday_status"] == "active"
        assert lr.risk_reasons(row) == []
        assert lr.play_probability(row)[0] == 1.0

    def test_analysis_rows_carry_the_final_report_reading(self):
        a = PlayerAnalysis(player_name="Stefon Diggs", player_id="2449", position="WR",
                           team="WSH", opponent="LAR", practice_status="DNP",
                           practice_pattern="DNP-DNP-DNP", no_designation_on_final=True,
                           practice_note="DNP with no injury listed ...")
        row = lr.from_analysis(a.to_dict(), "WR")
        assert row["no_designation_on_final"] and lr.risk_reasons(row) == []
