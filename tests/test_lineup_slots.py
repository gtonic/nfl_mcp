"""One slot-eligibility table and one exact lineup optimizer for every tool.

Regressions covered:
- `win_probability._eligible` only knew FLEX/SUPERFLEX, so Sleeper's
  SUPER_FLEX, WRRB_FLEX, REC_FLEX, IDP_FLEX never filled.
- briefing / roster_needs folded WRRB_FLEX and REC_FLEX into FLEX, starting a
  TE in a WRRB_FLEX and an RB in a REC_FLEX.
- The greedy fill handed a SUPERFLEX listed before the FLEX the best leftover
  RB, leaving the FLEX empty with a QB on the bench.
- analyze_full_lineup dropped SUPER_FLEX/DEF keys, compared bench players only
  against "weak" starters and summed overlapping gains.
- compare_players_for_slot started a QB in a FLEX.
"""
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp.lineup_slots import (
    assign_to_slots,
    normalize_slot,
    optimal_lineup,
    slot_accepts,
    starting_slots,
)
from nfl_mcp.roster_needs import lineup_bars, lineup_gain, lineup_slots, starting_lineup_total
from nfl_mcp.win_probability import mean_optimal_lineup, optimize_win_probability

pytestmark = pytest.mark.usefixtures("offline_sources")  # no ambient network reads


def _p(name, position, points, sd=None):
    p = {"name": name, "position": position, "team": name, "projected_points": points}
    if sd is not None:
        p["sd"] = sd
    return p


def _names(lineup):
    return [p["name"] if p else None for p in lineup]


class TestSlotEligibility:
    @pytest.mark.parametrize("slot,accepts,rejects", [
        ("FLEX", {"RB", "WR", "TE"}, {"QB", "K", "DEF"}),
        ("WRRB_FLEX", {"RB", "WR"}, {"TE", "QB"}),
        ("REC_FLEX", {"WR", "TE"}, {"RB", "QB"}),
        ("SUPER_FLEX", {"QB", "RB", "WR", "TE"}, {"K", "DEF"}),
        ("SUPERFLEX", {"QB", "RB", "WR", "TE"}, {"K", "DEF"}),
        ("IDP_FLEX", {"DL", "LB", "DB"}, {"WR"}),
        ("DEF", {"DEF", "DST"}, {"K"}),
        ("DST", {"DEF", "DST"}, {"K"}),
    ])
    def test_sleeper_slots(self, slot, accepts, rejects):
        for position in accepts:
            assert slot_accepts(slot, position), (slot, position)
        for position in rejects:
            assert not slot_accepts(slot, position), (slot, position)

    def test_numbered_slot_names_are_the_position(self):
        assert normalize_slot("WR2") == "WR"
        assert slot_accepts("RB1", "RB") and not slot_accepts("RB1", "WR")

    def test_starting_slots_keep_restricted_flexes(self):
        slots = starting_slots(["QB", "RB", "WR", "TE", "FLEX", "WRRB_FLEX", "REC_FLEX",
                                "SUPER_FLEX", "K", "DEF", "BN", "BN", "IR", "TAXI"])
        assert slots == {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1, "WRRB_FLEX": 1,
                         "REC_FLEX": 1, "SUPERFLEX": 1, "K": 1, "DST": 1}
        assert lineup_slots(["WRRB_FLEX", "REC_FLEX"]) == {"WRRB_FLEX": 1, "REC_FLEX": 1}

    def test_the_legacy_eligibility_wrapper_uses_the_shared_table(self):
        from nfl_mcp.win_probability import _eligible
        assert _eligible("SUPER_FLEX", "QB")
        assert _eligible("REC_FLEX", "TE") and not _eligible("REC_FLEX", "RB")


