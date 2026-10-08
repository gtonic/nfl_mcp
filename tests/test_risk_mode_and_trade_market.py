"""Risk-aware lineups (risk_mode) and the league trade market.

Network is never touched: playoff odds go through `risk_mode._fetch_odds`
(stubbed per test), the market's league context is built by hand.
"""
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import risk_mode as rm
from nfl_mcp import trade_market as tm
from nfl_mcp import trade_risk
from nfl_mcp.win_probability import optimize_win_probability

# ---------------------------------------------------------------------------
# risk_mode: resolution
# ---------------------------------------------------------------------------

class TestResolve:
    def test_normalize_and_aliases(self):
        assert rm.normalize_risk_mode(None) == "auto"
        assert rm.normalize_risk_mode("Seek-Variance") == "seek_variance"
        assert rm.normalize_risk_mode("ceiling") == "seek_variance"
        assert rm.normalize_risk_mode("floor") == "protect_floor"
        with pytest.raises(ValueError):
            rm.normalize_risk_mode("yolo")

    def test_auto_underdog_seeks_variance_on_p_win(self):
        r = rm.resolve("auto", p_win=0.30, playoff_pct=50.0)
        assert r["risk_mode"] == "seek_variance" and r["target"] == "p_win"
        assert "underdog" in r["reason"]

    def test_auto_heavy_favourite_protects_floor(self):
        r = rm.resolve("auto", p_win=0.85, playoff_pct=97.0)
        assert r["risk_mode"] == "protect_floor" and r["target"] == "p_win"
        assert "nearly safe" in r["reason"]

    def test_auto_long_shot_close_game_tilts_beyond_p_win(self):
        r = rm.resolve("auto", p_win=0.50, playoff_pct=9.0)
        assert r["risk_mode"] == "seek_variance" and r["target"] == "shifted"
        assert "long-shot" in r["reason"]

    def test_auto_long_shot_but_comfortable_favourite_protects(self):
        r = rm.resolve("auto", p_win=0.80, playoff_pct=9.0)
        assert r["risk_mode"] == "protect_floor"

    def test_auto_without_opponent_is_neutral_unless_long_shot(self):
        assert rm.resolve("auto")["risk_mode"] == "neutral"
        assert rm.resolve("auto", playoff_pct=5.0)["risk_mode"] == "seek_variance"

    def test_explicit_modes_win(self):
        assert rm.resolve("neutral", p_win=0.2)["target"] == "mean"
        assert rm.resolve("protect_floor", p_win=0.2)["risk_mode"] == "protect_floor"

    def test_trade_resolution_from_season(self):
        assert rm.resolve_trade("auto", 9.0)["risk_mode"] == "seek_variance"
        assert rm.resolve_trade("auto", 97.0)["risk_mode"] == "protect_floor"
        assert rm.resolve_trade("auto", 40.0)["risk_mode"] == "neutral"
        assert rm.resolve_trade("auto", None)["risk_mode"] == "neutral"

    def test_lineup_target(self):
        seek = {"risk_mode": "seek_variance", "target": "shifted"}
        protect = {"risk_mode": "protect_floor", "target": "shifted"}
        # Without an opponent the target sits half an sd off one's own mean.
        assert rm.lineup_target(seek, 100.0, 400.0, None, 0.0) == pytest.approx(110.0)
        assert rm.lineup_target(protect, 100.0, 400.0, None, 0.0) == pytest.approx(90.0)
        # Trailing by more than the shift: beat the opponent, nothing more.
        assert rm.lineup_target(seek, 100.0, 300.0, 130.0, 100.0) == pytest.approx(130.0)
        assert rm.lineup_target({"risk_mode": "neutral", "target": "mean"}, 1, 1, 2, 1) is None


# ---------------------------------------------------------------------------
# risk_mode: season odds (cached)
# ---------------------------------------------------------------------------

