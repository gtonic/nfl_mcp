"""Tests for the FAAB bid recommender (offline, mocked Sleeper + values)."""

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
    "elite3": {"player_id": "elite3", "name": "Elite RB C", "position": "RB", "value": 9400, "position_rank": 4},
    "te1": {"player_id": "te1", "name": "My TE", "position": "TE", "value": 3000, "position_rank": 12},
    "te_fa": {"player_id": "te_fa", "name": "Free TE", "position": "TE", "value": 4500, "position_rank": 8},
    "mid_rb": {"player_id": "mid_rb", "name": "Mid RB", "position": "RB", "value": 2000, "position_rank": 30},
    "k_fa": {"player_id": "k_fa", "name": "Free K", "position": "K", "value": 500, "position_rank": 1},
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
        # Three RBs better than the target fill both RB slots and the FLEX ->
        # it's depth, not an upgrade. (With only two, the FLEX would be empty
        # and he would start.)
        rosters = {"success": True, "rosters": [
            {"roster_id": 1, "players_enriched": [
                {"player_id": "9509", "full_name": "Bijan Robinson", "position": "RB"},
                {"player_id": "elite1", "full_name": "Elite RB A", "position": "RB"},
                {"player_id": "elite3", "full_name": "Elite RB C", "position": "RB"},
            ], "settings": {"waiver_budget_used": 0}}]}
        # Target the weaker RB (value 9300, below my last starter 9500).
        res = await _run(league_id="1", player_id="elite2", my_roster_id=1, rosters=rosters)
        r = res["recommendation"]
        assert r["breakdown"]["upgrade_score"] == 0.0   # no upgrade
        assert any("strong at RB" in w for w in r["warnings"])

    async def test_an_empty_flex_makes_the_same_rb_an_upgrade(self):
        rosters = {"success": True, "rosters": [
            {"roster_id": 1, "players_enriched": [
                {"player_id": "9509", "full_name": "Bijan Robinson", "position": "RB"},
                {"player_id": "elite1", "full_name": "Elite RB A", "position": "RB"},
            ], "settings": {"waiver_budget_used": 0}}]}
        res = await _run(league_id="1", player_id="elite2", my_roster_id=1, rosters=rosters)
        assert res["recommendation"]["breakdown"]["upgrade_score"] == 1.0
        assert res["horizon"] == "rest_of_season"

    async def test_a_te_who_beats_your_te1_is_an_upgrade_not_depth(self):
        # The Dalton Schultz case: a fixed TE=1 table with no FLEX called him depth.
        rosters = {"success": True, "rosters": [
            {"roster_id": 1, "players_enriched": [
                {"player_id": "9509", "full_name": "Bijan Robinson", "position": "RB"},
                {"player_id": "elite1", "full_name": "Elite RB A", "position": "RB"},
                {"player_id": "elite3", "full_name": "Elite RB C", "position": "RB"},
                {"player_id": "te1", "full_name": "My TE", "position": "TE"},
            ], "settings": {"waiver_budget_used": 0}}]}
        res = await _run(league_id="1", player_id="te_fa", my_roster_id=1, rosters=rosters)
        r = res["recommendation"]
        assert r["breakdown"]["upgrade_score"] > 0
        assert not any("strong at TE" in w for w in r["warnings"])

    async def test_a_position_the_league_does_not_start(self):
        rosters = {"success": True, "rosters": [
            {"roster_id": 1, "players_enriched": [], "settings": {"waiver_budget_used": 0}}]}
        res = await _run(league_id="1", player_id="k_fa", my_roster_id=1, rosters=rosters)
        assert any("starts no K" in w for w in res["recommendation"]["warnings"])

    async def test_non_faab_league(self):
        res = await _run(league_id="1", player_id="9509", league=_league(faab=False))
        assert res["is_faab_league"] is False
        assert any("Not a FAAB" in w for w in res["recommendation"]["warnings"])

    async def test_player_not_in_values(self):
        res = await _run(league_id="1", player_id="unknown_id")
        assert res["recommendation"] is None
        assert "consensus value list" in res["message"]

    async def test_exhausted_budget_caps_every_bid_at_zero(self):
        # `remaining or 10**9` read an empty budget as unlimited.
        rosters = {"success": True, "rosters": [
            {"roster_id": 1, "players_enriched": [], "settings": {"waiver_budget_used": 100}}]}
        res = await _run(league_id="1", player_id="9509", my_roster_id=1, rosters=rosters)
        r = res["recommendation"]
        assert res["remaining_budget"] == 0
        assert r["bid_absolute"] == 0
        assert r["range_absolute"] == {"safe": 0, "aggressive": 0}

    async def test_a_rostered_player_is_not_a_waiver_target(self):
        rosters = {"success": True, "rosters": [
            {"roster_id": 4, "players": ["9509"], "players_enriched": []}]}
        res = await _run(league_id="1", player_id="9509", rosters=rosters)
        assert res["recommendation"] is None
        assert res["rostered_by"] == 4
        assert "already rostered" in res["message"]


class TestPriorityAdvice:
    """Non-FAAB advice used to say "high waiver-priority claim" for everyone.

    Tre Tucker, a +0 upgrade, was told to spend a high claim — under rolling
    waivers that sends you to the back of the order for depth.
    """

    ROSTER_FULL_AT_RB = {"success": True, "rosters": [{"roster_id": 1, "players_enriched": [
        {"player_id": "9509", "full_name": "Bijan Robinson", "position": "RB"},
        {"player_id": "elite1", "full_name": "Elite RB A", "position": "RB"},
        {"player_id": "elite3", "full_name": "Elite RB C", "position": "RB"},
    ]}]}

    async def test_depth_does_not_get_a_high_claim(self):
        res = await _run(league_id="1", player_id="mid_rb", my_roster_id=1,
                         league=_league(faab=False), rosters=self.ROSTER_FULL_AT_RB,
                         trending=[])
        r = res["recommendation"]
        assert r["tier"] == "solid" and r["breakdown"]["upgrade_score"] == 0.0
        assert r["priority_advice"] == "low"
        assert "high waiver-priority" not in res["message"]
        assert "don't burn waiver priority" in res["message"]
        assert "back of the order" in res["message"]   # waiver_type 0 is rolling

    async def test_a_real_upgrade_gets_a_high_claim(self):
        rosters = {"success": True, "rosters": [{"roster_id": 1, "players_enriched": [
            {"player_id": "weak", "full_name": "Weak RB", "position": "RB"}]}]}
        res = await _run(league_id="1", player_id="9509", my_roster_id=1,
                         league=_league(faab=False), rosters=rosters)
        assert res["recommendation"]["priority_advice"] == "high"
        assert "high waiver-priority claim" in res["message"]

    async def test_faab_leagues_carry_no_priority_advice(self):
        res = await _run(league_id="1", player_id="9509")
        assert res["recommendation"]["priority_advice"] is None

    def test_advice_follows_tier_and_gain(self):
        assert ft._priority_advice("must_add", 0.0) == "high"      # scarce, high value
        assert ft._priority_advice("strong", 0.0) == "low"
        assert ft._priority_advice("strong", 0.6) == "high"
        assert ft._priority_advice("speculative", 0.6) == "medium"
        assert ft._priority_advice("hold_or_stream", 1.0) == "low"
        assert ft._priority_advice("solid", None) == "medium"      # no roster context

    async def test_requires_player(self):
        res = await ft.recommend_faab_bid(league_id="1", db=None)
        assert res["success"] is False
