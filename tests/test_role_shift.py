"""Role shifts: a recent change of role, read from weekly usage (pure)."""
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import opportunity, opportunity_tools, projections, role_shift, usage_trends
from nfl_mcp import sleeper_projections as sp
from tests.test_sleeper_first_blend import _engine
from tests.test_vacated_volume import _games


def _row(week, snap=None, carries=None, target=None, rz=None, status="played"):
    if status != "played":
        return {"week": week, "status": status}
    return {"week": week, "status": "played", "snap_share": snap, "carries_share": carries,
            "target_share": target, "rz_opportunities": rz}


def _swift():
    """Benched after a fumble in week 4: snaps 69% -> 36%, carries 57% -> 28%."""
    return [_row(1, 70, 58, 10, 2), _row(2, 68, 56, 9, 2), _row(3, 69, 57, 11, 3),
            _row(4, 36, 28, 8, 1)]


class TestClassify:
    def test_a_one_week_benching_is_a_lost_role(self):
        got = role_shift.classify(_swift(), "RB")
        assert got["role_trend"] == "role_down"
        assert got["break_week"] == got["reweight_from_week"] == 4
        assert got["recent_weeks"] == 1
        assert role_shift.MIN_MULTIPLIER <= got["role_multiplier"] < 1.0
        assert got["confidence_delta"] == role_shift.SHIFT_CONFIDENCE_DELTA
        assert "carries share 57%→28% (week 4)" in got["role_flags"]
        assert any(f.startswith("snap share 69%→36%") for f in got["role_flags"])

    def test_a_two_week_committee_reads_over_both_weeks(self):
        rows = [_row(1, 70, 57, 10), _row(2, 75, 72, 12), _row(3, 45, 30, 9),
                _row(4, 40, 28, 8)]
        got = role_shift.classify(rows, "RB")
        assert got["role_trend"] == "role_down" and got["recent_weeks"] == 2
        assert any("(weeks 3-4)" in f for f in got["role_flags"])
        # The two-week read is weighted in full, so it moves more than one week would.
        one = role_shift.classify(rows[:2] + rows[3:], "RB")
        assert got["role_multiplier"] <= one["role_multiplier"]

    def test_multiplier_is_bounded(self):
        rows = [_row(w, 95, 90, 20) for w in (1, 2, 3)] + [_row(4, 5, 2, 0), _row(5, 5, 2, 0)]
        got = role_shift.classify(rows, "RB")
        assert got["role_multiplier"] == role_shift.MIN_MULTIPLIER

    def test_a_gained_role_is_flagged_not_priced(self):
        rows = [_row(1, 40, 30, 5), _row(2, 42, 28, 6), _row(3, 85, 75, 9), _row(4, 90, 80, 10)]
        got = role_shift.classify(rows, "RB")
        assert got["role_trend"] == "role_up"
        assert got["role_multiplier"] == 1.0 and got["reweight_from_week"] is None
        assert got["role_flags"]

    def test_a_tight_ends_lost_role_is_reweighted_not_multiplied(self):
        rows = [_row(1, 80, None, 22), _row(2, 82, None, 24), _row(3, 79, None, 21),
                _row(4, 50, None, 9), _row(5, 48, None, 8)]
        got = role_shift.classify(rows, "TE")
        assert got["role_trend"] == "role_down" and got["role_multiplier"] == 1.0
        assert got["reweight_from_week"] == 4

    def test_noise_is_stable(self):
        rows = [_row(1, 70, 55, 10), _row(2, 64, 50, 12), _row(3, 72, 58, 9), _row(4, 66, 49, 11)]
        got = role_shift.classify(rows, "RB")
        assert got["role_trend"] == "stable" and got["role_multiplier"] == 1.0
        assert got["role_flags"] == []

    def test_snaps_alone_do_not_make_a_role(self):
        # A personnel-package swing with the same carries is not a new role.
        rows = [_row(1, 70, 50, 10), _row(2, 70, 50, 10), _row(3, 70, 50, 10), _row(4, 40, 50, 10)]
        assert role_shift.classify(rows, "RB")["role_trend"] == "stable"

    def test_byes_and_missed_weeks_are_not_played_weeks(self):
        rows = _swift()
        with_gaps = [rows[0], _row(2, status="bye"), rows[1],
                     _row(4, status="injured"), rows[2], rows[3]]
        # Same verdict as without the gaps: a week out is not a lost role.
        assert role_shift.classify(with_gaps, "RB")["role_trend"] == "role_down"
        healthy_then_hurt = [rows[0], rows[1], rows[2], _row(5, status="injured")]
        assert role_shift.classify(healthy_then_hurt, "RB")["role_trend"] == "stable"

    def test_too_few_weeks_and_quarterbacks_are_insufficient(self):
        assert role_shift.classify(_swift()[:2], "RB")["role_trend"] == "insufficient_data"
        assert role_shift.classify(_swift(), "QB")["role_trend"] == "insufficient_data"
        assert role_shift.classify([], None)["role_multiplier"] == 1.0

    def test_missing_snap_data_still_reads_shares(self):
        rows = [{**r, "snap_share": None, "rz_opportunities": None} for r in _swift()]
        assert role_shift.classify(rows, "RB")["role_trend"] == "role_down"


