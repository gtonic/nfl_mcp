"""
Tests for season defaults and year-agnostic functionality.

This module verifies that the NFL MCP Server correctly uses 2026 as the
default season across all tools and properly handles season detection.
"""
import pytest
import inspect
from unittest.mock import patch, AsyncMock


class TestSeasonDefaults:
    """Test that all season defaults are 2026, not 2025."""

    def test_nfl_tools_team_player_stats_default_season(self):
        """Verify get_team_player_stats defaults to season=2026."""
        from nfl_mcp.nfl_tools import get_team_player_stats
        sig = inspect.signature(get_team_player_stats)
        season_param = sig.parameters['season']
        assert season_param.default == 2026,             f"Expected default season 2026, got {season_param.default}"

    def test_nfl_tools_standings_default_season(self):
        """Verify get_nfl_standings defaults to season=2026."""
        from nfl_mcp.nfl_tools import get_nfl_standings
        sig = inspect.signature(get_nfl_standings)
        season_param = sig.parameters['season']
        assert season_param.default == 2026,             f"Expected default season 2026, got {season_param.default}"

    def test_nfl_tools_schedule_default_season(self):
        """Verify get_team_schedule defaults to season=2026."""
        from nfl_mcp.nfl_tools import get_team_schedule
        sig = inspect.signature(get_team_schedule)
        season_param = sig.parameters['season']
        assert season_param.default == 2026,             f"Expected default season 2026, got {season_param.default}"

    def test_nfl_tools_league_leaders_default_season(self):
        """Verify get_league_leaders defaults to season=2026."""
        from nfl_mcp.nfl_tools import get_league_leaders
        sig = inspect.signature(get_league_leaders)
        season_param = sig.parameters['season']
        assert season_param.default == 2026,             f"Expected default season 2026, got {season_param.default}"

    def test_tool_registry_team_player_stats_default_season(self):
        """Verify tool_registry get_team_player_stats defaults to season=2026."""
        from nfl_mcp.tool_registry import get_team_player_stats
        sig = inspect.signature(get_team_player_stats)
        season_param = sig.parameters['season']
        assert season_param.default == 2026,             f"Expected default season 2026, got {season_param.default}"

    def test_tool_registry_standings_default_season(self):
        """Verify tool_registry get_nfl_standings defaults to season=2026."""
        from nfl_mcp.tool_registry import get_nfl_standings
        sig = inspect.signature(get_nfl_standings)
        season_param = sig.parameters['season']
        assert season_param.default == 2026,             f"Expected default season 2026, got {season_param.default}"

    def test_tool_registry_schedule_default_season(self):
        """Verify tool_registry get_team_schedule defaults to season=2026."""
        from nfl_mcp.tool_registry import get_team_schedule
        sig = inspect.signature(get_team_schedule)
        season_param = sig.parameters['season']
        assert season_param.default == 2026,             f"Expected default season 2026, got {season_param.default}"

    def test_cbs_fantasy_projections_default_season(self):
        """Verify cbs_fantasy get_cbs_projections defaults to season=2026."""
        from nfl_mcp.cbs_fantasy_tools import get_cbs_projections
        sig = inspect.signature(get_cbs_projections)
        season_param = sig.parameters['season']
        assert season_param.default == 2026,             f"Expected default season 2026, got {season_param.default}"

    def test_sleeper_bye_week_coordination_default_season(self):
        """Verify sleeper bye_week_coordination defaults to season=2026."""
        from nfl_mcp.sleeper_tools import get_season_bye_week_coordination
        sig = inspect.signature(get_season_bye_week_coordination)
        season_param = sig.parameters['season']
        assert season_param.default == 2026,             f"Expected default season 2026, got {season_param.default}"

    def test_get_current_season_and_week_exists(self):
        """Verify get_current_season_and_week function exists."""
        from nfl_mcp.nfl_tools import get_current_season_and_week
        assert inspect.iscoroutinefunction(get_current_season_and_week),             "get_current_season_and_week should be async"

    @pytest.mark.asyncio
    async def test_get_current_season_and_week_returns_tuple(self):
        """Verify get_current_season_and_week returns (season, week) tuple."""
        from nfl_mcp.nfl_tools import get_current_season_and_week
        result = await get_current_season_and_week()
        assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
        assert len(result) == 2, f"Expected 2 elements, got {len(result)}"
        season, week = result
        assert season is None or isinstance(season, int),             f"Season should be int or None, got {type(season)}"
        assert week is None or isinstance(week, int),             f"Week should be int or None, got {type(week)}"

    @pytest.mark.asyncio
    async def test_get_current_season_and_week_returns_2026(self):
        """Verify get_current_season_and_week returns 2026 as current season."""
        from nfl_mcp.nfl_tools import get_current_season_and_week
        result = await get_current_season_and_week()
        season, _ = result
        assert season == 2026, f"Expected season 2026, got {season}"


