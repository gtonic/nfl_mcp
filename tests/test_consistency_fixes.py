"""Consistency fixes: one roster loader / roster finder, usage inputs for
start/sit, occupied-but-unknown lineup seats, and assorted review follow-ups."""
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp import lineup_tools, roster_context, sleeper_tools
from nfl_mcp.errors import ErrorType

pytestmark = pytest.mark.usefixtures("offline_sources")


def test_every_referenced_error_type_exists():
    import pathlib
    import re
    used = set()
    for path in pathlib.Path("nfl_mcp").glob("*.py"):
        used |= set(re.findall(r"ErrorType\.([A-Z_]+)", path.read_text()))
    missing = {name for name in used if not hasattr(ErrorType, name)}
    assert not missing


class TestLoadRosters:
    @pytest.mark.asyncio
    async def test_fresh_rosters_are_usable_for_both(self):
        fetch = AsyncMock(return_value={"success": True, "rosters": [{"roster_id": 1}]})
        for purpose in ("availability", "lineup"):
            state = await sleeper_tools.load_rosters("L", purpose, fetch=fetch)
            assert state["blocking_error"] is None and state["rosters"] == [{"roster_id": 1}]
            assert state["stale"] is False

    @pytest.mark.asyncio
    async def test_old_snapshot_blocks_availability_but_not_lineup(self):
        snap = {"success": False, "stale": True, "snapshot_age_seconds": 7200,
                "error": "timeout", "rosters": [{"roster_id": 1}]}
        fetch = AsyncMock(return_value=snap)
        avail = await sleeper_tools.load_rosters("L", "availability", fetch=fetch)
        assert avail["blocking_error"]
        lineup = await sleeper_tools.load_rosters("L", "lineup", fetch=fetch)
        assert lineup["blocking_error"] is None
        assert lineup["stale"] is True and lineup["warning"]

    @pytest.mark.asyncio
    async def test_nothing_at_all_blocks_both(self):
        fetch = AsyncMock(side_effect=RuntimeError("down"))
        state = await sleeper_tools.load_rosters("L", "lineup", fetch=fetch)
        assert state["blocking_error"] and state["rosters"] == []

    @pytest.mark.asyncio
    async def test_unknown_purpose_is_rejected(self):
        with pytest.raises(ValueError):
            await sleeper_tools.load_rosters("L", "vibes", fetch=AsyncMock())


class TestFindRoster:
    ROSTERS = [{"roster_id": 1, "owner_id": "a"},
               {"roster_id": 2, "owner_id": "b", "co_owners": ["c"]}]

    def test_by_id_owner_and_co_owner(self):
        assert sleeper_tools.find_roster(self.ROSTERS, "L", 1, None)[0]["roster_id"] == 1
        assert sleeper_tools.find_roster(self.ROSTERS, "L", "2", None)[0]["roster_id"] == 2
        assert sleeper_tools.find_roster(self.ROSTERS, "L", None, "b")[0]["roster_id"] == 2
        assert sleeper_tools.find_roster(self.ROSTERS, "L", None, "c")[0]["roster_id"] == 2

    def test_errors(self):
        mine, err = sleeper_tools.find_roster(self.ROSTERS, "L", None, None, purpose="advise")
        assert mine is None and "which team to advise" in err
        mine, err = sleeper_tools.find_roster(self.ROSTERS, "L", 9, None)
        assert mine is None and "No roster found" in err

    def test_briefing_re_exports_the_same_function(self):
        from nfl_mcp import briefing_tools
        assert briefing_tools.find_roster is sleeper_tools.find_roster


class _UsageDB:
    """player_usage_stats for a WR on KC and his teammates, weeks 1-3."""
    WEEKS = [  # most recent first, as get_usage_weekly_breakdown returns them
        {"week": 3, "targets": 12, "routes": 35, "rz_touches": 3, "snap_share": 90, "touches": 9},
        {"week": 2, "targets": 8, "routes": 33, "rz_touches": 1, "snap_share": 88, "touches": 6},
        {"week": 1, "targets": 7, "routes": 30, "rz_touches": 2, "snap_share": 85, "touches": 5},
    ]

    def get_usage_weekly_breakdown(self, pid, season, week, n=3):
        return self.WEEKS if pid == "wr1" else []

    def get_usage_for_week(self, season, week):
        own = next(r for r in self.WEEKS if r["week"] == week)
        return [{"player_id": "wr1", "targets": own["targets"], "snap_share": own["snap_share"]},
                {"player_id": "te1", "targets": 40 - own["targets"]},
                {"player_id": "other", "targets": 30}]

    def get_athletes_by_ids(self, ids):
        teams = {"wr1": "KC", "te1": "KC", "other": "BUF"}
        return {i: {"team_id": teams.get(i), "full_name": i, "position": "WR"} for i in ids}