class TestPlayerRows:
    def test_rows_come_from_the_logs_and_sleeper_lines(self):
        entry = {"team": "CHI", "games": [
            {"week": w, "team": "CHI", "carries": c, "target_share": 0.1}
            for w, c in ((1, 15), (2, 14), (3, 15))]}
        logs = {"x": entry, "y": {"team": "CHI", "games": [
            {"week": w, "team": "CHI", "carries": 10} for w in (1, 2, 3, 4)]}}
        rows = role_shift.player_rows(
            entry, "CHI", [1, 2, 3, 4], usage_trends.team_week_carries(logs),
            usage_trends.teams_with_games(logs),
            {1: {"9": {"off_snp": 40, "tm_off_snp": 60}}}, "9")
        assert [r["status"] for r in rows] == ["played", "played", "played", "did_not_play"]
        assert rows[0]["snap_share"] == pytest.approx(66.7)
        assert rows[0]["carries_share"] == 60.0 and rows[0]["target_share"] == 10.0


class TestChangePointWeights:
    def test_weights_from_the_break_week(self):
        games = [{"week": w} for w in (1, 2, 3, 4)]
        assert opportunity.recency_weights(games) == [1, 2, 3, 4]
        assert opportunity.recency_weights(games, 4) == [1, 2, 3, 4 * opportunity.POST_BREAK_WEIGHT]

    def test_a_lost_role_lowers_the_volume_base(self):
        games = _games([1, 2, 3], carries=18) + _games([4], carries=8)
        plain = opportunity.project_opportunity(games, "RB")
        weighted = opportunity.project_opportunity(games, "RB", break_week=4)
        assert weighted < plain


def _rb_logs():
    games = (_games([1, 2, 3], carries=16.0) + _games([4], carries=7.0))
    for g in games:
        g["team"] = "CHI"
    mate = _games([1, 2, 3], carries=8.0) + _games([4], carries=17.0)
    for g in mate:
        g["team"] = "CHI"
    return {"a": {"player_id": "a", "name": "Benched Back", "position": "RB", "team": "CHI",
                  "games": games},
            "b": {"player_id": "b", "name": "Other Back", "position": "RB", "team": "CHI",
                  "games": mate}}


class TestOnTheBlend:
    @pytest.mark.asyncio
    async def test_lost_role_multiplies_sleepers_share_only(self, monkeypatch):
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_rb_logs()))
        payload = [{"player_id": "1", "team": "CHI", "opponent": "MIN",
                    "player": {"first_name": "Benched", "last_name": "Back", "position": "RB",
                               "team": "CHI"},
                    "stats": {"rush_att": 12.0, "rush_yd": 50.0, "pts_ppr": 10.0}}]
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(payload)))
        player = {"name": "Benched Back", "position": "RB", "team": "CHI", "opponent": "MIN",
                  "player_id": "1"}
        out = await _engine().project_many([player], scoring="ppr", season=2026, week=5)
        p = out["projections"][0]
        assert p["role_trend"] == "role_down"
        assert p["role_flags"] and 0.8 <= p["role_multiplier"] < 1.0
        assert p["breakdown"]["role_reweight_from_week"] == 4
        assert p["breakdown"]["role_mult"] == p["role_multiplier"]
        assert p["projected_points"] == round(
            0.25 * p["model_projection"] + 0.75 * p["sleeper_projection"] * p["role_multiplier"], 1)
        vol = projections._VOLATILITY["RB"]
        assert p["floor"] == round(p["projected_points"] * (1 - vol), 1)
        assert p["ceiling"] == round(p["projected_points"] * (1 + vol), 1)

    @pytest.mark.asyncio
    async def test_model_only_is_not_multiplied_again(self, monkeypatch):
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_rb_logs()))
        player = {"name": "Benched Back", "position": "RB", "team": "CHI", "opponent": "MIN"}
        p = (await _engine().project_many([player], scoring="ppr", season=2026, week=5)
             )["projections"][0]
        assert p["projection_source"] == "model_only"
        assert p["role_trend"] == "role_down"
        assert p["projected_points"] == p["model_projection"]