class TestSeasonOdds:
    @pytest.mark.asyncio
    async def test_cached_per_league(self, monkeypatch):
        calls = []

        async def _fetch(league_id, num_sims, db):
            calls.append(league_id)
            return {"success": True, "playoff_teams": 6, "odds": [
                {"roster_id": 7, "playoff_pct": 9.4, "record": "0-4"},
                {"roster_id": 1, "playoff_pct": 96.0, "record": "3-1"}]}

        monkeypatch.setattr(rm, "_fetch_odds", _fetch)
        assert await rm.playoff_pct_for("L", 7) == 9.4
        assert await rm.playoff_pct_for("L", 1) == 96.0
        assert calls == ["L"]
        assert await rm.playoff_pct_for("L", 99) is None

    @pytest.mark.asyncio
    async def test_failure_is_unknown_not_an_error(self, monkeypatch):
        async def _fail(*_a):
            return {"success": False, "odds": []}

        monkeypatch.setattr(rm, "_fetch_odds", _fail)
        assert await rm.playoff_pct_for("L", 7) is None


# ---------------------------------------------------------------------------
# Lineup optimizer under risk modes
# ---------------------------------------------------------------------------

def _wr(name, mean, sd):
    return {"name": name, "position": "WR", "projected_points": mean, "sd": sd}


class TestOptimizeWithRiskMode:
    def test_neutral_is_points_optimal_even_as_an_underdog(self):
        cands = [_wr("Steady", 12, 3), _wr("Boom", 11, 8)]
        opp = [_wr("Star", 16, 3)]
        res = optimize_win_probability(cands, opp, slots={"WR": 1}, risk_mode="neutral")
        assert res["recommended_lineup"][0]["player"] == "Steady"
        assert res["risk_mode"] == "neutral" and res["risk_adjustment"] is None

    def test_auto_underdog_reports_the_trade_off(self):
        cands = [_wr("Steady", 12, 3), _wr("Boom", 11, 8)]
        opp = [_wr("Star", 16, 3)]
        res = optimize_win_probability(cands, opp, slots={"WR": 1})
        assert res["risk_mode"] == "seek_variance"
        assert res["recommended_lineup"][0]["player"] == "Boom"
        adj = res["risk_adjustment"]
        assert adj["swaps"][0] == {"start": "Boom", "start_mean": 11, "start_sd": 8,
                                   "over": "Steady", "over_mean": 12, "over_sd": 3}
        assert adj["mean_delta"] == -1.0
        assert adj["summary"].startswith("Starting Boom over Steady raises P(win)")
        assert "although mean -1.0" in adj["summary"]

    def test_long_shot_in_a_close_game_takes_the_ceiling(self):
        # Even game: P(win) alone barely cares; a 9% season does.
        cands = [_wr("Steady", 12, 2), _wr("Boom", 11.5, 9)]
        opp = [_wr("Opp", 12, 4)]
        plain = optimize_win_probability(cands, opp, slots={"WR": 1}, playoff_pct=60.0)
        long_shot = optimize_win_probability(cands, opp, slots={"WR": 1}, playoff_pct=9.0)
        assert plain["recommended_lineup"][0]["player"] == "Steady"
        assert long_shot["risk_mode"] == "seek_variance"
        assert long_shot["recommended_lineup"][0]["player"] == "Boom"
        assert "long-shot" in long_shot["risk_reason"]

    def test_explicit_protect_floor_without_being_favoured(self):
        cands = [_wr("Boom", 12, 9), _wr("Steady", 11.5, 2)]
        res = optimize_win_probability(cands, None, slots={"WR": 1}, risk_mode="protect_floor")
        assert res["recommended_lineup"][0]["player"] == "Steady"
        assert res["win_probability"] is None and res["you_are"] is None

    def test_no_opponent_auto_is_expected_points(self):
        cands = [_wr("Steady", 12, 2), _wr("Boom", 11.5, 9)]
        res = optimize_win_probability(cands, None, slots={"WR": 1})
        assert res["risk_mode"] == "neutral"
        assert res["recommended_lineup"][0]["player"] == "Steady"

    @pytest.mark.asyncio
    async def test_tool_reads_playoff_odds_for_auto(self, monkeypatch):
        from nfl_mcp.win_probability import get_win_probability_lineup

        async def _fetch(*_a):
            return {"success": True, "odds": [{"roster_id": 7, "playoff_pct": 8.0}]}

        monkeypatch.setattr(rm, "_fetch_odds", _fetch)
        res = await get_win_probability_lineup(
            [_wr("Steady", 12, 2), _wr("Boom", 11.5, 9)], [_wr("Opp", 12, 4)],
            slots={"WR": 1}, league_id="L", roster_id=7)
        assert res["success"] and res["playoff_pct"] == 8.0
        assert res["risk_mode"] == "seek_variance"
        assert "Risk mode seek_variance" in res["message"]

    @pytest.mark.asyncio
    async def test_tool_rejects_an_unknown_mode(self):
        from nfl_mcp.win_probability import get_win_probability_lineup
        res = await get_win_probability_lineup([_wr("A", 1, 1)], [_wr("B", 1, 1)], risk_mode="yolo")
        assert res["success"] is False