class TestOptimalLineup:
    def test_superflex_listed_before_flex_still_fills_both(self):
        """The greedy fill gave SUPERFLEX the RB and left FLEX empty."""
        players = [_p("QB1", "QB", 25), _p("QB2", "QB", 18), _p("RB1", "RB", 22)]
        for superflex in ("SUPERFLEX", "SUPER_FLEX"):
            lineup = mean_optimal_lineup(players, ["QB", superflex, "FLEX"])
            assert _names(lineup) == ["QB1", "QB2", "RB1"]

    def test_superflex_before_flex_on_a_full_roster(self):
        slots = ["QB", "RB", "RB", "WR", "WR", "TE", "SUPERFLEX", "FLEX"]
        players = [_p("QB1", "QB", 24), _p("QB2", "QB", 20),
                   _p("RB1", "RB", 25), _p("RB2", "RB", 23), _p("RB3", "RB", 21),
                   _p("WR1", "WR", 20), _p("WR2", "WR", 17), _p("TE1", "TE", 10),
                   _p("WR3", "WR", 3)]
        # The greedy put RB3 in the SUPERFLEX and WR3 in the FLEX (143) with
        # QB2 on the bench. Optimal: QB2 in SUPERFLEX, RB3 in FLEX.
        lineup = mean_optimal_lineup(players, slots)
        assert sum(p["projected_points"] for p in lineup if p) == 160
        assert _names(lineup)[6:] == ["QB2", "RB3"]

    def test_narrow_flexes_are_respected(self):
        players = [_p("RB1", "RB", 20), _p("RB2", "RB", 19), _p("TE1", "TE", 15),
                   _p("TE2", "TE", 14), _p("WR1", "WR", 5)]
        lineup = optimal_lineup(players, ["RB", "WRRB_FLEX", "REC_FLEX", "TE"])
        # RB2 takes the WRRB_FLEX; the REC_FLEX goes to a TE, never an RB.
        assert _names(lineup)[:2] == ["RB1", "RB2"]
        assert set(_names(lineup)[2:]) == {"TE1", "TE2"}

    def test_unfillable_slot_is_empty_not_filled_illegally(self):
        lineup = optimal_lineup([_p("K1", "K", 9)], ["FLEX", "K"])
        assert _names(lineup) == [None, "K1"]

    def test_defense_spellings(self):
        lineup = optimal_lineup([_p("BUF", "DEF", 8)], ["DST"])
        assert _names(lineup) == ["BUF"]
        lineup = optimal_lineup([_p("BUF", "DST", 8)], ["DEF"])
        assert _names(lineup) == ["BUF"]

    def test_assign_to_slots_reshuffles(self):
        players = [_p("WR1", "WR", 1), _p("RB1", "RB", 1)]
        seats = assign_to_slots(players, ["FLEX", "WR"])
        assert _names(seats) == ["RB1", "WR1"]
        assert assign_to_slots([_p("TE1", "TE", 1)], ["WRRB_FLEX"]) is None


class TestRosterNeedsUseTheSameOptimizer:
    ROSTER = ["QB", "SUPER_FLEX", "FLEX"]

    def test_lineup_total_and_gain_with_superflex_first(self):
        players = [_p("QB1", "QB", 25), _p("RB1", "RB", 22)]
        slots = lineup_slots(self.ROSTER)
        assert starting_lineup_total(players, slots) == 47
        # A second QB is a 18-point gain: he takes the SUPERFLEX, RB1 the FLEX.
        assert lineup_gain(players, slots, _p("QB2", "QB", 18)) == 18

    def test_restricted_flex_gains(self):
        slots = lineup_slots(["RB", "REC_FLEX"])
        players = [_p("RB1", "RB", 15), _p("WR1", "WR", 8)]
        assert lineup_gain(players, slots, _p("RB2", "RB", 20)) == 5  # only beats RB1
        assert lineup_gain(players, slots, _p("TE1", "TE", 12)) == 4

    def test_bars_by_restricted_flex(self):
        slots = lineup_slots(["RB", "WR", "WRRB_FLEX", "TE"])
        players = [_p("RB1", "RB", 15), _p("RB2", "RB", 9), _p("WR1", "WR", 12),
                   _p("TE1", "TE", 6)]
        bars = lineup_bars(players, slots)
        assert bars["RB"] == 9 and bars["WR"] == 9 and bars["TE"] == 6


class TestWinProbabilitySearch:
    def test_superflex_lineup_is_points_optimal(self):
        players = [_p("QB1", "QB", 25, 1), _p("QB2", "QB", 18, 1), _p("RB1", "RB", 22, 1)]
        out = optimize_win_probability(players, [_p("OPP", "QB", 50, 1)],
                                       slots={"QB": 1, "SUPER_FLEX": 1, "FLEX": 1})
        assert out["projected_points"] == 65

    def test_search_moves_starters_between_slots(self):
        """An underdog wants the boom WR in for the steady RB in the RB slot,
        which needs the other RB to slide from FLEX to RB — a bench-into-slot
        swap alone can only replace the FLEX RB."""
        players = [_p("SteadyRB", "RB", 12, 0.1), _p("BoomRB", "RB", 11.8, 6),
                   _p("BoomWR", "WR", 11.5, 6)]
        out = optimize_win_probability(players, [_p("OPP", "QB", 40, 1)],
                                       slots={"RB": 1, "FLEX": 1})
        starters = {row["player"] for row in out["recommended_lineup"]}
        assert starters == {"BoomRB", "BoomWR"}
        by_slot = {row["slot"]: row["position"] for row in out["recommended_lineup"]}
        assert by_slot == {"RB": "RB", "FLEX": "WR"}


def _analyzer():
    from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
    analyzer = DefenseRankingsAnalyzer(db=None)
    analyzer.fetch_defense_rankings = AsyncMock(return_value={})
    analyzer.get_matchup_difficulty = lambda pos, opp, rk=None: {
        "rank": 16, "matchup_tier": "neutral"}
    return analyzer


