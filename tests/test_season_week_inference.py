"""Omitting season/week must not silently downgrade the projection.

It is the common case — an agent rarely knows the current NFL week — and it
dropped every projection to the positional-rank baseline: six static values per
position. Measured on the same player and week, 16.8 points instead of 31.1.
All differentiation then came from the matchup tier, which the engine's own
backtest rates at zero for WRs.
"""
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp.lineup_optimizer_tools import _resolve_season_week


def _analyzer():
    from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
    analyzer = DefenseRankingsAnalyzer(db=None)
    analyzer.fetch_defense_rankings = AsyncMock(return_value={})
    return analyzer


class TestResolveSeasonWeek:
    @pytest.mark.asyncio
    async def test_explicit_values_are_left_alone_and_not_flagged(self, monkeypatch):
        async def _boom():
            raise AssertionError("must not call NFL state when both are given")
        monkeypatch.setattr("nfl_mcp.nfl_tools.get_current_season_and_week", _boom)

        assert await _resolve_season_week(2026, 3) == (2026, 3, False)

    @pytest.mark.asyncio
    async def test_both_missing_are_inferred(self, monkeypatch):
        async def _state():
            return (2026, 5)
        monkeypatch.setattr("nfl_mcp.nfl_tools.get_current_season_and_week", _state)

        assert await _resolve_season_week(None, None) == (2026, 5, True)

    @pytest.mark.asyncio
    async def test_a_partially_given_pair_keeps_the_caller_value(self, monkeypatch):
        async def _state():
            return (2026, 5)
        monkeypatch.setattr("nfl_mcp.nfl_tools.get_current_season_and_week", _state)

        # Caller pinned the week; only the season is filled in.
        assert await _resolve_season_week(None, 9) == (2026, 9, True)
        assert await _resolve_season_week(2024, None) == (2024, 5, True)

    @pytest.mark.asyncio
    async def test_a_failing_lookup_degrades_instead_of_raising(self, monkeypatch):
        async def _boom():
            raise RuntimeError("sleeper down")
        monkeypatch.setattr("nfl_mcp.nfl_tools.get_current_season_and_week", _boom)

        assert await _resolve_season_week(None, None) == (None, None, False)


class TestToolsReportWhatTheyUsed:
    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        monkeypatch.setattr(lo, "get_lineup_optimizer",
                            lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                       defense_analyzer=_analyzer()))

        async def _state():
            return (2026, 4)
        monkeypatch.setattr("nfl_mcp.nfl_tools.get_current_season_and_week", _state)

    @pytest.mark.asyncio
    async def test_start_sit_reports_the_inferred_week(self):
        out = await lo.get_start_sit_recommendation(
            "X", "RB", "KC", "MIA", projected_points=12.0)
        assert (out["season"], out["week"], out["week_inferred"]) == (2026, 4, True)

    @pytest.mark.asyncio
    async def test_roster_recommendations_report_it(self):
        out = await lo.get_roster_recommendations(
            [{"name": "X", "position": "RB", "team": "KC", "opponent": "MIA",
              "projection": {"projected_points": 12.0}}])
        assert out["week_inferred"] is True
        assert out["week"] == 4

    @pytest.mark.asyncio
    async def test_compare_reports_it(self):
        out = await lo.compare_players_for_slot([
            {"name": "A", "position": "RB", "team": "KC", "opponent": "MIA",
             "projection": {"projected_points": 12.0}},
            {"name": "B", "position": "RB", "team": "KC", "opponent": "MIA",
             "projection": {"projected_points": 8.0}}])
        assert out["week_inferred"] is True

    @pytest.mark.asyncio
    async def test_full_lineup_reports_it(self):
        out = await lo.analyze_full_lineup({
            "RB": [{"name": "X", "team": "KC", "opponent": "MIA", "position": "RB",
                    "projection": {"projected_points": 12.0}}]})
        assert out["week_inferred"] is True

    @pytest.mark.asyncio
    async def test_an_explicit_week_is_not_reported_as_inferred(self):
        out = await lo.get_start_sit_recommendation(
            "X", "RB", "KC", "MIA", projected_points=12.0, season=2026, week=7)
        assert out["week_inferred"] is False
        assert out["week"] == 7


class TestBaseSourceIsVisible:
    @pytest.mark.asyncio
    async def test_the_baseline_behind_the_number_is_reported(self, monkeypatch):
        """`rank_bucket` is a static placeholder, not a read on the player."""
        class FakeEngine:
            async def project_many(self, players, **kwargs):
                return {"projections": [{
                    "projected_points": 12.0, "floor": 4.0, "ceiling": 20.0,
                    "breakdown": {"base_source": "rank_bucket"},
                }]}

        monkeypatch.setattr(lo, "get_lineup_optimizer",
                            lambda: lo.LineupOptimizer(db=None, auto_project=True,
                                                       defense_analyzer=_analyzer()))
        async def _state():
            return (2026, 2)
        monkeypatch.setattr("nfl_mcp.nfl_tools.get_current_season_and_week", _state)
        monkeypatch.setattr("nfl_mcp.projections.get_projection_engine",
                            lambda db=None: FakeEngine())

        out = await lo.get_start_sit_recommendation("X", "RB", "KC", "MIA")
        assert out["recommendation"]["base_source"] == "rank_bucket"
