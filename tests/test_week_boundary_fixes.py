"""Monday of week 10 2026, looking at week 11.

What the tools said then, and should not have:

* Kenneth Walker (held out Sunday, "expected to go next Sunday") and Brock
  Bowers (left the game, "day-to-day") were ``Out`` for week 11 and projected
  0.0 -- the feeds still carried the status of the game already played;
* the week-10 briefing, every game of the roster played, came back with no
  lineup, no win probability and all ten starters under ``move_to_bench``;
* a lead back had been dropped on his bye for a kicker and no plan check
  would have said a word;
* the calibration fit counted the week still being played;
* the signal review read "expected_to_play multiplier 0.934 looks too low"
  for a flag whose weight is 1.0.
"""
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from nfl_mcp import calibration, signal_review, status_carryover
from nfl_mcp.briefing_tools import week_decided
from nfl_mcp.injury_match import resolve_injury
from nfl_mcp.move_planner import DROP_GUARD_POINTS, simulate
from nfl_mcp.win_probability import get_win_probability_lineup
from tests.test_move_planner import _ctx, _moves

MONDAY = datetime(2026, 11, 16, 20, 30, tzinfo=UTC)   # after Sunday of week 10
SUNDAY_10 = "2026-11-15T18:00Z"
SUNDAY_11 = "2026-11-22T18:00Z"


class FakeDb:
    """Schedule, practice days and report rows -- what the carry-over reads."""

    def __init__(self, kickoffs, practice=None, reports=None):
        self.kickoffs, self.practice, self.reports = kickoffs, practice or {}, reports or []

    def get_schedule_week_spans(self):
        return [{"season": 2026, "week": w, "last_kickoff": max(k.values())}
                for w, k in self.kickoffs.items()]

    def get_week_kickoffs(self, season, week):
        return self.kickoffs.get(week, {})

    def get_team_practice_snapshots(self, season, week, source=None):
        return self.practice.get(week, {})

    def get_all_current_injuries(self):
        return self.reports


def _db(**kw):
    return FakeDb({10: {"KC": SUNDAY_10, "LV": SUNDAY_10, "LAC": "2026-11-17T01:15Z", "MIN": SUNDAY_10},
                   11: {"KC": SUNDAY_11, "LV": SUNDAY_11, "LAC": SUNDAY_11, "MIN": SUNDAY_11}}, **kw)


def _athlete(name, team, status):
    return {"full_name": name, "team_id": team, "position": "RB",
            "raw": json.dumps({"injury_status": status})}


class TestCarriedTeams:
    def test_team_that_played_and_has_no_new_practice_report_is_carried(self):
        carried = status_carryover.carried_teams(_db(), MONDAY)
        assert carried["KC"]["last_week"] == 10 and carried["KC"]["next_week"] == 11

    def test_team_still_to_play_this_week_is_not(self):
        # LAC plays Monday night: its status is about a game not yet played.
        assert "LAC" not in status_carryover.carried_teams(_db(), MONDAY)

    def test_first_practice_report_of_the_next_week_ends_it(self):
        db = _db(practice={11: {"KC": {"2026-11-18": {}}}})
        carried = status_carryover.carried_teams(db, MONDAY + timedelta(days=2))
        assert "KC" not in carried and "LV" in carried

    def test_last_weeks_report_days_do_not_end_it(self):
        db = _db(practice={11: {"KC": {"2026-11-13": {}}}})
        assert "KC" in status_carryover.carried_teams(db, MONDAY)

    def test_it_ends_72_hours_before_the_next_kickoff_regardless(self):
        thursday = datetime(2026, 11, 19, 18, 30, tzinfo=UTC)
        assert status_carryover.carried_teams(_db(), thursday) == {}

    def test_no_schedule_means_trust_the_status(self):
        assert status_carryover.carried_teams(FakeDb({}), MONDAY) == {}
        assert status_carryover.carried_teams(None, MONDAY) == {}


WINDOW = {"last_kickoff": "2026-11-15T18:00:00+00:00", "next_kickoff": "2026-11-22T18:00:00+00:00"}


