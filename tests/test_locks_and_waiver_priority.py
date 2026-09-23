"""Kickoff locks outside the briefing, and waiver-priority strategy.

Times are pinned to week 3 of 2026: TNF Friday 00:15 UTC, Sunday early slate
17:00 UTC, waivers on Wednesday (run ~07:00 UTC, midnight US Pacific).
"""
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from nfl_mcp import game_clock, lineup_optimizer_tools, win_probability
from nfl_mcp.database import NFLDatabase
from nfl_mcp.game_clock import game_lock, game_progress, week_games
from nfl_mcp.waiver_rules import (
    ADD_NOW,
    CLAIM_NOW,
    DONT_BOTHER,
    WAIT,
    latest_drops,
    next_run,
    priority_strategy,
    trend_demand,
    waiver_status,
)

TNF = datetime(2026, 9, 25, 0, 15, tzinfo=UTC)          # Thu night US, Fri 02:15 Vienna
SUNDAY = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
LAST_SUNDAY = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
WED_RUN = datetime(2026, 9, 23, 7, 0, tzinfo=UTC)


def _league(clear_days=2, daily=0, teams=10, waiver_type=0, budget=100):
    return {"total_rosters": teams, "settings": {
        "waiver_type": waiver_type, "waiver_budget": budget, "waiver_day_of_week": 2,
        "waiver_clear_days": clear_days, "daily_waivers": daily, "daily_waivers_hour": 0,
    }}


def _roster(position):
    return {"roster_id": 1, "settings": {"waiver_position": position}}


class TestGameClock:
    def test_espn_final_beats_the_clock(self):
        # 2h into the game by the clock, but ESPN says it is over.
        now = SUNDAY + timedelta(hours=2)
        assert game_progress(SUNDAY.isoformat(), now) < 1.0
        assert game_progress(SUNDAY.isoformat(), now, "post", True) == 1.0

    def test_overtime_in_progress_is_not_final(self):
        now = SUNDAY + timedelta(hours=3, minutes=40)
        assert game_progress(SUNDAY.isoformat(), now) == 1.0
        assert 0 < game_progress(SUNDAY.isoformat(), now, "in", False) < 1.0

    def test_a_stale_in_progress_state_falls_back_to_the_clock(self):
        now = SUNDAY + timedelta(hours=8)
        assert game_progress(SUNDAY.isoformat(), now, "in", False) == 1.0

    def test_a_stale_pre_state_never_unlocks_a_started_game(self):
        now = SUNDAY + timedelta(minutes=5)
        assert game_progress(SUNDAY.isoformat(), now, "pre", False) > 0.0

    def test_lock_fields_are_utc_and_vienna(self):
        lock = game_lock({"kickoff": "2026-09-25T00:15Z"}, TNF - timedelta(hours=1))
        assert lock["kickoff"] == "2026-09-25T00:15:00Z"
        assert lock["kickoff_local"] == "2026-09-25T02:15:00+02:00"
        assert lock["kickoff_weekday"] == "Fri"
        assert lock["locked"] is False and lock["game_status"] == "upcoming"
        started = game_lock({"kickoff": "2026-09-25T00:15Z"}, TNF + timedelta(minutes=1))
        assert started["locked"] is True and started["game_status"] == "in_progress"

    def test_unknown_game_is_not_locked(self):
        assert game_lock(None)["locked"] is False

    def test_week_games_reads_the_stored_espn_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = NFLDatabase(str(Path(tmp) / "t.db"))
            db.upsert_schedule_games([
                {"season": 2026, "week": 3, "team": "WAS", "opponent": "DAL", "is_home": 1,
                 "kickoff": "2026-09-27T17:00Z",
                 "raw": {"status": {"type": {"state": "post", "completed": True}}}},
                {"season": 2026, "week": 3, "team": "DAL", "opponent": "WAS", "is_home": 0,
                 "kickoff": "2026-09-27T17:00Z",
                 "raw": {"competitions": [{"status": {"type": {"state": "in", "completed": False}}}]}},
            ])
            games = week_games(db, 2026, 3)
        assert games["WSH"]["completed"] is True       # Sleeper WAS, canonical WSH
        assert games["DAL"]["state"] == "in"