@pytest.fixture
def offline_optimizer(monkeypatch):
    monkeypatch.setattr(lo, "get_lineup_optimizer",
                        lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                   defense_analyzer=_analyzer()))

    async def _state(db=None):
        return {"season": 2026, "week": 4, "source": "nfl_state"}
    monkeypatch.setattr("nfl_mcp.week_context.current_season_week", _state)


def _e(name, position, points):
    return {"name": name, "position": position, "team": "KC", "opponent": "OPP",
            "projection": {"projected_points": points}}


class TestAnalyzeFullLineup:
    @pytest.mark.asyncio
    async def test_superflex_and_def_keys_are_analysed(self, offline_optimizer):
        out = await lo.analyze_full_lineup({
            "QB": [_e("QB1", "QB", 20)],
            "SUPER_FLEX": [_e("QB2", "QB", 18)],
            "DEF": [_e("BUF", "DEF", 8)],
        })
        assert set(out["starters"]) == {"QB", "SUPER_FLEX", "DEF"}
        assert out["total_starters"] == 3
        assert out["total_projected"] == 46

    @pytest.mark.asyncio
    async def test_a_strong_bench_player_behind_a_decent_starter(self, offline_optimizer):
        """10-point WR starting (not a weak spot), 25-point WR on the bench."""
        out = await lo.analyze_full_lineup({
            "WR": [_e("Starter", "WR", 10)],
            "BENCH": [_e("Stud", "WR", 25)],
        })
        assert out["lineup_grade"] != "A"
        assert [c["bench_in"] for c in out["suggested_changes"]] == ["Stud"]
        assert out["optimal_projected"] == 25

    @pytest.mark.asyncio
    async def test_one_bench_player_is_not_suggested_for_every_weak_spot(self, offline_optimizer):
        out = await lo.analyze_full_lineup({
            "WR": [_e("Weak1", "WR", 2), _e("Weak2", "WR", 3)],
            "BENCH": [_e("Good", "WR", 15)],
        })
        assert len(out["suggested_changes"]) == 1
        change = out["suggested_changes"][0]
        assert (change["bench_in"], change["bench_out"], change["gain"]) == ("Good", "Weak1", 13)
        # Best possible is a real lineup (15 + 3), not 5 + 13 + 12.
        assert out["optimal_projected"] == 18
        assert out["lineup_efficiency_pct"] == pytest.approx(5 / 18 * 100, abs=0.1)

    @pytest.mark.asyncio
    async def test_a_bench_qb_for_a_superflex_rb(self, offline_optimizer):
        out = await lo.analyze_full_lineup({
            "QB": [_e("QB1", "QB", 24)],
            "SUPER_FLEX": [_e("RB1", "RB", 14)],
            "FLEX": [_e("WR1", "WR", 12)],
            "BENCH": [_e("QB2", "QB", 20), _e("RB2", "RB", 5)],
        })
        changes = out["suggested_changes"]
        assert len(changes) == 1
        assert (changes[0]["bench_in"], changes[0]["bench_out"]) == ("QB2", "WR1")
        assert out["optimal_projected"] == 24 + 20 + 14


class TestCompareForSlot:
    @pytest.mark.asyncio
    async def test_flex_never_picks_a_quarterback(self, offline_optimizer):
        out = await lo.compare_players_for_slot(
            [_e("BigQB", "QB", 25), _e("WR1", "WR", 12), _e("RB1", "RB", 10)], slot="FLEX")
        assert out["winner"]["player"] == "WR1"
        assert [i["player"] for i in out["ineligible"]] == ["BigQB"]

    @pytest.mark.asyncio
    async def test_rec_flex_excludes_a_runner(self, offline_optimizer):
        out = await lo.compare_players_for_slot(
            [_e("RB1", "RB", 20), _e("TE1", "TE", 9), _e("WR1", "WR", 8)], slot="REC_FLEX")
        assert out["winner"]["player"] == "TE1"

    @pytest.mark.asyncio
    async def test_no_eligible_player_is_an_error(self, offline_optimizer):
        out = await lo.compare_players_for_slot(
            [_e("QB1", "QB", 20), _e("K1", "K", 9)], slot="FLEX")
        assert out["success"] is False


class TestRosterRecommendationsOrder:
    @pytest.mark.asyncio
    async def test_by_position_is_sorted_by_points(self, offline_optimizer):
        out = await lo.get_roster_recommendations(
            [_e("Low", "WR", 4), _e("High", "WR", 19), _e("Mid", "WR", 11)])
        assert [r["player_name"] for r in out["by_position"]["WR"]] == ["High", "Mid", "Low"]

    def test_def_has_good_game_thresholds(self):
        assert lo._good_game_thresholds("DEF") == lo._good_game_thresholds("DST")