class TestSlotChoice:
    def test_matchup_picks_the_higher_p_win(self):
        cands = [{"key": "Steady", "mean": 10.0, "sd": 2.0}, {"key": "Boom", "mean": 9.5, "sd": 8.0}]
        ctx = {"my_mean": 95.0, "my_var": 400.0, "opp_mean": 115.0, "opp_var": 400.0,
               "ref_key": "Steady"}
        pick = rm.slot_choice(cands, "auto", context=ctx)
        assert pick["resolution"]["risk_mode"] == "seek_variance"
        assert pick["best_key"] == "Boom" and pick["points_best_key"] == "Steady"
        assert pick["by_key"]["Boom"]["p_win"] > pick["by_key"]["Steady"]["p_win"]

    def test_without_matchup_auto_ranks_on_points(self):
        cands = [{"key": "Steady", "mean": 10.0, "sd": 2.0}, {"key": "Boom", "mean": 9.5, "sd": 8.0}]
        assert rm.slot_choice(cands, "auto")["best_key"] == "Steady"
        assert rm.slot_choice(cands, "seek_variance")["best_key"] == "Boom"
        assert rm.slot_choice(cands, "protect_floor")["best_key"] == "Steady"


@pytest.mark.usefixtures("offline_sources")
class TestCompareWithRisk:
    @pytest.fixture(autouse=True)
    def _optimizer(self, monkeypatch):
        from nfl_mcp import lineup_optimizer_tools as lo
        from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer

        analyzer = DefenseRankingsAnalyzer(db=None)
        analyzer.fetch_defense_rankings = AsyncMock(return_value={})
        analyzer.get_matchup_difficulty = lambda pos, opp, rk=None: {"rank": 16, "matchup_tier": "neutral"}
        opt = lo.LineupOptimizer(db=None, defense_analyzer=analyzer, auto_project=False)
        monkeypatch.setattr(lo, "get_lineup_optimizer", lambda: opt)

    def _players(self):
        return [
            {"name": "Steady Eddie", "position": "RB", "team": "KC", "opponent": "LV",
             "projection": {"projected_points": 10.0, "floor": 8.0, "ceiling": 12.0}},
            {"name": "Boom Bust", "position": "WR", "team": "BUF", "opponent": "MIA",
             "projection": {"projected_points": 9.5, "floor": 1.0, "ceiling": 18.0}},
        ]

    @pytest.mark.asyncio
    async def test_underdog_starts_the_ceiling_and_says_why(self):
        from nfl_mcp.lineup_optimizer_tools import compare_players_for_slot
        res = await compare_players_for_slot(
            self._players(), slot="FLEX", scoring="ppr",
            matchup_context={"my_mean": 95.0, "my_var": 400.0, "opp_mean": 118.0,
                             "opp_var": 400.0, "starting": ["Steady Eddie"]})
        assert res["success"]
        assert res["points_winner"] == "Steady Eddie"
        assert res["winner"]["player"] == "Boom Bust"
        assert res["risk"]["risk_mode"] == "seek_variance"
        assert res["risk"]["summary"].startswith("Starting Boom Bust over Steady Eddie raises P(win)")
        assert "On points alone" in res["verdict"]

    @pytest.mark.asyncio
    async def test_without_context_auto_keeps_the_points_pick(self):
        from nfl_mcp.lineup_optimizer_tools import compare_players_for_slot
        res = await compare_players_for_slot(self._players(), slot="FLEX", scoring="ppr")
        assert res["winner"]["player"] == "Steady Eddie"
        assert res["risk"]["risk_mode"] == "neutral" and res["risk"]["summary"] is None


# ---------------------------------------------------------------------------
# Trade risk weighting
# ---------------------------------------------------------------------------

def _entry(pid, pos, per_week, weeks, name=None):
    return {"player_id": pid, "player": name or pid, "name": name or pid, "position": pos,
            "team": "KC", "weekly_points": dict.fromkeys(weeks, per_week),
            "total_points": per_week * len(weeks), "projected_points": per_week * len(weeks),
            "ros_points": per_week * len(weeks), "playoff_points": 0.0}