class TestInjuryShortenedGames:
    """A game he left hurt is not a lost role (Chase, Jefferson); a healthy
    benching still is (Swift)."""

    KICKOFFS = {1: "2026-09-13T17:00Z", 2: "2026-09-20T17:00Z", 3: "2026-09-27T17:00Z",
                4: "2026-10-04T17:00Z"}

    @staticmethod
    def _chase():
        """WR, ~94% of snaps, concussed early in week 4: 30% of snaps."""
        return [_row(1, 94, None, 26, 1), _row(2, 95, None, 25, 1), _row(3, 93, None, 27, 2),
                _row(4, 30, None, 8, 0)]

    CONCUSSION = [{"injury_status": "Active", "recorded_at": "2026-09-16T07:00:00+00:00"},
                  {"injury_status": "Questionable", "recorded_at": "2026-10-04T18:31:00+00:00"},
                  {"injury_status": "Out", "recorded_at": "2026-10-04T19:31:00+00:00"}]

    def test_chase_concussion_week_is_excluded_and_not_role_down(self):
        rows = self._chase()
        assert role_shift.classify(rows, "WR")["role_trend"] == "role_down"  # the old read
        assert role_shift.injury_exit_weeks(rows, self.CONCUSSION, self.KICKOFFS) == {4}
        rows[3]["injury_exit"] = True
        got = role_shift.classify(rows, "WR")
        assert got["role_trend"] != "role_down"
        assert got["role_multiplier"] == 1.0 and got["reweight_from_week"] is None

    def test_swift_benched_healthy_is_still_role_down(self):
        # A hip report three days later (Wednesday practice) is not an in-game injury.
        history = [{"injury_status": "Active", "recorded_at": "2026-10-02T19:48:00+00:00"},
                   {"injury_status": "Questionable", "recorded_at": "2026-10-07T20:27:00+00:00"}]
        rows = _swift()
        assert role_shift.injury_exit_weeks(rows, history, self.KICKOFFS) == set()
        assert role_shift.injury_exit_weeks(rows) == set()
        assert role_shift.classify(rows, "RB")["role_trend"] == "role_down"

    def test_a_designation_already_on_file_before_kickoff_is_not_new(self):
        history = [{"injury_status": "Questionable", "recorded_at": "2026-10-02T12:00:00+00:00"},
                   {"injury_status": "Out", "recorded_at": "2026-10-05T12:00:00+00:00"}]
        assert role_shift.injury_exit_weeks(self._chase(), history, self.KICKOFFS) == set()

    def test_low_snaps_then_a_missed_game_is_an_exit_without_reports(self):
        """Jefferson: 12% of snaps in week 3, out in week 4."""
        rows = [_row(1, 96, None, 30), _row(2, 97, None, 33), _row(3, 12, None, 8),
                _row(4, status="injured")]
        assert role_shift.injury_exit_weeks(rows) == {3}
        # A full game before the missed one is no exit (the low-snap rule).
        rows[2] = _row(3, 95, None, 30)
        assert role_shift.injury_exit_weeks(rows) == set()

    def test_player_rows_marks_the_exit_and_classify_skips_it(self, monkeypatch):
        def _week_row(wk, game, sleeper, team, *a, **k):
            return next(r for r in self._chase() if r["week"] == wk)
        monkeypatch.setattr(usage_trends, "week_row", _week_row)
        rows = role_shift.player_rows({"name": "Ja'Marr Chase"}, "CIN", [1, 2, 3, 4], {}, {},
                                      injury_history=self.CONCUSSION, kickoffs=self.KICKOFFS)
        assert [r["week"] for r in rows if r.get("injury_exit")] == [4]
        assert role_shift.classify(rows, "WR")["role_trend"] != "role_down"

    def test_without_a_database_nothing_is_looked_up(self, monkeypatch):
        from nfl_mcp import database
        monkeypatch.setattr(database, "_shared_db", None)
        assert role_shift._injury_context("Ja'Marr Chase", "CIN", [4]) == ([], {})
