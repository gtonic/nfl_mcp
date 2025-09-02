"""
Test the sleeper_tools module to ensure functions are properly extracted.
"""

import pytest
from unittest.mock import AsyncMock, patch

from nfl_mcp import sleeper_tools


class TestSleeperToolsModule:
    """Test the sleeper_tools module functionality."""
    
    def test_module_imports_successfully(self):
        """Test that the sleeper_tools module can be imported."""
        assert hasattr(sleeper_tools, 'get_league')
        assert hasattr(sleeper_tools, 'get_rosters')
        assert hasattr(sleeper_tools, 'get_league_users')
        assert hasattr(sleeper_tools, 'get_matchups')
        assert hasattr(sleeper_tools, 'get_playoff_bracket')
        assert hasattr(sleeper_tools, 'get_transactions')
        assert hasattr(sleeper_tools, 'get_traded_picks')
        assert hasattr(sleeper_tools, 'get_nfl_state')
        assert hasattr(sleeper_tools, 'get_trending_players')
    
    @pytest.mark.asyncio
    async def test_get_league_function_exists(self):
        """Test that get_league function exists and has correct signature."""
        func = getattr(sleeper_tools, 'get_league')
        assert callable(func)
        
        # Test with mock to avoid actual API call
        with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_client:
            mock_response = AsyncMock()
            mock_response.json.return_value = {"name": "Test League"}
            mock_response.raise_for_status.return_value = None
            
            mock_context = AsyncMock()
            mock_context.__aenter__.return_value.get.return_value = mock_response
            mock_client.return_value = mock_context
            
            result = await func("test_league_id")
            assert "league" in result
            assert "success" in result
            assert "error" in result
    
    @pytest.mark.asyncio
    async def test_get_matchups_parameter_validation(self):
        """Test that get_matchups validates week parameter correctly."""
        func = getattr(sleeper_tools, 'get_matchups')
        
        # Test invalid week (too low)
        result = await func("test_league", 0)
        assert result["success"] is False
        assert "Week must be between" in result["error"]
        
        # Test invalid week (too high)
        result = await func("test_league", 25)
        assert result["success"] is False
        assert "Week must be between" in result["error"]
    
    @pytest.mark.asyncio 
    async def test_get_transactions_parameter_validation(self):
        """Test that get_transactions validates round parameter correctly."""
        func = getattr(sleeper_tools, 'get_transactions')
        
        # Test invalid round (too low)
        result = await func("test_league", 0)
        assert result["success"] is False
        assert "Round must be between" in result["error"]
        
        # Test invalid round (too high)  
        result = await func("test_league", 20)
        assert result["success"] is False
        assert "Round must be between" in result["error"]
    
    @pytest.mark.asyncio
    async def test_get_trending_players_parameter_validation(self):
        """Test that get_trending_players validates parameters correctly."""
        func = getattr(sleeper_tools, 'get_trending_players')
        
        # Test invalid trend_type
        result = await func("invalid_type", 24, 25)
        assert result["success"] is False
        assert "trend_type must be one of" in result["error"]