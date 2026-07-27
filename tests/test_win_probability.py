"""Tests for win-probability lineup optimization (nfl_mcp.win_probability)."""
import pytest

from nfl_mcp.win_probability import (
    _team_stats,
    get_win_probability_lineup,
    optimize_win_probability,
    player_sd,
    win_prob,
)


class TestWinProb:
    def test_tie_zero_variance(self):
        assert win_prob(10, 0, 10, 0) == 0.5

    def test_deterministic_when_no_variance(self):
        assert win_prob(12, 0, 8, 0) == 1.0
        assert win_prob(8, 0, 12, 0) == 0.0

    def test_favorite_above_half(self):
        p = win_prob(15, 4, 10, 4)          # Φ(5/sqrt(8)) ≈ 0.9615
        assert p == pytest.approx(0.9615, abs=0.002)

    def test_symmetry(self):
        assert win_prob(10, 5, 14, 5) == pytest.approx(1 - win_prob(14, 5, 10, 5), abs=1e-9)


class TestPlayerSd:
    def test_explicit_sd(self):
        assert player_sd({"sd": 4.0}) == 4.0

    def test_from_floor_ceiling_band(self):
        assert player_sd({"projected_points": 12, "floor": 8, "ceiling": 16}) == 4.0

    def test_position_volatility_fallback(self):
        # WR volatility 0.38 -> sd = 10 * 0.38 = 3.8
        assert player_sd({"projected_points": 10, "position": "WR"}) == pytest.approx(3.8)


class TestOptimizeWinProbability:
    def test_underdog_tilts_to_ceiling(self):
        # Opponent is strong; a boom/bust WR beats the steady one on P(win).
        candidates = [
            {"name": "Steady", "position": "WR", "projected_points": 12, "sd": 3},
            {"name": "Boom", "position": "WR", "projected_points": 11, "sd": 8},
        ]
        opp = [{"name": "Star", "position": "WR", "projected_points": 16, "sd": 3}]
        res = optimize_win_probability(candidates, opp, slots={"WR": 1})

        assert res["you_are"] == "underdog"
        assert res["recommended_lineup"][0]["player"] == "Boom"
        assert res["points_optimal_lineup"][0]["player"] == "Steady"
        assert res["win_probability"] > res["points_optimal_win_probability"]
        assert "ceiling" in res["strategy"]

    def test_favorite_tilts_to_floor(self):
        # Opponent is weak; the low-variance WR protects the lead better.
        candidates = [
            {"name": "Boom", "position": "WR", "projected_points": 12, "sd": 8},
            {"name": "Steady", "position": "WR", "projected_points": 11.5, "sd": 2},
        ]
        opp = [{"name": "Scrub", "position": "WR", "projected_points": 8, "sd": 3}]
        res = optimize_win_probability(candidates, opp, slots={"WR": 1})

        assert res["you_are"] == "favorite"
        assert res["recommended_lineup"][0]["player"] == "Steady"
        assert res["points_optimal_lineup"][0]["player"] == "Boom"
        assert res["win_probability"] > res["points_optimal_win_probability"]
        assert "floor" in res["strategy"]

    def test_flex_eligibility_and_slot_fill(self):
        candidates = [
            {"name": "QB1", "position": "QB", "projected_points": 20, "sd": 4},
            {"name": "RB1", "position": "RB", "projected_points": 15, "sd": 5},
            {"name": "WR1", "position": "WR", "projected_points": 14, "sd": 5},
            {"name": "TE1", "position": "TE", "projected_points": 9, "sd": 4},
        ]
        opp = [{"name": "O", "position": "QB", "projected_points": 18, "sd": 4}]
        res = optimize_win_probability(candidates, opp, slots={"QB": 1, "FLEX": 1})
        slots = {r["slot"] for r in res["recommended_lineup"]}
        assert slots == {"QB", "FLEX"}
        flex = next(r for r in res["recommended_lineup"] if r["slot"] == "FLEX")
        assert flex["position"] in {"RB", "WR", "TE"}   # QB not FLEX-eligible


class TestGetWinProbabilityLineupTool:
    @pytest.mark.asyncio
    async def test_returns_success(self):
        res = await get_win_probability_lineup(
            your_players=[{"name": "A", "position": "WR", "projected_points": 12, "sd": 4}],
            opponent_players=[{"name": "B", "position": "WR", "projected_points": 10, "sd": 4}],
            slots={"WR": 1},
        )
        assert res["success"] is True
        assert res["win_probability"] > 50
        assert res["recommended_lineup"][0]["player"] == "A"

    @pytest.mark.asyncio
    async def test_validation(self):
        r1 = await get_win_probability_lineup(your_players=[], opponent_players=[{"x": 1}])
        r2 = await get_win_probability_lineup(your_players=[{"x": 1}], opponent_players=[])
        assert r1["success"] is False and "your_players" in r1["error"]
        assert r2["success"] is False and "opponent_players" in r2["error"]


class TestStackCorrelation:
    def test_same_team_qb_wr_raises_variance(self):
        qb = {"name": "QB", "position": "QB", "team": "BUF", "projected_points": 20, "sd": 6}
        wr = {"name": "WR", "position": "WR", "team": "BUF", "projected_points": 14, "sd": 6}
        _, var = _team_stats([qb, wr], stack_rho=0.35)
        _, indep = _team_stats([qb, wr], stack_rho=0.0)
        assert var == pytest.approx(indep + 2 * 0.35 * 6 * 6)   # +covariance
        assert var > indep

    def test_no_covariance_for_different_teams(self):
        qb = {"name": "QB", "position": "QB", "team": "BUF", "projected_points": 20, "sd": 6}
        wr = {"name": "WR", "position": "WR", "team": "MIA", "projected_points": 14, "sd": 6}
        _, var = _team_stats([qb, wr], stack_rho=0.35)
        _, indep = _team_stats([qb, wr], stack_rho=0.0)
        assert var == indep

    def test_underdog_prefers_the_stack(self):
        # Same-team QB+WR stack widens variance; as a big underdog that beats a
        # marginally-higher-mean non-stacked WR on P(win).
        candidates = [
            {"name": "QB_BUF", "position": "QB", "team": "BUF", "projected_points": 20, "sd": 7},
            {"name": "WR_BUF", "position": "WR", "team": "BUF", "projected_points": 12, "sd": 7},
            {"name": "WR_MIA", "position": "WR", "team": "MIA", "projected_points": 12.5, "sd": 7},
        ]
        opp = [{"name": "O", "position": "QB", "projected_points": 45, "sd": 9}]
        res = optimize_win_probability(candidates, opp, slots={"QB": 1, "WR": 1})

        rec_wr = next(r for r in res["recommended_lineup"] if r["slot"] == "WR")
        mo_wr = next(r for r in res["points_optimal_lineup"] if r["slot"] == "WR")
        assert mo_wr["player"] == "WR_MIA"          # higher mean, no stack
        assert rec_wr["player"] == "WR_BUF"         # stack chosen for its ceiling
        assert res["stacks"]                        # QB+WR stack reported
        assert res["win_probability"] > res["points_optimal_win_probability"]
        assert "ceiling" in res["strategy"]