class TestRecentUsage:
    def test_fills_target_share_red_zone_and_trend(self):
        lineup_tools._team_targets_cache.clear()
        out = lineup_tools.recent_usage(_UsageDB(), "wr1", "WR", "KC", 2026, 4)
        assert out["target_share"] == round(100 * 27 / 120, 1)
        assert out["red_zone_opportunities"] == 2
        assert out["usage_trend"] == "upward"

    def test_nothing_for_qb_or_missing_data(self):
        assert lineup_tools.recent_usage(_UsageDB(), "wr1", "QB", "KC", 2026, 4) == {}
        assert lineup_tools.recent_usage(_UsageDB(), "nobody", "WR", "KC", 2026, 4) == {}
        assert lineup_tools.recent_usage(None, "wr1", "WR", "KC", 2026, 4) == {}

    def test_player_input_carries_it_without_overriding_the_caller(self):
        lineup_tools._team_targets_cache.clear()
        item = {"name": "wr1", "position": "WR", "team": "KC", "player_id": "wr1",
                "usage": {"target_share": 5.0}}
        out = lineup_tools.player_input(_UsageDB(), item, 2026, 4)
        assert out["usage"]["target_share"] == 5.0
        assert out["usage"]["usage_trend"] == "upward"
        assert out["usage"]["red_zone_opportunities"] == 2


class TestOccupiedUnknownSeat:
    @pytest.mark.asyncio
    async def test_unplaceable_starter_is_not_an_empty_seat(self, monkeypatch):
        players = [{"player_id": "q", "name": "QB A", "position": "QB", "team": "KC",
                    "opponent": "LV"}]
        ctx = {"league": {"roster_positions": ["QB", "RB", "BN"]},
               "roster": {"reserve": ["ir1"]}, "roster_id": 1, "season": 2026, "week": 3,
               "players": players, "starters": ["q", "9999"], "starters_raw": ["q", "9999"],
               "error": None, "stale": True, "snapshot_age_seconds": 600,
               "roster_warning": "Rosters are a cached snapshot"}
        monkeypatch.setattr(roster_context, "load_roster_players", AsyncMock(return_value=ctx))
        monkeypatch.setattr(sleeper_tools, "get_matchups", AsyncMock(return_value={"matchups": []}))
        captured = {}

        async def _full(**kwargs):
            captured.update(kwargs)
            return {"success": True}
        monkeypatch.setattr(lo, "analyze_full_lineup", _full)

        class _DB:
            def get_athletes_by_ids(self, ids):
                return {"ir1": {"full_name": "Hurt Guy", "position": "RB"}} if "ir1" in ids else {}

            def get_usage_for_week(self, *a):
                return []
        out = await lineup_tools.analyze_lineup(league_id="L", roster_id=1, db=_DB())
        assert captured["empty_slots"] == []
        assert out["occupied_unknown_slots"] == [{"slot": "RB", "player_id": "9999"}]
        assert out["reserve"] == [{"player_id": "ir1", "name": "Hurt Guy", "position": "RB"}]
        assert out["stale"] is True
        assert any("cached snapshot" in w for w in out["warnings"])


class TestStartSitWording:
    def test_a_qb_is_never_flex(self):
        opt = lo.LineupOptimizer(db=None, auto_project=False, defense_analyzer=None)
        adequate, _ = lo._good_game_thresholds("QB", 1.0, 1.0)
        assert opt.determine_decision(adequate * 0.8, "QB", 100) == "start"
        assert opt.determine_decision(0, "QB", 100) == "sit"
        assert opt.determine_decision(adequate * 0.8, "WR", 100) in ("flex", "start")

    def test_dnp_is_not_called_limited(self):
        opt = lo.LineupOptimizer(db=None, auto_project=False, defense_analyzer=None)
        a = lo.PlayerAnalysis(player_name="X", player_id="x", position="WR", team="KC", opponent="LV")
        a.practice_status = "DNP"
        _, _, reasons = opt.calculate_confidence(a)
        assert any("Did not practice: DNP" in r for r in reasons)
        assert not any("Limited practice: DNP" in r for r in reasons)