class TestWaiverTiming:
    def test_runs_are_midnight_pacific(self):
        monday = datetime(2026, 9, 21, 12, tzinfo=UTC)
        assert next_run(_league(), monday) == datetime(2026, 9, 22, 7, tzinfo=UTC)
        assert next_run(_league(), monday, weekly=True) == WED_RUN

    def test_last_weeks_game_locks_him_until_waiver_day(self):
        monday = datetime(2026, 9, 21, 12, tzinfo=UTC)
        status = waiver_status(_league(), kickoff=SUNDAY, previous_kickoff=LAST_SUNDAY,
                               now=monday)
        assert status["on_waivers"] is True and status["clears_at"] == WED_RUN.isoformat()
        assert status["in_time_for_kickoff"] is True

    def test_after_the_run_he_is_an_instant_add(self):
        status = waiver_status(_league(), kickoff=SUNDAY, previous_kickoff=LAST_SUNDAY,
                               now=WED_RUN + timedelta(hours=1))
        assert status["instant_add"] is True and status["on_waivers"] is False

    def test_clear_days_count_pacific_calendar_days(self):
        # Observed: dropped Wed 07:34 UTC with 2 clear days, cleared in Friday's run.
        drop = datetime(2026, 9, 16, 7, 34, tzinfo=UTC)
        status = waiver_status(_league(), kickoff=SUNDAY, dropped_at=drop,
                               now=drop + timedelta(minutes=5))
        assert status["clears_at"] == datetime(2026, 9, 18, 7, tzinfo=UTC).isoformat()

    def test_thursday_game_with_two_clear_days_is_too_late(self):
        drop = datetime(2026, 9, 23, 16, tzinfo=UTC)          # Wednesday afternoon
        tnf = datetime(2026, 9, 25, 0, 15, tzinfo=UTC)
        two = waiver_status(_league(clear_days=2), kickoff=tnf, dropped_at=drop,
                            now=drop + timedelta(minutes=1))
        one = waiver_status(_league(clear_days=1), kickoff=tnf, dropped_at=drop,
                            now=drop + timedelta(minutes=1))
        assert two["in_time_for_kickoff"] is False
        assert one["in_time_for_kickoff"] is True          # Thursday's run, before TNF

    def test_no_kickoffs_is_unknown_not_free_agent(self):
        status = waiver_status(_league(), now=WED_RUN)
        assert status["on_waivers"] is None and status["instant_add"] is False

    def test_latest_drops_uses_completed_moves_only(self):
        ms = int(WED_RUN.timestamp() * 1000)
        drops = latest_drops([
            {"status": "complete", "status_updated": ms, "drops": {"p1": 3}},
            {"status": "failed", "status_updated": ms + 5000, "drops": {"p2": 3}},
        ])
        assert set(drops) == {"p1"} and drops["p1"] == WED_RUN


class TestPriorityStrategy:
    MONDAY = datetime(2026, 9, 21, 12, tzinfo=UTC)

    def _run(self, **kw):
        args = {"kickoff": SUNDAY, "previous_kickoff": LAST_SUNDAY, "now": self.MONDAY,
                "worth": "medium", "demand": "low", "position": 3, "league": _league()}
        args.update(kw)
        return priority_strategy(args.pop("league"), _roster(args.pop("position")), **args)

    def test_uncontested_medium_waits_for_free_agency(self):
        out = self._run()
        assert out["recommendation"] == WAIT and out["wait_days"] == 2
        assert out["teams_ahead"] == 2 and out["claim_cost_places"] == 7

    def test_contested_medium_claims_now(self):
        assert self._run(demand="high")["recommendation"] == CLAIM_NOW

    def test_high_worth_claims_now(self):
        assert self._run(worth="high")["recommendation"] == CLAIM_NOW

    def test_last_in_line_claims_for_free(self):
        out = self._run(position=10)
        assert out["recommendation"] == CLAIM_NOW and out["claim_cost_places"] == 0

    def test_free_agent_is_added_without_priority(self):
        out = self._run(now=WED_RUN + timedelta(hours=2))
        assert out["recommendation"] == ADD_NOW

    def test_game_started_is_too_late_this_week(self):
        out = self._run(worth="high", now=SUNDAY + timedelta(minutes=10))
        assert out["recommendation"] == DONT_BOTHER and "already started" in out["reason"]

    def test_rest_of_season_survives_a_started_game(self):
        out = self._run(worth="high", now=SUNDAY + timedelta(minutes=10), this_week=False)
        assert out["recommendation"] == CLAIM_NOW

    def test_low_worth_contested_is_not_worth_it(self):
        assert self._run(worth="low", demand="high")["recommendation"] == DONT_BOTHER

    def test_faab_league_has_no_priority_strategy(self):
        assert self._run(league=_league(waiver_type=2)) is None

    def test_trend_demand_cutoffs(self):
        assert [trend_demand(r) for r in (0, 15, 50, None)] == [
            "high", "moderate", "light", "low"]


# ---------------------------------------------------------------------------
# Lineup tools
# ---------------------------------------------------------------------------

class _FakeDB:
    """Kickoffs only: BUF played Thursday (started), KC plays Sunday."""

    def get_week_game_states(self, season, week):
        return {"BUF": {"kickoff": TNF.isoformat(), "state": "in", "completed": False},
                "KC": {"kickoff": SUNDAY.isoformat(), "state": "pre", "completed": False}}

    def get_week_opponents(self, season, week):
        return {}

    def get_week_kickoffs(self, season, week):
        return {}