class TestTradeRisk:
    def test_components(self):
        weeks, playoff = [6, 7, 8], [9]
        allw = weeks + playoff
        slots = {"WR": 1}
        before = [_entry("a", "WR", 10.0, allw)]
        after = [_entry("b", "WR", 12.0, allw)]
        c = trade_risk.risk_components(before, after, slots, weeks, playoff)
        assert c["playoff_gain"] == pytest.approx(2.0)
        assert c["upside_gain"] > 0  # more points, same volatility -> more sd

    def test_long_shot_re_ranks_by_upside_but_keeps_your_gain(self):
        weeks = [6, 7]
        mine = [_entry("m1", "RB", 10.0, weeks), _entry("m2", "WR", 10.0, weeks)]
        by_pid = {"te": _entry("te", "TE", 10.0, weeks), "qb": _entry("qb", "QB", 10.4, weeks)}
        props = [
            {"you_give": {"player_id": "m2"}, "you_get": {"player_id": "qb"}, "your_gain": 1.0,
             "rank_score": 1.0, "partner_roster_id": 2},
            {"you_give": {"player_id": "m2"}, "you_get": {"player_id": "te"}, "your_gain": 0.5,
             "rank_score": 0.5, "partner_roster_id": 3},
        ]
        res = trade_risk.adjust_proposals(
            props, mine=mine, by_pid=by_pid, slots={"RB": 1, "WR": 1, "TE": 1, "QB": 1},
            regular_weeks=weeks, playoff_weeks=[],
            resolution={"risk_mode": "seek_variance"})
        assert all("risk" in p for p in res)
        assert res[0]["your_gain"] in (1.0, 0.5)
        assert {p["your_gain"] for p in res} == {1.0, 0.5}


# ---------------------------------------------------------------------------
# Trade market
# ---------------------------------------------------------------------------

WEEKS = [6, 7, 8, 9, 10]
PLAYOFF = [11]
SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}


def _ctx(rosters: dict[int, list[tuple]], values: dict[str, float] | None = None,
         odds: dict[int, dict] | None = None) -> dict:
    allw = WEEKS + PLAYOFF
    scored, by_id, roster_list = {}, {}, []
    for rid, players in rosters.items():
        entries = []
        for pid, name, pos, ppw in players:
            e = _entry(pid, pos, ppw, allw, name)
            entries.append(e)
            by_id[pid] = e
        scored[rid] = entries
        roster_list.append({"roster_id": rid, "owner_id": f"u{rid}",
                            "players": [p[0] for p in players], "settings": {"wins": 2, "losses": 2}})
    vals = {pid: {"player_id": pid, "value": v, "position": by_id[pid]["position"]}
            for pid, v in (values or {}).items()}
    return {
        "league_id": "L", "league": {"roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE",
                                                          "BN", "BN", "BN", "BN"],
                                     "total_rosters": len(rosters), "name": "Test"},
        "rosters": roster_list, "roster_by_id": {r["roster_id"]: r for r in roster_list},
        "roster_state": {"stale": False, "snapshot_age_seconds": 0, "warning": None},
        "names": {rid: f"Team{rid}" for rid in rosters}, "season": 2026, "week": 6,
        "by_id": by_id, "meta": {"windows": {"regular": WEEKS, "playoff": PLAYOFF}},
        "weeks": allw, "regular_weeks": WEEKS, "playoff_weeks": PLAYOFF,
        "slots": SLOTS, "scored": scored,
        "values": {"by_id": vals, "by_name": {}, "source": "test", "stale": False},
        "fmt": {"scoring_model": None}, "odds": odds or {}, "num_teams": len(rosters),
        "bar": 1.0, "_base": {}, "timing": {"projection_seconds": 0.0},
    }