class TestForwardStatus:
    def test_out_gives_way_to_the_reports_milder_forward_read(self):
        row = {"injury_status": "Out", "game_status": "Questionable", "return_date": "2026-11-22"}
        assert status_carryover.forward_status(row, WINDOW) == "Questionable"

    def test_out_on_both_fields_stays_out(self):
        row = {"injury_status": "Out", "game_status": "Out", "return_date": "2026-11-22"}
        assert status_carryover.forward_status(row, WINDOW) == "Out"

    def test_return_date_after_the_next_game_stays_out(self):
        row = {"injury_status": "Out", "game_status": "Questionable", "return_date": "2026-11-29"}
        assert status_carryover.forward_status(row, WINDOW) == "Out"

    @pytest.mark.parametrize("status,forward", [("Out", "PUP-R"), ("IR", "Questionable"),
                                                ("Out", "Injured Reserve")])
    def test_reserve_lists_are_never_softened(self, status, forward):
        row = {"injury_status": status, "game_status": forward}
        assert status_carryover.forward_status(row, WINDOW) == status

    def test_outside_the_window_nothing_changes(self):
        row = {"injury_status": "Out", "game_status": "Questionable"}
        assert status_carryover.forward_status(row, None) == "Out"

    def test_questionable_is_left_alone(self):
        row = {"injury_status": "Questionable", "game_status": "Questionable"}
        assert status_carryover.forward_status(row, WINDOW) == "Questionable"

    def test_changed_rows_are_copies_that_keep_the_original(self):
        rows = [{"team_id": "KC", "player_name": "Kenneth Walker III", "injury_status": "Out",
                 "game_status": "Questionable"}]
        out = status_carryover.forward_reports(rows, {"KC": WINDOW})
        assert out[0]["injury_status"] == "Questionable" and out[0]["carried_from"] == "Out"
        assert rows[0]["injury_status"] == "Out"


class TestResolveInjuryAcrossTheWeekBoundary:
    def _index(self, reports):
        return status_carryover.injury_index(_db(reports=reports), now=MONDAY)

    def test_walker_out_on_both_feeds_is_questionable_for_next_week(self):
        index = self._index([{"team_id": "KC", "player_name": "Kenneth Walker III",
                              "injury_status": "Out", "game_status": "Questionable",
                              "return_date": "2026-11-22", "injury_type": "Groin"}])
        got = resolve_injury(_athlete("Kenneth Walker III", "KC", "Out"), index, "KC")
        assert got["status"] == "Questionable"
        assert got["sleeper_status"] == "Out"           # what Sleeper sends, unchanged
        assert got["carried_over"]["from"] == "Out"

    def test_sleeper_in_game_out_defers_to_a_fresh_report(self):
        index = self._index([{"team_id": "LV", "player_name": "Brock Bowers",
                              "injury_status": "Questionable", "game_status": "Questionable"}])
        got = resolve_injury(_athlete("Brock Bowers", "LV", "Out"), index, "LV")
        assert got["status"] == "Questionable" and got["carried_over"]

    def test_sleeper_out_with_no_report_is_an_open_question_not_a_zero(self):
        got = resolve_injury(_athlete("Emmett Johnson", "KC", "Out"), self._index([]), "KC")
        assert got["status"] == status_carryover.CARRIED_STATUS

    def test_a_report_that_says_out_for_the_next_game_still_wins(self):
        index = self._index([{"team_id": "MIN", "player_name": "Jordan Addison",
                              "injury_status": "Out", "game_status": "Out",
                              "return_date": "2026-11-29"}])
        got = resolve_injury(_athlete("Jordan Addison", "MIN", "Out"), index, "MIN")
        assert got["status"] == "Out" and "carried_over" not in got

    def test_ir_stays_ir(self):
        got = resolve_injury(_athlete("A.J. Brown", "KC", "IR"), self._index([]), "KC")
        assert got["status"] == "IR"

    def test_team_yet_to_play_keeps_its_out(self):
        got = resolve_injury(_athlete("Ladd McConkey", "LAC", "Out"), self._index([]), "LAC")
        assert got["status"] == "Out" and "carried_over" not in got

    def test_a_plain_index_behaves_as_before(self):
        from nfl_mcp.injury_match import build_injury_index
        got = resolve_injury(_athlete("Kenneth Walker III", "KC", "Out"), build_injury_index([]), "KC")
        assert got["status"] == "Out"


# -- a week with nothing left to decide ---------------------------------------

