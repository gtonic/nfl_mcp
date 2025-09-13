import os
import pytest
from importlib import reload


def _count_tools(app):
    # FastMCP stores tools in app._tool_manager._tools
    return len(getattr(app._tool_manager, '_tools'))  # type: ignore[attr-defined]


def _tool_names(app):
    return set(getattr(app._tool_manager, '_tools').keys())  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_league_leaders_flag_disabled(monkeypatch):
    # Ensure flag is unset/disabled
    monkeypatch.delenv('MCP_FEATURE_LEAGUE_LEADERS', raising=False)
    # Re-import config to recompute flag
    from nfl_mcp import config as cfg
    reload(cfg)
    # Rebuild tool registry module (feature conditional executed at import time)
    from nfl_mcp import tool_registry
    reload(tool_registry)
    # Build server app
    from nfl_mcp.server import create_app
    app = create_app()
    names = _tool_names(app)
    assert 'get_league_leaders' not in names, 'league leaders tool should not be registered when flag disabled'


@pytest.mark.asyncio
async def test_league_leaders_flag_enabled(monkeypatch):
    monkeypatch.setenv('MCP_FEATURE_LEAGUE_LEADERS', '1')
    from nfl_mcp import config as cfg
    reload(cfg)
    from nfl_mcp import tool_registry
    reload(tool_registry)
    from nfl_mcp.server import create_app
    app = create_app()
    names = _tool_names(app)
    assert 'get_league_leaders' in names, 'league leaders tool should be registered when flag enabled'
