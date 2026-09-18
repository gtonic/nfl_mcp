"""Start/sit must answer in the league's scoring, like every other tool.

`lineup_optimizer_tools` called the projection engine with no scoring, season
or week, so four tools produced full-PPR points off the weaker rank-bucket
baseline while `get_weekly_briefing` — same server, same player — used the
league's real scoring. `week` was accepted, validated and echoed back without
ever reaching the model.
"""
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp.lineup_optimizer_tools import PlayerAnalysis, _good_game_thresholds


def _defense():
    from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
    analyzer = DefenseRankingsAnalyzer(db=None)
    analyzer.fetch_defense_rankings = AsyncMock(return_value={})
    return analyzer


class _RecordingEngine:
    """Stands in for the projection engine and records what it was asked."""

    def __init__(self, points=12.0):
        self.calls = []
        self.points = points

    async def project_many(self, players, **kwargs):
        self.calls.append(kwargs)
        return {"projections": [
            {"projected_points": self.points, "floor": 4.0, "ceiling": 20.0}
            for _ in players
        ]}


def _player(name="Some WR", position="WR"):
    return {"name": name, "position": position, "team": "MIA", "opponent": "NE"}


class TestScoringReachesTheProjection:
    @pytest.mark.asyncio
    async def test_single_player_tool_forwards_scoring_season_and_week(self):
        engine = _RecordingEngine()
        with patch("nfl_mcp.projections.get_projection_engine", return_value=engine), \
             patch.object(lo, "get_lineup_optimizer",
                          return_value=lo.LineupOptimizer(db=None, defense_analyzer=_defense())):
            out = await lo.get_start_sit_recommendation(
                player_name="Some WR", position="WR", team="MIA", opponent="NE",
                scoring="half_ppr", season=2026, week=6,
            )

        assert engine.calls == [{"scoring": "half_ppr", "season": 2026, "week": 6}]
        assert out["scoring"] == "half_ppr"

    @pytest.mark.asyncio
    async def test_week_reaches_the_model_not_just_the_response(self):
        """`week` used to be validated, echoed back, and otherwise discarded."""
        engine = _RecordingEngine()
        with patch("nfl_mcp.projections.get_projection_engine", return_value=engine), \
             patch.object(lo, "get_lineup_optimizer",
                          return_value=lo.LineupOptimizer(db=None, defense_analyzer=_defense())):
            await lo.get_roster_recommendations(
                [_player(), _player("Other WR")], week=9, season=2026, scoring="0.5"
            )

        assert len(engine.calls) == 2
        assert all(c == {"scoring": "0.5", "season": 2026, "week": 9} for c in engine.calls)

    @pytest.mark.asyncio
    async def test_compare_and_full_lineup_forward_it_too(self):
        engine = _RecordingEngine()
        with patch("nfl_mcp.projections.get_projection_engine", return_value=engine), \
             patch.object(lo, "get_lineup_optimizer",
                          return_value=lo.LineupOptimizer(db=None, defense_analyzer=_defense())):
            await lo.compare_players_for_slot(
                [_player(), _player("Other WR", "RB")],
                scoring="half_ppr", season=2026, week=6,
            )
            calls_after_compare = len(engine.calls)

            await lo.analyze_full_lineup(
                {"WR": [_player()], "BENCH": [_player("Bench WR")]},
                week=6, season=2026, scoring="half_ppr",
            )

        assert calls_after_compare == 2
        assert len(engine.calls) > calls_after_compare
        assert all(c == {"scoring": "half_ppr", "season": 2026, "week": 6}
                   for c in engine.calls)

    @pytest.mark.asyncio
    async def test_full_ppr_remains_the_default(self):
        engine = _RecordingEngine()
        with patch("nfl_mcp.projections.get_projection_engine", return_value=engine), \
             patch.object(lo, "get_lineup_optimizer",
                          return_value=lo.LineupOptimizer(db=None, defense_analyzer=_defense())):
            await lo.get_start_sit_recommendation(
                player_name="Some WR", position="WR", team="MIA", opponent="NE")

        assert engine.calls == [{"scoring": "ppr", "season": None, "week": None}]


class TestGoodGameThresholds:
    def test_rebased_to_the_league_scoring(self):
        full_low, full_high = _good_game_thresholds("WR", 1.0)
        half_low, half_high = _good_game_thresholds("WR", 0.5)

        assert (full_low, full_high) == (10, 16)
        assert half_low < full_low and half_high < full_high

    def test_positions_without_receptions_are_untouched(self):
        assert _good_game_thresholds("QB", 0.0) == _good_game_thresholds("QB", 1.0)
        assert _good_game_thresholds("K", 0.0) == _good_game_thresholds("K", 1.0)

    def test_an_unknown_position_still_gets_a_bar(self):
        assert _good_game_thresholds("LB", 1.0) == (10, 16)

    def test_half_ppr_projection_is_not_punished_against_a_full_ppr_bar(self):
        """14 points is a good WR week in half PPR, merely fine in full PPR."""
        optimizer = lo.LineupOptimizer(db=None, defense_analyzer=_defense())
        analysis = PlayerAnalysis(
            player_name="X", player_id="", position="WR", team="MIA",
            opponent="NE", projected_points=14.0,
        )

        _, _, reasoning_full = optimizer.calculate_confidence(analysis, ppr=1.0)
        _, _, reasoning_half = optimizer.calculate_confidence(analysis, ppr=0.5)

        assert not any("Strong projection" in r for r in reasoning_full)
        assert any("Strong projection" in r for r in reasoning_half)


class TestBandIsSurfaced:
    @pytest.mark.asyncio
    async def test_floor_and_ceiling_reach_the_caller(self):
        """The calibrated band was computed and then dropped from the response."""
        engine = _RecordingEngine()
        with patch("nfl_mcp.projections.get_projection_engine", return_value=engine), \
             patch.object(lo, "get_lineup_optimizer",
                          return_value=lo.LineupOptimizer(db=None, defense_analyzer=_defense())):
            out = await lo.get_start_sit_recommendation(
                player_name="Some WR", position="WR", team="MIA", opponent="NE")

        rec = out["recommendation"]
        assert rec["projected_points"] == 12.0
        assert rec["floor"] == 4.0
        assert rec["ceiling"] == 20.0