def _league():
    """Team 1 is deep at RB and thin at WR; team 2 the reverse; team 3 balanced."""
    return _ctx({
        1: [("q1", "Q1", "QB", 18), ("r1", "R1", "RB", 15), ("r2", "R2", "RB", 13),
            ("r3", "R3", "RB", 12), ("w1", "W1", "WR", 12), ("w2", "W2", "WR", 4),
            ("t1", "T1", "TE", 8)],
        2: [("q2", "Q2", "QB", 18), ("r4", "R4", "RB", 14), ("r5", "R5", "RB", 4),
            ("w3", "W3", "WR", 15), ("w4", "W4", "WR", 13), ("w5", "W5", "WR", 12),
            ("t2", "T2", "TE", 8)],
        3: [("q3", "Q3", "QB", 17), ("r6", "R6", "RB", 11), ("r7", "R7", "RB", 10),
            ("w6", "W6", "WR", 11), ("w7", "W7", "WR", 10), ("t3", "T3", "TE", 9)],
    }, values={"r3": 3000, "w5": 3000, "r1": 6000, "w3": 6000, "t1": 500, "w2": 300,
               "r5": 300, "w4": 4000, "r2": 4000},
        odds={1: {"playoff_pct": 9.0, "record": "0-4"}, 2: {"playoff_pct": 95.0, "record": "4-0"},
              3: {"playoff_pct": 40.0, "record": "2-2"}})


class TestRosterProfile:
    def test_needs_and_surpluses_mirror_each_other(self):
        ctx = _league()
        p1, p2 = tm.roster_profile(ctx, 1), tm.roster_profile(ctx, 2)
        assert p1["needs"]["WR"] > p1["needs"]["RB"]
        assert p2["needs"]["RB"] > p2["needs"]["WR"]
        assert p1["surpluses"]["RB"] > 0 and p2["surpluses"]["WR"] > 0
        assert "R3" in [p["name"] for p in p1["surplus_players"]]
        assert p1["situation"] == "long_shot" and p2["situation"] == "contender"

    def test_partner_fit_finds_the_complement(self):
        ctx = _league()
        p1, p2 = tm.roster_profile(ctx, 1), tm.roster_profile(ctx, 2)
        fit = tm.partner_fit(p1, p2, tm._surplus_names(p1), tm._surplus_names(p2))
        assert fit["natural_partner"]
        assert fit["they_need_you_have"][0]["position"] == "RB"
        assert fit["you_need_they_have"][0]["position"] == "WR"


class TestAcceptance:
    def _ev(self, give, get, values=None):
        ctx = _league()
        if values:
            for pid, v in values.items():
                ctx["values"]["by_id"][pid] = {"player_id": pid, "value": v,
                                               "position": ctx["by_id"][pid]["position"]}
        profiles = {rid: tm.roster_profile(ctx, rid) for rid in ctx["scored"]}
        return tm.evaluate_trade(ctx, profiles, 1, 2, give, get)

    def test_fair_complementary_swap_is_likely(self):
        ev = self._ev(["r3"], ["w5"])
        assert ev["your_gain"] > 0 and ev["their_gain"] > 0 and ev["both_gain"]
        assert ev["acceptance_label"] in ("high", "medium")
        names = {f["factor"] for f in ev["acceptance_factors"]}
        assert {"their_lineup_gain", "market_value", "positional_need", "situation"} <= names

    def test_market_lopsided_against_them_is_less_likely(self):
        fair = self._ev(["r3"], ["w5"])
        lopsided = self._ev(["r3"], ["w5"], values={"r3": 1000})
        assert lopsided["acceptance_likelihood"] < fair["acceptance_likelihood"]

    def test_their_lineup_loss_is_less_likely(self):
        good = self._ev(["r3"], ["w5"])
        bad = self._ev(["w2"], ["w3"])  # they lose their best WR for a scrub
        assert bad["their_gain"] < 0
        assert bad["acceptance_likelihood"] < good["acceptance_likelihood"]


class TestCounters:
    def test_counter_keeps_my_gain_and_raises_acceptance(self):
        ctx = _league()
        profiles = {rid: tm.roster_profile(ctx, rid) for rid in ctx["scored"]}
        # A stingy offer: their spare WR for my scrub WR.
        res = tm.counter_offers(ctx, profiles, 1, 2, ["w2"], ["w5"])
        orig = res["original"]
        assert orig["your_gain"] > 0
        assert res["counters"], res["message"]
        for c in res["counters"]:
            assert c["your_gain"] >= res["keep_your_gain_at_least"]
            assert c["their_gain"] > 0
            assert c["acceptance_likelihood"] > orig["acceptance_likelihood"]
            assert c["change"]
        assert len(res["counters"]) <= tm.MAX_COUNTERS


