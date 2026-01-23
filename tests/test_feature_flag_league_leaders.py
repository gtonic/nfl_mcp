import os
import pytest
from importlib import reload


async def _count_tools(app):
    # FastMCP 3.0: list_tools() is async and returns a list
    tools = await app.list_tools()
    return len(tools)


async def _tool_names(app):
    # FastMCP 3.0: list_tools() returns list of Tool objects
    tools = await app.list_tools()
    return set(tool.name for tool in tools)


@pytest.mark.asyncio
async def test_league_leaders_flag_disabled(monkeypatch):
    # Explicitly disable the feature flag
    monkeypatch.setenv('MCP_FEATURE_LEAGUE_LEADERS', '0')
    # Re-import config to recompute flag
    from nfl_mcp import config as cfg
    reload(cfg)
    # Rebuild tool registry module (feature conditional executed at import time)
    from nfl_mcp import tool_registry
    reload(tool_registry)
    # Build server app
    from nfl_mcp.server import create_app
    app = create_app()
    names = await _tool_names(app)
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
    names = await _tool_names(app)
    assert 'get_league_leaders' in names, 'league leaders tool should be registered when flag enabled'
