"""Tests for the FAAB bid recommender (offline, mocked Sleeper + values)."""

import pytest
from unittest.mock import patch

from nfl_mcp import faab_tools as ft


class FakeService:
    def __init__(self, by_id):
        self._by_id = {str(k): v for k, v in by_id.items()}

    async def get_values(self, *a, **k):
        return {"source": "fantasycalc", "list": list(self._by_id.values())}

    def lookup(self, idx, player_id=None, name=None, position=None):
        return self._by_id.get(str(player_id)) or next(
            (v for v in self._by_id.values() if v.get("name") == name), None)


def _league(faab=True, budget=100):
    return {"success": True, "league": {
        "scoring_settings": {"rec": 1.0},
        "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN", "BN"],
        "total_rosters": 12,
        "settings": {"type": 0, "waiver_type": 2 if faab else 0, "waiver_budget": budget},
    }}


# Target: elite RB (value 10000, RB#1). Some other RBs for "redundant" case.
VALUES = {
    "9509": {"player_id": "9509", "name": "Bijan Robinson", "position": "RB", "value": 10000, "position_rank": 1},
    "elite1": {"player_id": "elite1", "name": "Elite RB A", "position": "RB", "value": 9500, "position_rank": 2},
    "elite2": {"player_id": "elite2", "name": "Elite RB B", "position": "RB", "value": 9300, "position_rank": 3},
    "weak": {"player_id": "weak", "name": "Weak RB", "position": "RB", "value": 800, "position_rank": 60},
}


def _patches(league, rosters, trending_ids, week=10):
    async def L(l): return league
    async def R(l): return rosters
    async def T(db, a, b, c): return {"success": True, "trending_players": [{"player_id": p, "count": 999} for p in trending_ids]}
    async def S(): return {"success": True, "nfl_state": {"week": week}}
    return [
        patch.object(ft, "get_league", L),
        patch.object(ft, "get_rosters", R),
        patch.object(ft, "get_trending_players", T),
        patch.object(ft, "get_nfl_state", S),
        patch.object(ft, "get_values_service", lambda db=None: FakeService(VALUES)),
    ]


async def _run(**kwargs):
    league = kwargs.pop("league", _league())
    rosters = kwargs.pop("rosters", {"success": True, "rosters": []})
    trending = kwargs.pop("trending", ["9509"])
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _patches(league, rosters, trending):
            stack.enter_context(p)
        return await ft.recommend_faab_bid(**kwargs)


class TestFaab:
    async def test_elite_add_thin_roster_is_must_add(self):
        rosters = {"success": True, "rosters": [
            {"roster_id": 1, "players_enriched": [{"player_id": "weak", "full_name": "Weak RB", "position": "RB"}],
             "settings": {"waiver_budget_used": 20}}]}
        res = await _run(league_id="1", player_id="9509", my_roster_id=1, rosters=rosters)
        r = res["recommendation"]
        assert res["is_faab_league"] is True
        assert r["tier"] == "must_add"
        assert r["bid_pct"] >= 30
        assert r["bid_absolute"] is not None
        assert res["remaining_budget"] == 80

    async def test_redundant_add_is_cheaper_and_warns(self):
        # I roster two RBs better than the target -> it's depth, not an upgrade.
        rosters = {"success": True, "rosters": [
            {"roster_id": 1, "players_enriched": [
                {"player_id": "9509", "full_name": "Bijan Robinson", "position": "RB"},
                {"player_id": "elite1", "full_name": "Elite RB A", "position": "RB"},
            ], "settings": {"waiver_budget_used": 0}}]}
        # Target the weaker RB (value 9300, below my last starter 9500).
        res = await _run(league_id="1", player_id="elite2", my_roster_id=1, rosters=rosters)
        r = res["recommendation"]
        assert r["breakdown"]["upgrade_score"] == 0.0   # no upgrade
        assert any("strong at RB" in w for w in r["warnings"])

    async def test_non_faab_league(self):
        res = await _run(league_id="1", player_id="9509", league=_league(faab=False))
        assert res["is_faab_league"] is False
        assert any("Not a FAAB" in w for w in res["recommendation"]["warnings"])

    async def test_player_not_in_values(self):
        res = await _run(league_id="1", player_id="unknown_id")
        assert res["recommendation"] is None
        assert "consensus value list" in res["message"]

    async def test_requires_player(self):
        res = await ft.recommend_faab_bid(league_id="1", db=None)
        assert res["success"] is False