def _game(done=True, started=True):
    kick = datetime.now(UTC) - (timedelta(hours=30) if done else timedelta(minutes=30))
    if not started:
        kick = datetime.now(UTC) + timedelta(days=2)
    return {"kickoff": kick.isoformat()}


ATHLETES = {"1": {"team_id": "CIN"}, "2": {"team_id": "DET"}, "3": {"team_id": "NE"},
            "8": {"team_id": "SF"}, "9": {"team_id": "IND"}}
MINE = {"players": ["1", "2", "3"], "reserve": [], "taxi": []}


class TestWeekDecided:
    def test_every_game_played_is_final_with_an_outcome(self):
        games = {t: _game() for t in ("CIN", "DET", "NE", "SF", "IND")}
        got = week_decided(MINE, ATHLETES, games, {"starters": ["1", "2"], "points": 152.06},
                           {"starters": ["8", "9"], "points": 99.96})
        assert got["decided"] and got["final"] and got["outcome"] == "won"
        assert "152.06 : 99.96" in got["summary"]

    def test_a_bench_player_still_to_kick_off_keeps_the_week_open(self):
        games = {"CIN": _game(), "DET": _game(), "NE": _game(started=False)}
        assert week_decided(MINE, ATHLETES, games, {"starters": ["1", "2"], "points": 50.0}, None) is None

    def test_a_player_on_bye_decides_nothing(self):
        games = {"CIN": _game(), "DET": _game(), "SF": _game(), "IND": _game()}  # NE: bye
        got = week_decided(MINE, ATHLETES, games, {"starters": ["1", "2"], "points": 80.0},
                           {"starters": ["8", "9"], "points": 90.0})
        assert got["outcome"] == "lost"

    def test_opponent_still_playing_is_leading_not_won(self):
        games = {"CIN": _game(), "DET": _game(), "NE": _game(), "SF": _game(done=False),
                 "IND": _game()}
        got = week_decided(MINE, ATHLETES, games, {"starters": ["1", "2"], "points": 120.0},
                           {"starters": ["8", "9"], "points": 100.0})
        assert got["final"] is False and got["outcome"] == "leading"

    def test_before_the_first_kickoff_nothing_is_decided(self):
        games = {t: _game(started=False) for t in ("CIN", "DET", "NE")}
        assert week_decided(MINE, ATHLETES, games, {"starters": ["1", "2"], "points": 0.0}, None) is None

    def test_cold_schedule_cache_decides_nothing(self):
        assert week_decided(MINE, ATHLETES, {}, {"starters": ["1"], "points": 10.0}, None) is None


class TestAllLockedLineup:
    @pytest.mark.asyncio
    async def test_a_fully_locked_roster_is_a_lineup_not_an_error(self):
        locked = [{"name": "A", "position": "QB", "team": "CIN", "projected_points": 32.2, "sd": 0.0,
                   "slot": "QB"},
                  {"name": "B", "position": "RB", "team": "DET", "projected_points": 40.7, "sd": 0.0,
                   "slot": "RB"}]
        opponent = [{"name": "X", "position": "QB", "team": "SF", "projected_points": 26.6, "sd": 0.0},
                    {"name": "Y", "position": "RB", "team": "IND", "projected_points": 11.3, "sd": 0.0}]
        got = await get_win_probability_lineup(
            your_players=[], opponent_players=opponent, slots={"QB": 1, "RB": 1},
            locked_players=locked, opponent_basis="set_lineup")
        assert got["success"] and got["win_probability"] == 100.0
        assert [s["player"] for s in got["recommended_lineup"]] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_no_players_at_all_is_still_an_error(self):
        got = await get_win_probability_lineup(your_players=[], opponent_players=None,
                                               slots={"QB": 1}, locked_players=[])
        assert not got["success"]


# -- the drop guard -----------------------------------------------------------