class TestGetTradeMarket:
    @pytest.mark.asyncio
    async def test_end_to_end(self, monkeypatch):
        from nfl_mcp import trade_finder_tools

        async def _ctx_builder(*_a, **_k):
            return _league()

        async def _finder(*_a, **_k):
            return {"success": True, "risk_mode": "seek_variance", "risk_reason": "long shot",
                    "trade_deadline": {"passed": False},
                    "proposals": [{"partner_roster_id": 2, "you_give": {"player_id": "r3"},
                                   "you_get": {"player_id": "w5"}, "your_gain": 5.0,
                                   "their_gain": 4.0, "risk": {"risk_mode": "seek_variance"}}],
                    "package_proposals": []}

        monkeypatch.setattr(tm, "build_context", _ctx_builder)
        monkeypatch.setattr(trade_finder_tools, "find_trade_targets", _finder)
        res = await tm.get_trade_market("L", 1, offer={"partner_roster_id": 2,
                                                       "you_give": ["w2"], "you_get": ["w5"]})
        assert res["success"], res.get("error")
        assert res["you"]["roster_id"] == 1 and len(res["teams"]) == 3
        top = res["partners"][0]
        assert top["partner_roster_id"] == 2 and top["natural_partner"]
        pkg = top["packages"][0]
        assert 0.0 <= pkg["acceptance_likelihood"] <= 1.0
        assert pkg["finder_your_gain"] == 5.0
        assert res["counter_offers"]["original"]["partner_roster_id"] == 2
        assert res["risk_mode"] == "seek_variance"

    @pytest.mark.asyncio
    async def test_bad_offer_is_reported(self, monkeypatch):
        from nfl_mcp import trade_finder_tools

        async def _ctx_builder(*_a, **_k):
            return _league()

        monkeypatch.setattr(tm, "build_context", _ctx_builder)
        monkeypatch.setattr(trade_finder_tools, "find_trade_targets",
                            AsyncMock(return_value={"proposals": [], "package_proposals": []}))
        res = await tm.get_trade_market("L", 1, offer={"you_give": ["w2"]})
        assert "error" in res["counter_offers"]

    @pytest.mark.asyncio
    async def test_registry_validates_input(self):
        from nfl_mcp import tool_registry
        res = await tool_registry.get_trade_market("L", roster_id=1, offer="nope")
        assert res["success"] is False


class TestAnalyzeTradeCounters:
    @pytest.mark.asyncio
    async def test_suggest_counters_and_risk_block(self, monkeypatch):
        from unittest.mock import patch

        from nfl_mcp import trade_analyzer_tools as ta

        r1 = {"roster_id": 1, "players_enriched": [
            {"player_id": "1", "full_name": "P1", "position": "RB"}], "starters_enriched": []}
        r2 = {"roster_id": 2, "players_enriched": [
            {"player_id": "3", "full_name": "P3", "position": "WR"}], "starters_enriched": []}

        async def _rosters(_lid):
            return {"success": True, "rosters": [r1, r2]}

        async def _league(_lid):
            return {"success": True, "league": {"scoring_settings": {"rec": 1.0},
                    "roster_positions": ["RB", "WR"], "total_rosters": 12, "settings": {"type": 0}}}

        async def _trending(*_a):
            return {"success": True, "trending_players": []}

        class _Svc:
            async def get_values(self, **_k):
                return {"source": "fantasycalc", "stale": False, "by_id": {}}

            def lookup(self, *_a, **_k):
                return None

        async def _odds(*_a):
            return {"success": True, "odds": [{"roster_id": 1, "playoff_pct": 7.0}]}

        counters = AsyncMock(return_value={"counters": [], "message": "none"})
        monkeypatch.setattr(rm, "_fetch_odds", _odds)
        monkeypatch.setattr(tm, "counters_for_trade", counters)
        with patch.object(ta, "get_rosters", side_effect=_rosters), \
             patch.object(ta, "get_league", side_effect=_league), \
             patch.object(ta, "get_trending_players", side_effect=_trending), \
             patch.object(ta, "get_values_service", return_value=_Svc()):
            res = await ta.analyze_trade("L", 1, 2, ["1"], ["3"], suggest_counters=True)
        assert res["success"]
        assert res["counter_offers"] == {"counters": [], "message": "none"}
        counters.assert_awaited_once()
        assert res["risk"]["risk_mode"] == "seek_variance" and res["risk"]["playoff_pct"] == 7.0
