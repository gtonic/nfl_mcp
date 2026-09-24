"""
Tests for season defaults and year-agnostic functionality.

No tool hard-codes a season: an omitted season resolves to the current one via
the canonical ``week_context.current_season_week`` (live NFL state, last good
state, cached schedule, calendar), so an outage never lands on week 0.
"""
import inspect
from unittest.mock import AsyncMock, patch

import pytest

# What Sleeper's /state/nfl returns (string season, as the live feed does).
_LIVE_STATE = {"success": True, "nfl_state": {"season": "2026", "week": 3}}


class TestSeasonDefaults:
    """Season parameters default to None (the current season), never a literal year."""

    @pytest.mark.parametrize("module,name", [
        ("nfl_mcp.nfl_tools", "get_team_player_stats"),
        ("nfl_mcp.nfl_tools", "get_nfl_standings"),
        ("nfl_mcp.nfl_tools", "get_team_schedule"),
        ("nfl_mcp.nfl_tools", "get_league_leaders"),
        ("nfl_mcp.tool_registry", "get_team_player_stats"),
        ("nfl_mcp.tool_registry", "get_nfl_standings"),
        ("nfl_mcp.tool_registry", "get_team_schedule"),
        ("nfl_mcp.cbs_fantasy_tools", "get_cbs_projections"),
    ])
    def test_season_default_is_none(self, module, name):
        import importlib
        fn = getattr(importlib.import_module(module), name)
        default = inspect.signature(fn).parameters["season"].default
        assert default is None, f"{module}.{name}: hard-coded season default {default}"

    def test_no_literal_season_fallbacks(self):
        import re
        for path in ("nfl_mcp/nfl_tools.py", "nfl_mcp/tool_registry.py", "nfl_mcp/server.py"):
            with open(path) as f:
                content = f.read()
            assert not re.search(r"(\bor |\belse |None = |int = |season = )20[2-9]\d\b", content), path

    def test_get_current_season_and_week_exists(self):
        from nfl_mcp.nfl_tools import get_current_season_and_week
        assert inspect.iscoroutinefunction(get_current_season_and_week)

    @pytest.mark.asyncio
    async def test_get_current_season_and_week_reads_live_state(self, monkeypatch):
        from nfl_mcp import week_context as wc
        from nfl_mcp.nfl_tools import get_current_season_and_week
        monkeypatch.setattr(wc, "_last_state", None)
        with patch("nfl_mcp.sleeper_tools.get_nfl_state", AsyncMock(return_value=_LIVE_STATE)):
            assert await get_current_season_and_week() == (2026, 3)

    @pytest.mark.asyncio
    async def test_omitted_season_resolves_to_current(self, monkeypatch):
        from nfl_mcp import nfl_tools

        async def _state(db=None):
            return {"season": 2031, "week": 4, "source": "nfl_state"}
        monkeypatch.setattr("nfl_mcp.week_context.current_season_week", _state)
        assert await nfl_tools._season_or_current(None) == 2031
        assert await nfl_tools._season_or_current(2024) == 2024


class TestSeasonFallback:
    """An NFL-state outage falls back to the schedule/calendar, never week 0."""

    @pytest.mark.asyncio
    async def test_outage_never_returns_week_zero(self, monkeypatch):
        from nfl_mcp import week_context as wc
        from nfl_mcp.nfl_tools import get_current_season_and_week
        monkeypatch.setattr(wc, "_last_state", None)
        with patch("nfl_mcp.sleeper_tools.get_nfl_state",
                   AsyncMock(side_effect=RuntimeError("sleeper down"))):
            season, week = await get_current_season_and_week()
        assert (season, week) == wc.infer_from_calendar()
        assert week >= 1

    @pytest.mark.asyncio
    async def test_resolve_season_week_outage_uses_fallback_not_week_zero(self, monkeypatch):
        from nfl_mcp import week_context as wc
        monkeypatch.setattr(wc, "_last_state", None)
        with patch("nfl_mcp.sleeper_tools.get_nfl_state",
                   AsyncMock(side_effect=RuntimeError("sleeper down"))):
            season, week, inferred = await wc.resolve_season_week(None, None)
        assert inferred is True
        assert season == wc.infer_from_calendar()[0]
        assert week >= 1

    @pytest.mark.asyncio
    async def test_registry_helper_matches_canonical(self, monkeypatch):
        from nfl_mcp import tool_registry

        async def _state(db=None):
            return {"season": 2026, "week": 7, "source": "schedule"}
        monkeypatch.setattr("nfl_mcp.week_context.current_season_week", _state)
        assert await tool_registry._current_season_week() == (2026, 7)


class TestNoHardcoded2025:
    """Verify no hardcoded 2025 values remain in critical code paths."""

    def test_nfl_tools_no_2025_fallback(self):
        """Verify nfl_tools.py has no 2025 fallbacks."""
        with open('nfl_mcp/nfl_tools.py') as f:
            content = f.read()

        # Should not have "or 2025" patterns (which would be fallback defaults)
        assert "or 2025" not in content,             "Found 'or 2025' in nfl_tools.py - should be 'or 2026'"

        # Should not have "default: 2025" patterns in docstrings
        assert "default: 2025" not in content,             "Found 'default: 2025' in nfl_tools.py docstrings"

    def test_tool_registry_no_2025_fallback(self):
        """Verify tool_registry.py has no 2025 fallbacks."""
        with open('nfl_mcp/tool_registry.py') as f:
            content = f.read()

        # Should not have "else 2025" patterns
        assert "else 2025" not in content,             "Found 'else 2025' in tool_registry.py"

        # Should not have "default=2025" patterns
        assert "default=2025" not in content,             "Found 'default=2025' in tool_registry.py"

    def test_server_no_2025_default(self):
        """Verify server.py has no 2025 default."""
        with open('nfl_mcp/server.py') as f:
            content = f.read()

        # Should not have "else 2025" patterns
        assert "else 2025" not in content,             "Found 'else 2025' in server.py"


@pytest.mark.live
class TestAPIYearAgility:
    """Test that the server properly handles year transitions.

    These hit live external APIs and are skipped by default (run with
    ``--run-live``). The same guarantees are covered offline by the
    data-source contracts watchdog under ``evals/``.
    """

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