class TestPlayoffMedian:
    @pytest.mark.asyncio
    async def test_median_league_ppg_is_points_over_weeks(self):
        from nfl_mcp import playoff_tools as pt
        from tests.test_lineup_league_fixes import _pairs, _pt_patches
        rosters = [{"roster_id": i, "owner_id": f"u{i}",
                    "settings": {"wins": 12, "losses": 12, "ties": 0, "fpts": 1200}}
                   for i in range(1, 5)]
        with _pt_patches(rosters, lambda w: _pairs([(1, 2), (3, 4)]),
                         league_settings={"league_average_match": 1}):
            out = await pt.get_playoff_odds("L", num_sims=200, seed=1, my_roster_id=1)
        assert {o["actual_ppg"] for o in out["odds"]} == {100.0}
        swing = out["this_week_swing"]
        assert swing["if_win_pct"] >= swing["if_lose_pct"]

    def test_forced_winner_keeps_both_in_the_median_pool(self):
        import random

        from nfl_mcp import playoff_tools as pt
        teams = [{"roster_id": i, "wins": 0, "points": 0, "mean": m}
                 for i, m in ((1, 100), (2, 200), (3, 50), (4, 10))]
        out = pt._simulate(teams, [(1, 2), (3, 4)], 2, 200, 1.0, random.Random(1),
                           median_weeks=[5, 5], forced={0: 1})
        # 1 is pinned to beat 2 and takes the higher score of the pair: both
        # still sit in the median pool, so 1 (2 wins) and 2 (1 win, more
        # points than 3) make it.
        assert out[1]["playoff_pct"] == 100.0 and out[2]["playoff_pct"] == 100.0


class TestStreamingLeagueSlots:
    @pytest.mark.asyncio
    async def test_old_snapshot_is_not_used_for_availability(self, monkeypatch):
        from nfl_mcp import streaming_tools
        monkeypatch.setattr(sleeper_tools, "get_rosters", AsyncMock(return_value={
            "success": False, "stale": True, "snapshot_age_seconds": 90000,
            "rosters": [{"roster_id": 1, "players": ["BUF"]}]}))
        rostered, ok, why = await streaming_tools._rostered_ids("L")
        assert ok is False and why and rostered == set()

    @pytest.mark.asyncio
    async def test_reserve_and_taxi_count_as_rostered(self, monkeypatch):
        from nfl_mcp import streaming_tools
        monkeypatch.setattr(sleeper_tools, "get_rosters", AsyncMock(return_value={
            "success": True, "rosters": [{"roster_id": 1, "players": ["a"],
                                          "reserve": ["b"], "taxi": ["c"]}]}))
        rostered, ok, _ = await streaming_tools._rostered_ids("L")
        assert ok and rostered == {"a", "b", "c"}

    def test_league_starts(self):
        from nfl_mcp.lineup_slots import league_starts
        slots = ["QB", "RB", "WR", "FLEX", "DEF", "BN"]
        assert not league_starts(slots, "K")
        assert league_starts(slots, "DST") and league_starts(slots, "WR")
        assert league_starts(None, "K")


class TestTradeHeadline:
    def test_lineup_call_leads(self):
        from nfl_mcp.trade_analyzer_tools import _lineup_call
        both = {"team1": {"ros_points_delta": 20.0}, "team2": {"ros_points_delta": 15.0},
                "weeks": [5, 6]}
        assert _lineup_call(both) == "both_lineups_improve"
        assert _lineup_call(None) is None

    def test_needs_only_for_positions_the_league_starts(self):
        from nfl_mcp.trade_analyzer_tools import TradeAnalyzer
        needs = TradeAnalyzer()._calculate_positional_needs(
            {"players_enriched": []}, ["QB", "RB", "WR", "TE", "FLEX", "BN"])
        assert "K" not in needs and "DEF" not in needs and "QB" in needs


def test_bye_suggestion_does_not_call_covered_qb_thin():
    from nfl_mcp.bye_week_tools import _suggestion
    row = {"week": 7, "on_bye": [{"player": "QB One", "role": "starter", "position": "QB"}],
           "available_by_position": {"QB": 1}, "holes": [], "projected_total": 100,
           "full_strength_total": 115, "bye_cost": 15}
    text = _suggestion(row, ["QB", "RB"], ["QB"])
    assert "depth is thin" not in text and "covered by 1 other QB" in text


