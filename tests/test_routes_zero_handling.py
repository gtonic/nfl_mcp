"""Test that zero routes values are properly handled."""
import pytest
from nfl_mcp.sleeper_tools import _fetch_weekly_usage_stats


class TestRoutesZeroHandling:
    """Test that routes=0 is properly distinguished from routes=None."""
    
    @pytest.mark.asyncio
    async def test_routes_zero_value_preserved(self, monkeypatch):
        """Test that routes=0 is preserved and not treated as None."""
        mock_response_data = {
            "blocking_rb": {
                # Blocking RB who runs 0 routes
                "rec_tgt": 0,
                "routes_run": 0,  # This should be preserved as 0, not treated as None
                "rush_att": 15,
                "rec": 0,
            },
            "receiver_with_zero": {
                # Receiver with explicitly 0 routes (injured early?)
                "rec_tgt": 2,
                "routes": 0,  # Fallback field with 0
                "rec": 1,
                "rush_att": 0,
            },
            "player_no_routes_field": {
                # Player where routes field doesn't exist at all
                "rec_tgt": 5,
                "rec": 3,
                "rush_att": 0,
                # No routes_run or routes field
            }
        }
        
        # Mock the HTTP client
        class MockResponse:
            status_code = 200
            def json(self):
                return mock_response_data
        
        class MockClient:
            async def get(self, url, **kwargs):
                return MockResponse()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        
        def mock_create_client():
            return MockClient()
        
        # Mock the environment variable
        monkeypatch.setenv("NFL_MCP_ADVANCED_ENRICH", "1")
        
        # Import after setting env var
        from nfl_mcp import sleeper_tools
        monkeypatch.setattr("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", True)
        monkeypatch.setattr("nfl_mcp.sleeper_tools.create_http_client", mock_create_client)
        
        # Mock validation
        def mock_validate(data, validator, name, allow_partial=True):
            return True
        monkeypatch.setattr("nfl_mcp.response_validation.validate_response_and_log", mock_validate)
        
        # Call the function
        result = await sleeper_tools._fetch_weekly_usage_stats(2024, 10)
        
        # Verify results
        assert len(result) == 3, "Should extract data for all 3 players"
        
        # Check blocking_rb - routes should be 0, not None
        blocking_rb = next((r for r in result if r["player_id"] == "blocking_rb"), None)
        assert blocking_rb is not None, "Blocking RB should be extracted"
        assert blocking_rb["routes"] == 0, "Routes should be 0, not None"
        assert blocking_rb["targets"] == 0, "Targets should be 0"
        
        # Check receiver_with_zero - routes from fallback field should be 0
        receiver = next((r for r in result if r["player_id"] == "receiver_with_zero"), None)
        assert receiver is not None, "Receiver should be extracted"
        assert receiver["routes"] == 0, "Routes from fallback field should be 0, not None"
        assert receiver["targets"] == 2
        
        # Check player_no_routes_field - routes should be None
        no_routes = next((r for r in result if r["player_id"] == "player_no_routes_field"), None)
        assert no_routes is not None, "Player without routes field should be extracted"
        assert no_routes["routes"] is None, "Routes should be None when field doesn't exist"
        assert no_routes["targets"] == 5
    
    @pytest.mark.asyncio
    async def test_routes_additional_field_names(self, monkeypatch):
        """Test that additional route field names are tried."""
        mock_response_data = {
            "player_rec_routes": {
                "rec_tgt": 8,
                "rec_routes": 25,  # Alternative field name
                "rec": 5,
            },
            "player_pass_routes": {
                "rec_tgt": 10,
                "pass_routes": 30,  # Another alternative
                "rec": 7,
            },
            "player_receiving_routes": {
                "rec_tgt": 6,
                "receiving_routes": 22,  # Yet another alternative
                "rec": 4,
            }
        }
        
        # Mock the HTTP client
        class MockResponse:
            status_code = 200
            def json(self):
                return mock_response_data
        
        class MockClient:
            async def get(self, url, **kwargs):
                return MockResponse()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        
        def mock_create_client():
            return MockClient()
        
        # Mock the environment variable
        monkeypatch.setenv("NFL_MCP_ADVANCED_ENRICH", "1")
        
        # Import after setting env var
        from nfl_mcp import sleeper_tools
        monkeypatch.setattr("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", True)
        monkeypatch.setattr("nfl_mcp.sleeper_tools.create_http_client", mock_create_client)
        
        # Mock validation
        def mock_validate(data, validator, name, allow_partial=True):
            return True
        monkeypatch.setattr("nfl_mcp.response_validation.validate_response_and_log", mock_validate)
        
        # Call the function
        result = await sleeper_tools._fetch_weekly_usage_stats(2024, 10)
        
        # Verify all alternative field names work
        assert len(result) == 3, "Should extract data for all 3 players"
        
        player_1 = next((r for r in result if r["player_id"] == "player_rec_routes"), None)
        assert player_1 is not None
        assert player_1["routes"] == 25, "Should extract rec_routes"
        
        player_2 = next((r for r in result if r["player_id"] == "player_pass_routes"), None)
        assert player_2 is not None
        assert player_2["routes"] == 30, "Should extract pass_routes"
        
        player_3 = next((r for r in result if r["player_id"] == "player_receiving_routes"), None)
        assert player_3 is not None
        assert player_3["routes"] == 22, "Should extract receiving_routes"