class TestCostlyDrops:
    def test_dropping_a_starter_for_a_bench_body_is_called_out(self):
        plan = simulate(_ctx(), _moves({"action": "claim", "player": "Jadarian Price",
                                        "drop": "Brock Bowers"}))
        costly = plan["costly_drops"]
        assert len(costly) == 1 and costly[0]["player"] == "Brock Bowers"
        assert costly[0]["net"] <= -DROP_GUARD_POINTS
        assert any("dropping Brock Bowers costs" in w for w in plan["warnings"])
        # ... and a cheaper drop is named.
        assert costly[0]["cheaper_drop"]["lineup_points_lost"] < costly[0]["lineup_points_lost"]

    def test_the_cost_is_reported_on_the_step_even_when_small(self):
        plan = simulate(_ctx(), _moves({"action": "claim", "player": "Keaton Mitchell",
                                        "drop": "Bryce Young"}))
        assert plan["costly_drops"] == []
        step = plan["steps"][0]
        assert step["drop_cost"]["lineup_points_lost"] >= 0
        assert step["drop_cost"]["net"] > -DROP_GUARD_POINTS

    def test_lineup_points_not_raw_totals(self):
        # The second QB projects 14 a game (140 "ROS points") and starts
        # nowhere but the first QB's bye: dropping him costs one week, not 140.
        plan = simulate(_ctx(), _moves({"action": "drop", "player": "Bryce Young"}))
        assert plan["steps"][0]["drop_cost"]["lineup_points_lost"] < 30

    def test_no_projections_no_guard(self):
        ctx = _ctx()
        ctx.ros = {}
        plan = simulate(ctx, _moves({"action": "drop", "player": "Brock Bowers"}))
        assert plan["costly_drops"] == []


# -- calibration --------------------------------------------------------------

class TestCalibrationRefit:
    def test_a_week_still_being_played_is_not_fitted(self):
        from evals.backtest.weekly_calibration import graded_weeks
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE player_week_stats (player_id TEXT, season INT, week INT, raw TEXT)")
        db.execute("CREATE TABLE schedule_games (season INT, week INT, team TEXT, kickoff TEXT)")
        for week, kickoff in ((9, "2026-11-10T01:15Z"), (10, "2026-11-17T01:15Z")):
            db.execute("INSERT INTO player_week_stats VALUES ('1', 2026, ?, '{}')", (week,))
            db.execute("INSERT INTO schedule_games VALUES (2026, ?, 'LAC', ?)", (week, kickoff))
        assert graded_weeks(db, 2026, now=MONDAY) == [9]
        assert graded_weeks(db, 2026, now=MONDAY + timedelta(days=1)) == [9, 10]

    def test_weeks_without_a_cached_schedule_count(self):
        from evals.backtest.weekly_calibration import graded_weeks
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE player_week_stats (player_id TEXT, season INT, week INT, raw TEXT)")
        db.execute("INSERT INTO player_week_stats VALUES ('1', 2026, 3, '{}')")
        assert graded_weeks(db, 2026, now=MONDAY) == [3]

    def test_summary_says_when_a_refit_is_due(self, monkeypatch):
        monkeypatch.setitem(calibration.CALIBRATION, "weeks", [1, 2, 3, 4, 5, 6, 7])
        monkeypatch.setitem(calibration.CALIBRATION, "season", 2026)
        assert "refit_due" not in calibration.summary(8, 2026)
        due = calibration.summary(10, 2026)
        assert due["weeks_behind"] == 3 and "task calibrate" in due["refit_due"]
        assert "weeks_behind" not in calibration.summary()
        assert "weeks_behind" not in calibration.summary(10, 2025)


# -- the signal review --------------------------------------------------------

def _row(week, flags, projected=10.0, actual=11.0, pid="1"):
    return {"week": week, "player_id": pid, "projected": projected, "actual": actual,
            "news_flags": json.dumps(flags), "signals": json.dumps({"news_model_mult": 0.93}),
            "position": "WR", "projection_source": "sleeper_blend"}


class TestNewsFlagOwnWeight:
    def _specs(self):
        rows = [_row(9, ["expected_to_play", "committee"])]
        return {s["name"]: s for s in signal_review._catalogue(rows)}

    def test_a_flag_without_a_multiplier_reports_none(self):
        spec = self._specs()["news:expected_to_play"]
        assert spec["current"] is None and "no multiplier" in spec["param"]

    def test_a_priced_flag_reports_its_own_weight_not_the_rows_mean(self):
        from nfl_mcp.news_signals import EFFECTS
        spec = self._specs()["news:committee"]
        assert spec["current"]([]) == EFFECTS["committee"]["model_mult"]