class TestCircuitBreakerProbe:
    @pytest.mark.asyncio
    async def test_cancel_during_backoff_releases_the_probe(self, monkeypatch):
        import asyncio

        import httpx

        from nfl_mcp import retry_utils as ru
        breaker = ru.get_circuit_breaker("test_cancel_probe")
        breaker.reset()
        breaker.state = ru.CircuitState.OPEN
        breaker.last_failure_time = 0  # timeout long past -> next call is the probe

        async def _flaky():
            raise httpx.ConnectError("down")

        task = asyncio.create_task(ru.retry_with_backoff(
            _flaky, max_retries=3, initial_delay=10, circuit_breaker_name="test_cancel_probe"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert breaker.state == ru.CircuitState.HALF_OPEN
        assert breaker._probe_in_flight is False
        assert breaker.allow_request() is True

    def test_a_lost_probe_times_out(self, monkeypatch):
        from nfl_mcp import retry_utils as ru
        breaker = ru.CircuitBreaker("lost_probe")
        breaker.state = ru.CircuitState.HALF_OPEN
        breaker._probe_in_flight = True
        breaker._probe_started = 0.0
        assert breaker.allow_request() is True
        assert breaker.allow_request() is False  # the new probe is in flight

    def test_json_decode_error_is_retryable(self):
        import json

        from nfl_mcp import retry_utils as ru
        assert ru.is_retryable_error(json.JSONDecodeError("x", "<html>", 0))
        assert not ru.is_retryable_error(ValueError("bad input"))


class TestSharedDb:
    def test_injected_instance_wins_over_env_path(self, monkeypatch, tmp_path):
        from nfl_mcp import database
        injected = database.NFLDatabase(str(tmp_path / "injected.db"))
        database.set_shared_db(injected)
        try:
            monkeypatch.setenv("NFL_MCP_DB_PATH", str(tmp_path / "other.db"))
            assert database.get_shared_db() is injected
        finally:
            database.reset_shared_db()

    def test_rebuild_closes_the_old_instance(self, monkeypatch, tmp_path):
        from nfl_mcp import database
        database.reset_shared_db()
        monkeypatch.setenv("NFL_MCP_DB_PATH", str(tmp_path / "a.db"))
        first = database.get_shared_db()
        closed = []
        monkeypatch.setattr(first, "close", lambda: closed.append(True))
        monkeypatch.setenv("NFL_MCP_DB_PATH", str(tmp_path / "b.db"))
        second = database.get_shared_db()
        assert second is not first and closed == [True]
        database.reset_shared_db()


def test_advanced_enrich_is_read_at_call_time(monkeypatch):
    from nfl_mcp import sleeper_enrichment
    monkeypatch.setattr(sleeper_enrichment, "ADVANCED_ENRICH_ENABLED", False)
    monkeypatch.setenv("NFL_MCP_ADVANCED_ENRICH", "1")
    assert sleeper_enrichment.advanced_enrich_enabled() is True
    import pathlib
    src = pathlib.Path("nfl_mcp/nfl_tools.py").read_text()
    assert "import ADVANCED_ENRICH_ENABLED" not in src


class TestFlexSeatsByPoints:
    def _rb(self, name, pts):
        return {"name": name, "position": "RB", "projected_points": pts}

    def test_flex_rb3_is_a_starter_not_surplus(self):
        from nfl_mcp.roster_needs import (
            lineup_slots,
            replacement_levels,
            slot_counts,
            surplus_players,
        )
        positions = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "BN"]
        roster = [self._rb("RB1", 20), self._rb("RB2", 15), self._rb("RB3", 12),
                  {"name": "WR1", "position": "WR", "projected_points": 14},
                  {"name": "WR2", "position": "WR", "projected_points": 9},
                  {"name": "WR3", "position": "WR", "projected_points": 5},
                  {"name": "WR4", "position": "WR", "projected_points": 3},
                  {"name": "TE1", "position": "TE", "projected_points": 8},
                  {"name": "QB1", "position": "QB", "projected_points": 20}]
        frac, whole = slot_counts(positions), lineup_slots(positions)
        # Old behaviour (fractional floor): RB3 is surplus.
        assert "RB3" in {p["name"] for p in surplus_players(list(roster), frac)}
        surplus = {p["name"] for p in surplus_players(list(roster), frac, whole_slots=whole)}
        assert surplus == {"WR4"}  # RB3 and WR3 hold the two FLEX seats
        levels = replacement_levels(list(roster), frac, whole)
        assert levels["RB"] == 12 and levels["WR"] == 5

    def test_position_with_nobody_keeps_a_zero_bar(self):
        from nfl_mcp.roster_needs import lineup_slots, replacement_levels, slot_counts
        positions = ["QB", "RB", "TE", "BN"]
        levels = replacement_levels([{"name": "R", "position": "RB", "projected_points": 5}],
                                    slot_counts(positions), lineup_slots(positions))
        assert levels == {"RB": 5}


class TestTradeFinderDepth:
    def test_no_fourth_qb_in_one_qb_league(self):
        from nfl_mcp.trade_finder_tools import _over_depth
        mine = [{"position": "QB"}] * 3 + [{"position": "WR"}] * 5
        slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}
        assert _over_depth(mine, {"position": "WR"}, {"position": "QB"}, slots)
        assert not _over_depth(mine, {"position": "QB"}, {"position": "QB"}, slots)
        assert not _over_depth(mine[:2], {"position": "WR"}, {"position": "QB"}, slots)
        # Superflex: a QB fits two seats.
        assert not _over_depth(mine, {"position": "WR"}, {"position": "QB"},
                               {**slots, "SUPER_FLEX": 1})