class TestSeasonFallback:
    """Test fallback behavior when season is None."""

    @pytest.mark.asyncio
    async def test_get_current_season_and_week_handles_api_failure(self):
        """Verify get_current_season_and_week handles API failures gracefully."""
        from nfl_mcp.nfl_tools import get_current_season_and_week
        
        # Mock the sleeper_tools import to raise an exception
        import sys
        original_module = sys.modules.get('nfl_mcp.sleeper_tools')
        try:
            # Remove from sys.modules to force reimport
            if 'nfl_mcp.sleeper_tools' in sys.modules:
                del sys.modules['nfl_mcp.sleeper_tools']
            
            # Mock the import to raise exception
            with patch.dict('sys.modules', {'nfl_mcp.sleeper_tools': None}):
                # This will fail to import, but should still return fallback
                result = await get_current_season_and_week()
                season, week = result
                # Should fallback to current year when API fails
                import datetime
                assert season == datetime.datetime.now().year
        finally:
            # Restore original module
            if original_module is not None:
                sys.modules['nfl_mcp.sleeper_tools'] = original_module


class TestNoHardcoded2025:
    """Verify no hardcoded 2025 values remain in critical code paths."""

    def test_nfl_tools_no_2025_fallback(self):
        """Verify nfl_tools.py has no 2025 fallbacks."""
        with open('nfl_mcp/nfl_tools.py', 'r') as f:
            content = f.read()
        
        # Should not have "or 2025" patterns (which would be fallback defaults)
        assert "or 2025" not in content,             "Found 'or 2025' in nfl_tools.py - should be 'or 2026'"
        
        # Should not have "default: 2025" patterns in docstrings
        assert "default: 2025" not in content,             "Found 'default: 2025' in nfl_tools.py docstrings"

    def test_tool_registry_no_2025_fallback(self):
        """Verify tool_registry.py has no 2025 fallbacks."""
        with open('nfl_mcp/tool_registry.py', 'r') as f:
            content = f.read()
        
        # Should not have "else 2025" patterns
        assert "else 2025" not in content,             "Found 'else 2025' in tool_registry.py"
        
        # Should not have "default=2025" patterns
        assert "default=2025" not in content,             "Found 'default=2025' in tool_registry.py"

    def test_server_no_2025_default(self):
        """Verify server.py has no 2025 default."""
        with open('nfl_mcp/server.py', 'r') as f:
            content = f.read()
        
        # Should not have "else 2025" patterns
        assert "else 2025" not in content,             "Found 'else 2025' in server.py"


class TestAPIYearAgility:
    """Test that the server properly handles year transitions."""

    @pytest.mark.asyncio
    async def test_sleeper_state_returns_2026(self):
        """Verify Sleeper API returns 2026 as current season."""
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get('https://api.sleeper.app/v1/state/nfl', timeout=10)
            data = resp.json()
            assert data.get('season') == '2026' or data.get('season') == 2026

    @pytest.mark.asyncio
    async def test_espn_api_accepts_2026(self):
        """Verify ESPN API accepts 2026 as season parameter."""
        import httpx
        async with httpx.AsyncClient() as client:
            # ESPN standings endpoint should accept 2026
            resp = await client.get(
                'https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/2026/types/2/standings',
                timeout=10
            )
            # Should return 200 even if empty (season not started yet)
            assert resp.status_code == 200
