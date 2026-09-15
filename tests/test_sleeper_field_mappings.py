"""Test snap and usage field name mappings from Sleeper API."""
import pytest


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
        monkeypatch.setattr("nfl_mcp.sleeper_enrichment.ADVANCED_ENRICH_ENABLED", True)
        monkeypatch.setattr("nfl_mcp.sleeper_enrichment.create_http_client", mock_create_client)

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

        # Check player_456 - Sleeper ships no percentage, so it is derived from
        # the two counts. Leaving it None made every stored row NULL.
        player_456 = next((r for r in result if r["player_id"] == "player_456"), None)
        assert player_456 is not None, "Player 456 should be extracted"
        assert player_456["snaps_offense"] == 30
        assert player_456["snaps_team_offense"] == 60
        assert player_456["snap_pct"] == 50.0, "Should derive snap_pct from off_snp/tm_off_snp"

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
        monkeypatch.setattr("nfl_mcp.sleeper_enrichment.ADVANCED_ENRICH_ENABLED", True)
        monkeypatch.setattr("nfl_mcp.sleeper_enrichment.create_http_client", mock_create_client)

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

    @pytest.mark.asyncio
    async def test_air_yards_uses_sleeper_field_name(self, monkeypatch):
        """Sleeper ships `rec_air_yd` (singular); the plural never matched."""
        mock_response_data = {
            "player_123": {"rec_tgt": 6, "rec_air_yd": 79, "rec": 5, "rush_att": 0},
            # Legacy/alternate spellings must keep working.
            "player_456": {"rec_tgt": 4, "rec_air_yds": 40, "rec": 3, "rush_att": 0},
            "player_789": {"rec_tgt": 2, "air_yards": 12, "rec": 1, "rush_att": 0},
        }

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

        monkeypatch.setenv("NFL_MCP_ADVANCED_ENRICH", "1")
        from nfl_mcp import sleeper_tools
        monkeypatch.setattr("nfl_mcp.sleeper_enrichment.ADVANCED_ENRICH_ENABLED", True)
        monkeypatch.setattr(
            "nfl_mcp.sleeper_enrichment.create_http_client", lambda: MockClient()
        )
        monkeypatch.setattr(
            "nfl_mcp.response_validation.validate_response_and_log",
            lambda data, validator, name, allow_partial=True: True,
        )

        result = await sleeper_tools._fetch_weekly_usage_stats(2026, 1)
        by_id = {r["player_id"]: r for r in result}

        assert by_id["player_123"]["air_yards"] == 79, "Should read rec_air_yd"
        assert by_id["player_456"]["air_yards"] == 40, "Should still read rec_air_yds"
        assert by_id["player_789"]["air_yards"] == 12, "Should still read air_yards"


class TestAdvancedEnrichFlag:
    """The flag must be readable after `.env` is loaded, not only at import."""

    def test_env_set_after_import_is_honoured(self, monkeypatch):
        from nfl_mcp import sleeper_enrichment as se

        # Simulate the real startup order: module imported with the flag unset
        # (server.py imports the tool registry before it loads `.env`), then the
        # variable appears in the environment.
        monkeypatch.setattr(se, "ADVANCED_ENRICH_ENABLED", False)
        monkeypatch.delenv("NFL_MCP_ADVANCED_ENRICH", raising=False)
        assert se.advanced_enrich_enabled() is False

        monkeypatch.setenv("NFL_MCP_ADVANCED_ENRICH", "1")
        assert se.advanced_enrich_enabled() is True

    def test_module_attribute_still_overrides(self, monkeypatch):
        from nfl_mcp import sleeper_enrichment as se

        monkeypatch.delenv("NFL_MCP_ADVANCED_ENRICH", raising=False)
        monkeypatch.setattr(se, "ADVANCED_ENRICH_ENABLED", True)
        assert se.advanced_enrich_enabled() is True
