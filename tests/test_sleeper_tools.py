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
        assert result["error_type"] == "validation_error"  # Test new field
        
        # Test invalid week (too high)
        result = await func("test_league", 25)
        assert result["success"] is False
        assert "Week must be between" in result["error"]
        assert result["error_type"] == "validation_error"  # Test new field
    
    @pytest.mark.asyncio 
    async def test_get_transactions_parameter_validation(self):
        """Test that get_transactions validates round parameter correctly."""
        func = getattr(sleeper_tools, 'get_transactions')
        
        # Test invalid round (too low)
        result = await func("test_league", 0)
        assert result["success"] is False
        assert "Round must be between" in result["error"]
        assert result["error_type"] == "validation_error"  # Test new field
        
        # Test invalid round (too high)  
        result = await func("test_league", 20)
        assert result["success"] is False
        assert "Round must be between" in result["error"]
        assert result["error_type"] == "validation_error"  # Test new field
    
    @pytest.mark.asyncio
    async def test_get_trending_players_parameter_validation(self):
        """Test that get_trending_players validates parameters correctly."""
        func = getattr(sleeper_tools, 'get_trending_players')
        
        # Test invalid trend_type
        result = await func("invalid_type", 24, 25)
        assert result["success"] is False
        assert "trend_type must be one of" in result["error"]
        assert result["error_type"] == "validation_error"  # Test new field
    
    @pytest.mark.asyncio
    async def test_get_trending_players_with_dict_response(self):
        """Test that get_trending_players handles dict responses from API."""
        from unittest.mock import MagicMock
        
        func = getattr(sleeper_tools, 'get_trending_players')
        
        # Mock response that returns dict objects instead of string IDs
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {'player_id': '12345', 'name': 'Player 1'},
            {'player_id': '67890', 'name': 'Player 2'}
        ]
        mock_response.raise_for_status = MagicMock()
        
        # Mock the HTTP client  
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        
        with patch('nfl_mcp.sleeper_tools.create_http_client', return_value=mock_client):
            result = await func()
            assert result["success"] is True
            assert result["error"] is None
            assert result["count"] == 2
            assert len(result["trending_players"]) == 2
    
    @pytest.mark.asyncio
    async def test_get_trending_players_with_string_ids(self):
        """Test backward compatibility with string ID responses."""
        from unittest.mock import MagicMock
        
        func = getattr(sleeper_tools, 'get_trending_players')
        
        # Mock response with string IDs (original format)
        mock_response = MagicMock()
        mock_response.json.return_value = ['12345', '67890']
        mock_response.raise_for_status = MagicMock()
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        
        with patch('nfl_mcp.sleeper_tools.create_http_client', return_value=mock_client):
            result = await func()
            assert result["success"] is True
            assert result["error"] is None
            assert result["count"] == 2
            assert len(result["trending_players"]) == 2
    
    @pytest.mark.asyncio
    async def test_get_trending_players_with_mixed_formats(self):
        """Test handling of mixed response formats."""
        from unittest.mock import MagicMock
        
        func = getattr(sleeper_tools, 'get_trending_players')
        
        # Mock response with mixed formats
        mock_response = MagicMock()
        mock_response.json.return_value = [
            '12345',  # String ID
            {'player_id': '67890'},  # Dict with player_id
            {'id': '11111'},  # Dict with id
            {'name': 'Player without ID'},  # Dict without valid ID - should be skipped
        ]
        mock_response.raise_for_status = MagicMock()
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        
        with patch('nfl_mcp.sleeper_tools.create_http_client', return_value=mock_client):
            result = await func()
            assert result["success"] is True
            assert result["error"] is None
            assert result["count"] == 3  # Only valid IDs should be processed
            assert len(result["trending_players"]) == 3


class TestSleeperToolsIntegration:
    """Integration tests for sleeper tools in real server context."""
    
    @pytest.mark.asyncio
    async def test_trending_players_dict_response_integration(self):
        """Integration test to ensure dict responses work with server tooling."""
        from unittest.mock import MagicMock
        from nfl_mcp.server import create_app
        
        # This test simulates the real server environment where the function
        # is called through the MCP tool interface
        
        app = create_app()
        
        # Mock the HTTP response to return dict objects
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {'player_id': 'test123', 'name': 'Test Player'},
            {'id': 'test456', 'name': 'Another Player'}  # Different ID field
        ]
        mock_response.raise_for_status = MagicMock()
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        
        with patch('nfl_mcp.sleeper_tools.create_http_client', return_value=mock_client):
            # Import and call the function as the server would
            from nfl_mcp import sleeper_tools
            result = await sleeper_tools.get_trending_players("add", 24, 10)
            
            # Verify the fix works in integration context
            assert result["success"] is True
            assert result["error"] is None
            assert result["count"] == 2
            assert result["trend_type"] == "add"
            assert result["lookback_hours"] == 24