@pytest.fixture
def optimizer():
    opt = lineup_optimizer_tools.LineupOptimizer(db=_FakeDB(), auto_project=False)
    opt.defense_analyzer = None
    now = TNF + timedelta(hours=1)
    with patch.object(lineup_optimizer_tools, "get_lineup_optimizer", lambda: opt), \
         patch.object(lineup_optimizer_tools, "_now", lambda: now), \
         patch("nfl_mcp.injury_match.lookup_injury", lambda *a, **k: None):
        yield opt


def _p(name, team, pts, **extra):
    return {"name": name, "position": "WR", "team": team, "opponent": "X",
            "projection": {"projected_points": pts, "floor": pts - 4, "ceiling": pts + 4},
            **extra}


class TestLineupLocks:
    async def test_start_sit_reports_the_lock(self, optimizer):
        res = await lineup_optimizer_tools.get_start_sit_recommendation(
            player_name="Thursday WR", position="WR", team="BUF", opponent="MIA",
            projected_points=15.0, scoring="ppr", season=2026, week=3)
        rec = res["recommendation"]
        assert rec["locked"] is True and rec["kickoff_local"] == "2026-09-25T02:15:00+02:00"
        assert "locked" in res["message"]

    async def test_compare_picks_among_movable_players(self, optimizer):
        res = await lineup_optimizer_tools.compare_players_for_slot(
            players=[_p("Thursday WR", "BUF", 20.0), _p("Sunday WR", "KC", 12.0)],
            slot="FLEX", scoring="ppr", season=2026, week=3)
        assert res["winner"]["player"] == "Sunday WR"
        assert "locked" in res["verdict"]

    async def test_compare_with_a_locked_starter_changes_nothing(self, optimizer):
        res = await lineup_optimizer_tools.compare_players_for_slot(
            players=[_p("Thursday WR", "BUF", 8.0, starting=True), _p("Sunday WR", "KC", 20.0)],
            slot="FLEX", scoring="ppr", season=2026, week=3)
        assert res["winner"]["player"] == "Thursday WR"
        assert res["verdict"].startswith("Slot locked")

    async def test_full_lineup_never_moves_a_locked_player(self, optimizer):
        res = await lineup_optimizer_tools.analyze_full_lineup(
            lineup={"WR": [_p("Thursday WR", "BUF", 5.0)],
                    "BENCH": [_p("Sunday WR", "KC", 20.0), _p("Thursday Bench", "BUF", 30.0)]},
            scoring="ppr", season=2026, week=3)
        assert res["suggested_changes"] == []
        assert {p["player"] for p in res["locked_players"]} == {"Thursday WR", "Thursday Bench"}

    async def test_roster_recommendations_list_locked_players(self, optimizer):
        res = await lineup_optimizer_tools.get_roster_recommendations(
            players=[_p("Thursday WR", "BUF", 5.0), _p("Sunday WR", "KC", 20.0)],
            scoring="ppr", season=2026, week=3)
        assert res["locked"] == ["Thursday WR (WR)"]
        assert all("kickoff" in r for r in res["recommendations"])


class TestWinProbabilityLocks:
    async def test_started_players_are_locked_or_out(self):
        now = TNF + timedelta(hours=1)
        with patch.object(win_probability, "_now", lambda: now), \
             patch("nfl_mcp.database.get_shared_db", lambda *a, **k: _FakeDB()):
            res = await win_probability.get_win_probability_lineup(
                your_players=[
                    {"name": "Thursday Starter", "position": "WR", "team": "BUF",
                     "projected_points": 5, "sd": 0, "slot": "WR"},
                    {"name": "Thursday Bench", "position": "WR", "team": "BUF",
                     "projected_points": 30, "sd": 3, "slot": "BN"},
                    {"name": "Sunday WR", "position": "WR", "team": "KC",
                     "projected_points": 12, "sd": 4},
                ],
                opponent_players=[{"name": "Opp", "position": "WR", "projected_points": 15,
                                   "sd": 4}],
                slots={"WR": 2}, season=2026, week=3,
            )
        assert res["success"] is True
        names = {r["player"]: r for r in res["recommended_lineup"]}
        assert set(names) == {"Thursday Starter", "Sunday WR"}
        assert names["Thursday Starter"]["locked"] is True
        assert names["Sunday WR"]["kickoff"] == "2026-09-27T17:00:00Z"
        assert [u["player"] for u in res["unavailable_started"]] == ["Thursday Bench"]


def test_game_clock_local_zone_is_vienna():
    assert str(game_clock.LOCAL_TZ) == "Europe/Vienna"
