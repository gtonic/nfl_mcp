"""Test snap and usage field name mappings from Sleeper API."""
import pytest
from nfl_mcp.sleeper_tools import _fetch_week_player_snaps, _fetch_weekly_usage_stats


class TestSleeperFieldMappings:
    """Test that Sleeper API field names are correctly mapped."""
    
    @pytest.mark.asyncio
    async def test_snap_field_names_extracted(self, monkeypatch):
        """Test that snap data is extracted with correct field names."""
        # Mock the Sleeper API response with actual field names used by Sleeper
        mock_response_data = {
            "player_123": {
                "off_snp": 45,  # Sleeper uses off_snp, not off_snaps
                "tm_off_snp": 60,  # Team snaps
                "off_snp_pct": 75.0,  # Snap percentage
            },
            "player_456": {
                "off_snp": 30,
                "tm_off_snp": 60,
                # No snap_pct - should be calculated
            },
            "player_789": {
                # Legacy/alternate field names - should also work
                "snaps": 50,
                "team_snaps": 60,
                "snap_pct": 83.3,
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
        
        # Mock the environment variable to enable advanced enrich
        monkeypatch.setenv("NFL_MCP_ADVANCED_ENRICH", "1")
        
        # Import after setting env var
        from nfl_mcp import sleeper_tools
        monkeypatch.setattr("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", True)
        monkeypatch.setattr("nfl_mcp.sleeper_tools.create_http_client", mock_create_client)
        
        # Mock validation to always pass - it's imported inside the function
        def mock_validate(data, validator, name, allow_partial=True):
            return True
        monkeypatch.setattr("nfl_mcp.response_validation.validate_response_and_log", mock_validate)
        
        # Call the function
        result = await sleeper_tools._fetch_week_player_snaps(2024, 10)
        
        # Verify results
        assert len(result) == 3, "Should extract data for all 3 players"
        
        # Check player_123 - uses off_snp field names
        player_123 = next((r for r in result if r["player_id"] == "player_123"), None)
        assert player_123 is not None, "Player 123 should be extracted"
        assert player_123["snaps_offense"] == 45, "Should extract off_snp as snaps_offense"
        assert player_123["snaps_team_offense"] == 60, "Should extract tm_off_snp as snaps_team_offense"
        assert player_123["snap_pct"] == 75.0, "Should extract off_snp_pct as snap_pct"
        
        # Check player_456 - snap_pct should be None (not calculated in fetch, calculated in DB)
        player_456 = next((r for r in result if r["player_id"] == "player_456"), None)
        assert player_456 is not None, "Player 456 should be extracted"
        assert player_456["snaps_offense"] == 30
        assert player_456["snaps_team_offense"] == 60
        
        # Check player_789 - uses legacy field names
        player_789 = next((r for r in result if r["player_id"] == "player_789"), None)
        assert player_789 is not None, "Player 789 should be extracted"
        assert player_789["snaps_offense"] == 50
        assert player_789["snaps_team_offense"] == 60
        assert player_789["snap_pct"] == 83.3
    
    @pytest.mark.asyncio
    async def test_usage_field_names_extracted(self, monkeypatch):
        """Test that usage stats are extracted with correct field names."""
        mock_response_data = {
            "player_123": {
                "rec_tgt": 8,  # Sleeper uses rec_tgt for targets
                "routes_run": 25,  # Routes run
                "rec_tgt_rz": 2,  # Red zone targets
                "rush_att_rz": 1,  # Red zone rushes
                "off_snp": 40,  # Offensive snaps
                "tm_off_snp": 60,  # Team snaps
                "rec": 5,
                "rush_att": 3,
            },
            "player_456": {
                "rec_tgt": 10,
                "routes_run": 30,
                # No explicit RZ data
                "rec_td": 1,  # Should estimate RZ from TDs
                "rush_td": 0,
                "rec": 7,
                "rush_att": 0,
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
        
        # Mock validation - it's imported inside the function
        def mock_validate(data, validator, name, allow_partial=True):
            return True
        monkeypatch.setattr("nfl_mcp.response_validation.validate_response_and_log", mock_validate)
        
        # Call the function
        result = await sleeper_tools._fetch_weekly_usage_stats(2024, 10)
        
        # Verify results
        assert len(result) == 2, "Should extract data for 2 players"
        
        # Check player_123
        player_123 = next((r for r in result if r["player_id"] == "player_123"), None)
        assert player_123 is not None
        assert player_123["targets"] == 8, "Should extract rec_tgt as targets"
        assert player_123["routes"] == 25, "Should extract routes_run as routes"
        assert player_123["rz_touches"] == 3, "Should sum RZ targets and rushes"
        assert player_123["touches"] == 8, "Should sum receptions and rush attempts"
        
        # Check that snap_share is calculated correctly
        expected_snap_share = round((40 / 60) * 100, 1)
        assert player_123["snap_share"] == expected_snap_share, "Should calculate snap_share from off_snp/tm_off_snp"
        
        # Check player_456 - RZ estimated from TDs
        player_456 = next((r for r in result if r["player_id"] == "player_456"), None)
        assert player_456 is not None
        assert player_456["targets"] == 10
        assert player_456["routes"] == 30
        assert player_456["rz_touches"] == 1, "Should estimate RZ from TDs when no explicit RZ data"
