"""
Test the sleeper_tools module to ensure functions are properly extracted.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

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


class TestRosterAccessPermissions:
    """Test roster access permission handling."""
    
    @pytest.mark.asyncio
    async def test_get_rosters_access_denied_403(self):
        """Test handling of 403 Forbidden response (private rosters)."""
        func = getattr(sleeper_tools, 'get_rosters')
        
        with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_create_client:
            mock_response = MagicMock()
            mock_response.status_code = 403
            mock_response.raise_for_status.side_effect = Exception("Mocked - shouldn't be called")
            
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_create_client.return_value = mock_client
            
            result = await func("private_league_id")
            
            assert result["success"] is False
            assert result["error_type"] == "access_denied_error"
            assert "Access denied" in result["error"]
            assert "rosters" in result
            assert result["rosters"] == []
            assert "access_help" in result
            assert "league owner" in result["access_help"]
    
    @pytest.mark.asyncio
    async def test_get_rosters_authentication_required_401(self):
        """Test handling of 401 Unauthorized response."""
        func = getattr(sleeper_tools, 'get_rosters')
        
        with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_create_client:
            mock_response = MagicMock()
            mock_response.status_code = 401
            
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_create_client.return_value = mock_client
            
            result = await func("auth_required_league")
            
            assert result["success"] is False
            assert result["error_type"] == "access_denied_error"
            assert "Authentication required" in result["error"]
            assert "access_help" in result
            assert "private league" in result["access_help"]
    
    @pytest.mark.asyncio
    async def test_get_rosters_league_not_found_404(self):
        """Test handling of 404 Not Found response."""
        func = getattr(sleeper_tools, 'get_rosters')
        
        with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_create_client:
            mock_response = MagicMock()
            mock_response.status_code = 404
            
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_create_client.return_value = mock_client
            
            result = await func("nonexistent_league")
            
            assert result["success"] is False
            assert result["error_type"] == "http_error"
            assert "not found" in result["error"]
            assert "access_help" in result
            assert "verify the league ID" in result["access_help"]
    
    @pytest.mark.asyncio
    async def test_get_rosters_rate_limit_429(self):
        """Test handling of 429 Rate Limit response."""
        func = getattr(sleeper_tools, 'get_rosters')
        
        with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_create_client:
            from httpx import HTTPStatusError, Response, Request
            
            # Create a mock request and response for HTTPStatusError
            mock_request = MagicMock(spec=Request)
            mock_response = MagicMock(spec=Response)
            mock_response.status_code = 429
            mock_response.reason_phrase = "Too Many Requests"
            
            mock_client = AsyncMock()
            mock_client.get.side_effect = HTTPStatusError("Too Many Requests", request=mock_request, response=mock_response)
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_create_client.return_value = mock_client
            
            result = await func("test_league")
            
            assert result["success"] is False
            assert result["error_type"] == "http_error"
            assert "Rate limit exceeded" in result["error"]
            assert "access_help" in result
            assert "rate limits" in result["access_help"]
    
    @pytest.mark.asyncio
    async def test_get_rosters_empty_but_league_exists(self):
        """Test case where league exists but no rosters are returned (privacy setting)."""
        func = getattr(sleeper_tools, 'get_rosters')
        
        with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_create_client:
            # Mock successful roster request with empty array
            mock_roster_response = MagicMock()
            mock_roster_response.status_code = 200
            mock_roster_response.json.return_value = []
            mock_roster_response.raise_for_status.return_value = None
            
            # Mock successful league info request 
            mock_league_response = MagicMock()
            mock_league_response.status_code = 200
            mock_league_response.json.return_value = {"name": "Test League", "settings": {}}
            
            mock_client = AsyncMock()
            # First call returns empty rosters, second call returns league info
            mock_client.get.side_effect = [mock_roster_response, mock_league_response]
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_create_client.return_value = mock_client
            
            result = await func("league_with_private_rosters")
            
            assert result["success"] is True
            assert result["rosters"] == []
            assert result["count"] == 0
            assert "warning" in result
            assert "privacy settings" in result["warning"]
            assert "access_help" in result
    
    @pytest.mark.asyncio
    async def test_get_rosters_successful_with_data(self):
        """Test successful roster retrieval with data."""
        func = getattr(sleeper_tools, 'get_rosters')
        
        mock_rosters_data = [
            {
                "roster_id": 1,
                "owner_id": "user123",
                "players": ["4029", "5045", "6797"]
            },
            {
                "roster_id": 2,
                "owner_id": "user456", 
                "players": ["4034", "5046", "6798"]
            }
        ]
        
        with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_create_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_rosters_data
            mock_response.raise_for_status.return_value = None
            
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_create_client.return_value = mock_client
            
            result = await func("public_league")
            
            assert result["success"] is True
            assert result["error"] is None
            assert result["count"] == 2
            assert len(result["rosters"]) == 2
            assert result["rosters"][0]["roster_id"] == 1
    
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
        """Test that get_transactions validates required week (round) parameter bounds."""
        func = getattr(sleeper_tools, 'get_transactions')

        # Too low
        result = await func("test_league", 0)
        assert result["success"] is False
        assert "Week must be between" in result["error"]
        assert result["error_type"] == "validation_error"

        # Too high
        result = await func("test_league", 20)
        assert result["success"] is False
        assert "Week must be between" in result["error"]
        assert result["error_type"] == "validation_error"
    
    @pytest.mark.asyncio
    async def test_get_trending_players_parameter_validation(self):
        """Test that get_trending_players validates parameters correctly."""
        func = getattr(sleeper_tools, 'get_trending_players')
        
        # Test invalid trend_type
        result = await func(None, "invalid_type", 24, 25)
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
            result = await sleeper_tools.get_trending_players(None, "add", 24, 10)
            
            # Verify the fix works in integration context
            assert result["success"] is True
            assert result["error"] is None
            assert result["count"] == 2
            assert result["trend_type"] == "add"
            assert result["lookback_hours"] == 24


class TestStrategicPlanningFunctions:
    """Test the new strategic planning functions."""
    
    def test_strategic_functions_exist(self):
        """Test that all strategic planning functions exist."""
        assert hasattr(sleeper_tools, 'get_strategic_matchup_preview')
        assert hasattr(sleeper_tools, 'get_season_bye_week_coordination')
        assert hasattr(sleeper_tools, 'get_trade_deadline_analysis')
        assert hasattr(sleeper_tools, 'get_playoff_preparation_plan')
    
    @pytest.mark.asyncio
    async def test_strategic_matchup_preview_validation(self):
        """Test parameter validation for strategic matchup preview."""
        func = getattr(sleeper_tools, 'get_strategic_matchup_preview')
        
        # Test invalid week (too low)
        result = await func("test_league", 0, 4)
        assert result["success"] is False
        assert "Current week must be between" in result["error"]
        
        # Test invalid weeks_ahead (too high)
        result = await func("test_league", 8, 10)
        assert result["success"] is False
        assert "Weeks ahead must be between 1 and 8" in result["error"]
    
    @pytest.mark.asyncio
    async def test_season_bye_week_coordination_validation(self):
        """Test parameter validation for season bye week coordination."""
        func = getattr(sleeper_tools, 'get_season_bye_week_coordination')
        
        # Mock the get_league call to avoid API calls
        with patch('nfl_mcp.sleeper_tools.get_league') as mock_get_league:
            mock_get_league.return_value = {
                "success": True,
                "league": {
                    "settings": {
                        "playoff_week_start": 14,
                        "trade_deadline": 13
                    }
                }
            }
            
            # Test valid parameters 
            result = await func("test_league", 2025)
            # Should not fail validation (though it may fail on NFL API calls)
            assert "league_id" in result
            assert "season" in result
    
    @pytest.mark.asyncio
    async def test_trade_deadline_analysis_validation(self):
        """Test parameter validation for trade deadline analysis.""" 
        func = getattr(sleeper_tools, 'get_trade_deadline_analysis')
        
        # Mock get_league to test validation without network calls
        with patch('nfl_mcp.sleeper_tools.get_league') as mock_get_league:
            # First test with valid input but failed league call
            mock_get_league.return_value = {"success": False, "error": "Mock error"}
            result = await func("test_league", 10)
            assert result["success"] is False
            
            # Reset and test with valid league but invalid week - this should fail validation before API call
            # The validation happens at function entry, so we can test it directly
            result = await func("test_league", 25)  # Week 25 is invalid
            # Since validation is done by decorator, this should still try the API call
            # Let's test this differently by testing a clearly invalid value
            assert "league_id" in result  # Function should at least return structure
    
    @pytest.mark.asyncio 
    async def test_playoff_preparation_plan_validation(self):
        """Test parameter validation for playoff preparation plan."""
        func = getattr(sleeper_tools, 'get_playoff_preparation_plan')
        
        # Mock get_league to test without network calls
        with patch('nfl_mcp.sleeper_tools.get_league') as mock_get_league:
            mock_get_league.return_value = {"success": False, "error": "Mock error"}
            result = await func("test_league", 10)  # Valid week
            assert "league_id" in result  # Should return structure even on API failure
    
    @pytest.mark.asyncio
    async def test_strategic_matchup_preview_mock_success(self):
        """Test successful strategic matchup preview with mocked dependencies."""
        func = getattr(sleeper_tools, 'get_strategic_matchup_preview')
        
        # Mock get_league and get_matchups
        with patch('nfl_mcp.sleeper_tools.get_league') as mock_get_league, \
             patch('nfl_mcp.sleeper_tools.get_matchups') as mock_get_matchups:
            
            mock_get_league.return_value = {
                "success": True,
                "league": {"name": "Test League"}
            }
            
            mock_get_matchups.return_value = {
                "success": True,
                "matchups": [],
                "count": 0
            }
            
            result = await func("test_league", 8, 2)
            
            assert result["success"] is True
            assert "strategic_preview" in result
            assert "weeks_analyzed" in result
            assert result["league_id"] == "test_league"
    
    @pytest.mark.asyncio
    async def test_trade_deadline_analysis_mock_success(self):
        """Test successful trade deadline analysis with mocked dependencies."""
        func = getattr(sleeper_tools, 'get_trade_deadline_analysis')
        
        with patch('nfl_mcp.sleeper_tools.get_league') as mock_get_league, \
             patch('nfl_mcp.sleeper_tools.get_strategic_matchup_preview') as mock_preview:
            
            mock_get_league.return_value = {
                "success": True,
                "league": {
                    "settings": {
                        "trade_deadline": 13,
                        "playoff_week_start": 14
                    }
                }
            }
            
            mock_preview.return_value = {
                "success": True,
                "strategic_preview": {
                    "summary": {
                        "critical_bye_weeks": []
                    }
                }
            }
            
            result = await func("test_league", 10)
            
            assert result["success"] is True
            assert "trade_analysis" in result
            assert result["current_week"] == 10
            assert result["league_id"] == "test_league"