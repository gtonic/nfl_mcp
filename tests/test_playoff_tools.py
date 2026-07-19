"""Tests for the Monte-Carlo playoff odds tool (offline, mocked Sleeper)."""

import random
import contextlib
from unittest.mock import patch

import pytest

from nfl_mcp import playoff_tools as pt


class TestSimulate:
    def test_strong_beats_weak_and_deterministic(self):
        teams = [
            {"roster_id": 1, "wins": 10, "points": 1440, "mean": 120},
            {"roster_id": 2, "wins": 8, "points": 1344, "mean": 112},
            {"roster_id": 3, "wins": 2, "points": 1080, "mean": 90},
            {"roster_id": 4, "wins": 1, "points": 1020, "mean": 85},
        ]
        schedule = [(1, 3), (2, 4), (1, 2), (3, 4)]
        a = pt._simulate(teams, schedule, 2, 3000, 25.0, random.Random(1))
        b = pt._simulate(teams, schedule, 2, 3000, 25.0, random.Random(1))
        # deterministic with same seed
        assert a[1]["playoff_pct"] == b[1]["playoff_pct"]
        # strong team makes playoffs far more often than weak team
        assert a[1]["playoff_pct"] > a[4]["playoff_pct"]
        # top-2 league: total made ≈ 2 per sim -> pct sums near 200
        assert abs(sum(v["playoff_pct"] for v in a.values()) - 200.0) < 1.0


def _mock_league(playoff_teams=4, playoff_week_start=15):
    return {"success": True, "league": {"settings": {
        "playoff_teams": playoff_teams, "playoff_week_start": playoff_week_start}}}


def _mock_rosters():
    def r(rid, w, l, ppg):
        return {"roster_id": rid, "owner_id": f"u{rid}",
                "settings": {"wins": w, "losses": l, "ties": 0, "fpts": ppg * (w + l)}}
    return {"success": True, "rosters": [
        r(1, 10, 2, 120), r(2, 8, 4, 112), r(3, 7, 5, 108),
        r(4, 6, 6, 104), r(5, 4, 8, 98), r(6, 2, 10, 90)]}


def _mock_users():
    return {"success": True, "users": [{"user_id": f"u{i}", "display_name": f"Team{i}"} for i in range(1, 7)]}


def _mock_matchups(week):
    pairs = {13: [(1, 6), (2, 5), (3, 4)], 14: [(1, 2), (3, 6), (4, 5)]}.get(week, [])
    ms = []
    for mid, (a, b) in enumerate(pairs, 1):
        ms += [{"roster_id": a, "matchup_id": mid}, {"roster_id": b, "matchup_id": mid}]
    return {"success": True, "matchups": ms, "week": week}


@contextlib.contextmanager
def _patched():
    async def L(l): return _mock_league()
    async def R(l): return _mock_rosters()
    async def U(l): return _mock_users()
    async def S(): return {"success": True, "nfl_state": {"week": 13}}
    async def M(l, w): return _mock_matchups(w)
    with patch.object(pt, "get_league", L), patch.object(pt, "get_rosters", R), \
         patch.object(pt, "get_league_users", U), patch.object(pt, "get_nfl_state", S), \
         patch.object(pt, "get_matchups", M):
        yield


class TestGetPlayoffOdds:
    async def test_ordering_and_bounds(self):
        with _patched():
            res = await pt.get_playoff_odds("123", num_sims=5000, seed=42)
        assert res["success"] is True
        assert res["current_week"] == 13
        assert res["games_remaining"] == 6
        odds = res["odds"]
        by_id = {o["roster_id"]: o for o in odds}
        assert by_id[1]["playoff_pct"] >= by_id[6]["playoff_pct"]
        assert by_id[1]["playoff_pct"] == 100.0    # 10-2 juggernaut always in
        assert by_id[6]["playoff_pct"] < 20.0       # 2-10 near-dead
        # sorted best-first
        assert odds == sorted(odds, key=lambda x: x["playoff_pct"], reverse=True)

    async def test_this_week_swing(self):
        with _patched():
            res = await pt.get_playoff_odds("123", num_sims=3000, seed=1, my_roster_id=4)
        sw = res["this_week_swing"]
        assert sw["my_roster_id"] == 4
        # winning never hurts your odds
        assert sw["if_win_pct"] >= sw["if_lose_pct"]

    async def test_league_load_failure(self):
        async def L(l): return {"success": False, "error": "nope"}
        with patch.object(pt, "get_league", L):
            res = await pt.get_playoff_odds("123")
        assert res["success"] is False
