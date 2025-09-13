import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from nfl_mcp import sleeper_tools


@pytest.mark.asyncio
async def test_fantasy_context_basic_all_sections():
    with patch('nfl_mcp.sleeper_tools.get_league') as mock_league, \
         patch('nfl_mcp.sleeper_tools.get_rosters') as mock_rosters, \
         patch('nfl_mcp.sleeper_tools.get_league_users') as mock_users, \
         patch('nfl_mcp.sleeper_tools.get_nfl_state') as mock_state, \
         patch('nfl_mcp.sleeper_tools.get_matchups') as mock_matchups, \
         patch('nfl_mcp.sleeper_tools.get_transactions') as mock_tx:
        mock_league.return_value = {"success": True, "league": {"league_id": "L1"}}
        mock_rosters.return_value = {"success": True, "rosters": []}
        mock_users.return_value = {"success": True, "users": []}
        mock_state.return_value = {"success": True, "nfl_state": {"week": 9}}
        mock_matchups.return_value = {"success": True, "matchups": []}
        mock_tx.return_value = {"success": True, "transactions": []}
        result = await sleeper_tools.get_fantasy_context("L1")
        assert result["success"] is True
        assert result["auto_week_inferred"] is True
        assert result["week"] == 9
        ctx = result["context"]
        assert set(["league", "rosters", "users", "matchups", "transactions"]).issubset(ctx.keys())


@pytest.mark.asyncio
async def test_fantasy_context_subset_and_explicit_week():
    with patch('nfl_mcp.sleeper_tools.get_league') as mock_league, \
         patch('nfl_mcp.sleeper_tools.get_rosters') as mock_rosters:
        mock_league.return_value = {"success": True, "league": {"league_id": "L1"}}
        mock_rosters.return_value = {"success": True, "rosters": []}
        result = await sleeper_tools.get_fantasy_context("L1", week=5, include="league,rosters")
        assert result["success"] is True
        assert result["auto_week_inferred"] is False
        assert result["week"] == 5
        ctx = result["context"]
        assert set(ctx.keys()) == {"league", "rosters"}


@pytest.mark.asyncio
async def test_transactions_auto_inference_failure():
    # Force nfl state failure path
    with patch('nfl_mcp.sleeper_tools.get_nfl_state') as mock_state:
        mock_state.return_value = {"success": False}
        # Direct call to lower-level function to test validation path when inference fails
        result = await sleeper_tools.get_transactions("L1")
        assert result["success"] is False
        assert "infer" in (result.get("error") or "").lower()