class TestFaabConsistency:
    @pytest.mark.asyncio
    async def test_non_faab_has_no_bid_and_gain_text_follows_horizons(self, monkeypatch):
        from nfl_mcp import faab_tools as ft
        from nfl_mcp import ros
        from tests.test_waiver_horizons import _ros_mock, _Values
        monkeypatch.setattr(ros, "ros_for_ids", _ros_mock(12.0))

        async def _league(_):
            return {"success": True, "league": {
                "scoring_settings": {"rec": 0.5}, "total_rosters": 12,
                "roster_positions": ["QB", "RB", "RB", "WR", "WR", "BN", "BN"],
                "settings": {"type": 0, "waiver_type": 0}}}

        async def _rosters(_):
            return {"success": True, "rosters": [
                {"roster_id": 7, "players": ["m1", "m2", "m3"], "players_enriched": []}]}

        async def _state():
            return {"success": True, "nfl_state": {"week": 3, "season": 2026}}

        async def _empty(*a, **k):
            return {"success": True, "trending_players": [], "transactions": []}

        monkeypatch.setattr(ft, "get_league", _league)
        monkeypatch.setattr(ft, "get_rosters", _rosters)
        monkeypatch.setattr(ft, "get_nfl_state", _state)
        monkeypatch.setattr(ft, "get_trending_players", _empty)
        monkeypatch.setattr(ft, "get_transactions", _empty)
        monkeypatch.setattr(ft, "get_values_service", lambda *_a, **_k: _Values())

        class _DB:
            pass

        res = await ft.recommend_faab_bid("L", player_id="f1", my_roster_id=7, db=_DB())
        rec = res["recommendation"]
        assert rec["bid_pct"] is None and rec["range_pct"] is None
        assert rec["range_absolute"] is None
        assert rec["upgrade_basis"] == "lineup_points"
        assert rec["horizons"]["ros_gain"] > 0
        assert not any("not an upgrade" in w for w in rec["warnings"])
        assert any("Lineup gain for you" in r for r in rec["reasoning"])
        assert res["message"].startswith("Non-FAAB league")


class TestWaiverWording:
    def test_worth_text_omits_missing_value(self):
        from nfl_mcp.waiver_target_tools import _worth_text
        assert _worth_text({"value": None, "projected_points": 6.0}, points=True) == \
            "no market value, 6.0 pts"
        assert _worth_text({"value": 812.4}) == "value 812"

    def test_free_agent_claim_is_instant_and_costs_nothing(self):
        from datetime import UTC, datetime, timedelta

        from nfl_mcp.waiver_rules import priority_strategy, waiver_status
        now = datetime(2026, 9, 24, 12, tzinfo=UTC)
        league = {"settings": {"waiver_type": 0}, "total_rosters": 12}
        status = waiver_status(league, kickoff=now + timedelta(days=3), now=now)
        assert status["instant_add"] is True
        assert status["claim_processes_at"] is None
        assert status["claim_processes_at_local"] == "instant"
        assert status["in_time_for_kickoff"] is True
        out = priority_strategy(league, {"settings": {"waiver_position": 3}}, worth="high",
                                demand="low", kickoff=now + timedelta(days=3), now=now)
        assert out["recommendation"] == "add_now" and out["claim_cost_places"] == 0
