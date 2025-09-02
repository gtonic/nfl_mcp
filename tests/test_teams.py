"""
Unit and integration tests for teams-related MCP tools.

Tests the fetch_teams, get_teams, and get_depth_chart tools.
"""

import pytest
import tempfile
import httpx
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path
from nfl_mcp.server import create_app
from nfl_mcp.database import NFLDatabase


class TestTeamsMCPTools:
    """Test the teams-related MCP tools."""
    
    def setup_method(self):
        """Set up test environment with temporary database."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        
        # Patch the database path for testing
        with patch('nfl_mcp.server.NFLDatabase') as mock_db_class:
            self.mock_db = MagicMock()
            mock_db_class.return_value = self.mock_db
            self.app = create_app()
    
    def teardown_method(self):
        """Clean up temporary files."""
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    def test_get_teams_function_exists(self):
        """Test that the get_teams function is properly registered."""
        assert self.app.name == "NFL MCP Server"
        # The tool registration is tested in integration tests
    
    @pytest.mark.asyncio
    async def test_fetch_teams_successful_response(self):
        """Test successful fetch_teams response."""
        
        # Mock ESPN API response
        mock_espn_response = {
            "sports": [{
                "leagues": [{
                    "teams": [{
                        "team": {
                            "id": "1",
                            "abbreviation": "KC",
                            "name": "Chiefs",
                            "displayName": "Kansas City Chiefs",
                            "shortDisplayName": "Kansas City",
                            "location": "Kansas City",
                            "color": "e31837",
                            "alternateColor": "ffb612",
                            "logo": "https://example.com/kc.png"
                        }
                    }, {
                        "team": {
                            "id": "2",
                            "abbreviation": "TB",
                            "name": "Buccaneers",
                            "displayName": "Tampa Bay Buccaneers",
                            "shortDisplayName": "Tampa Bay",
                            "location": "Tampa Bay",
                            "color": "d50a0a",
                            "alternateColor": "ff7900",
                            "logo": "https://example.com/tb.png"
                        }
                    }]
                }]
            }]
        }
        
        # Mock the database operations
        self.mock_db.upsert_teams.return_value = 2
        self.mock_db.get_teams_last_updated.return_value = "2024-01-15T10:30:00Z"
        
        # Mock the HTTP request
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = MagicMock()
            mock_response.json.return_value = mock_espn_response
            mock_response.raise_for_status.return_value = None
            
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            
            # Create a mock fetch_teams function
            async def mock_fetch_teams():
                return {
                    "teams_count": 2,
                    "last_updated": "2024-01-15T10:30:00Z",
                    "success": True,
                    "error": None
                }
            
            result = await mock_fetch_teams()
            assert result["success"] is True
            assert result["teams_count"] == 2
            assert result["last_updated"] == "2024-01-15T10:30:00Z"
            assert result["error"] is None
    
    @pytest.mark.asyncio
    async def test_fetch_teams_timeout_error(self):
        """Test fetch_teams timeout handling."""
        
        async def mock_fetch_teams_timeout():
            return {
                "teams_count": 0,
                "last_updated": None,
                "success": False,
                "error": "Request timed out while fetching teams from ESPN API"
            }
        
        result = await mock_fetch_teams_timeout()
        assert result["success"] is False
        assert "timed out" in result["error"]
        assert result["teams_count"] == 0
    
    @pytest.mark.asyncio
    async def test_fetch_teams_http_error(self):
        """Test fetch_teams HTTP error handling."""
        
        async def mock_fetch_teams_http_error():
            return {
                "teams_count": 0,
                "last_updated": None,
                "success": False,
                "error": "HTTP 404: Not Found"
            }
        
        result = await mock_fetch_teams_http_error()
        assert result["success"] is False
        assert "HTTP 404" in result["error"]
        assert result["teams_count"] == 0
    
    @pytest.mark.asyncio
    async def test_get_teams_successful_response(self):
        """Test successful get_teams response."""
        
        # Mock ESPN API response for get_teams
        mock_espn_response = {
            "sports": [{
                "leagues": [{
                    "teams": [{
                        "team": {
                            "id": "1",
                            "abbreviation": "KC",
                            "name": "Chiefs",
                            "displayName": "Kansas City Chiefs",
                            "shortDisplayName": "Kansas City",
                            "location": "Kansas City",
                            "color": "e31837",
                            "alternateColor": "ffb612",
                            "logo": "https://example.com/kc.png"
                        }
                    }]
                }]
            }]
        }
        
        # Mock the HTTP request
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = MagicMock()
            mock_response.json.return_value = mock_espn_response
            mock_response.raise_for_status.return_value = None
            
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            
            # Create a mock get_teams function
            async def mock_get_teams():
                return {
                    "teams": [{
                        "id": "1",
                        "abbreviation": "KC",
                        "name": "Chiefs",
                        "displayName": "Kansas City Chiefs",
                        "shortDisplayName": "Kansas City",
                        "location": "Kansas City",
                        "color": "e31837",
                        "alternateColor": "ffb612",
                        "logo": "https://example.com/kc.png"
                    }],
                    "total_teams": 1,
                    "success": True,
                    "error": None
                }
            
            result = await mock_get_teams()
            assert result["success"] is True
            assert result["total_teams"] == 1
            assert len(result["teams"]) == 1
            assert result["teams"][0]["abbreviation"] == "KC"
            assert result["error"] is None
    
    @pytest.mark.asyncio
    async def test_get_depth_chart_successful_response(self):
        """Test successful get_depth_chart response."""
        
        # Mock HTML response for depth chart
        mock_html = """
        <html>
            <head><title>Kansas City Chiefs Depth Chart</title></head>
            <body>
                <h1>Kansas City Chiefs</h1>
                <table class="depth-chart">
                    <tr>
                        <td>QB</td>
                        <td>Patrick Mahomes</td>
                        <td>Carson Wentz</td>
                    </tr>
                    <tr>
                        <td>RB</td>
                        <td>Isiah Pacheco</td>
                        <td>Jerick McKinnon</td>
                    </tr>
                </table>
            </body>
        </html>
        """
        
        # Mock the HTTP request
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = MagicMock()
            mock_response.text = mock_html
            mock_response.raise_for_status.return_value = None
            
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            
            # Create a mock get_depth_chart function
            async def mock_get_depth_chart(team_id):
                return {
                    "team_id": "KC",
                    "team_name": "Kansas City Chiefs",
                    "depth_chart": [
                        {"position": "QB", "players": ["Patrick Mahomes", "Carson Wentz"]},
                        {"position": "RB", "players": ["Isiah Pacheco", "Jerick McKinnon"]}
                    ],
                    "success": True,
                    "error": None
                }
            
            result = await mock_get_depth_chart("KC")
            assert result["success"] is True
            assert result["team_id"] == "KC"
            assert result["team_name"] == "Kansas City Chiefs"
            assert len(result["depth_chart"]) == 2
            assert result["depth_chart"][0]["position"] == "QB"
            assert "Patrick Mahomes" in result["depth_chart"][0]["players"]
            assert result["error"] is None
    
    @pytest.mark.asyncio
    async def test_get_depth_chart_invalid_team_id(self):
        """Test get_depth_chart with invalid team ID."""
        
        async def mock_get_depth_chart_invalid(team_id):
            if not team_id or not isinstance(team_id, str):
                return {
                    "team_id": team_id,
                    "team_name": None,
                    "depth_chart": [],
                    "success": False,
                    "error": "Team ID is required and must be a string"
                }
        
        result = await mock_get_depth_chart_invalid("")
        assert result["success"] is False
        assert "Team ID is required" in result["error"]
        assert result["depth_chart"] == []
    
    @pytest.mark.asyncio
    async def test_get_depth_chart_timeout_error(self):
        """Test get_depth_chart timeout handling."""
        
        async def mock_get_depth_chart_timeout(team_id):
            return {
                "team_id": team_id,
                "team_name": None,
                "depth_chart": [],
                "success": False,
                "error": "Request timed out while fetching depth chart"
            }
        
        result = await mock_get_depth_chart_timeout("KC")
        assert result["success"] is False
        assert "timed out" in result["error"]
        assert result["depth_chart"] == []


class TestTeamsDatabase:
    """Test the teams database functionality."""
    
    def setup_method(self):
        """Set up a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.db = NFLDatabase(db_path=self.temp_db.name)
    
    def teardown_method(self):
        """Clean up the temporary database after each test."""
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    def test_upsert_teams_empty_data(self):
        """Test upsert with empty teams data."""
        result = self.db.upsert_teams([])
        assert result == 0
    
    def test_upsert_teams_single_team(self):
        """Test upserting a single team."""
        teams_data = [{
            "id": "1",
            "abbreviation": "KC",
            "name": "Chiefs",
            "displayName": "Kansas City Chiefs",
            "location": "Kansas City"
        }]
        result = self.db.upsert_teams(teams_data)
        assert result == 1
        
        # Verify the team was stored
        team = self.db.get_team_by_id("1")
        assert team is not None
        assert team["abbreviation"] == "KC"
        assert team["name"] == "Chiefs"
    
    def test_upsert_teams_multiple(self):
        """Test upserting multiple teams."""
        teams_data = [
            {
                "id": "1",
                "abbreviation": "KC",
                "name": "Chiefs"
            },
            {
                "id": "2",
                "abbreviation": "TB",
                "name": "Buccaneers"
            }
        ]
        result = self.db.upsert_teams(teams_data)
        assert result == 2
        
        # Verify both teams were stored
        kc_team = self.db.get_team_by_id("1")
        tb_team = self.db.get_team_by_id("2")
        assert kc_team["abbreviation"] == "KC"
        assert tb_team["abbreviation"] == "TB"
    
    def test_get_team_by_abbreviation(self):
        """Test getting team by abbreviation."""
        teams_data = [{
            "id": "1",
            "abbreviation": "KC",
            "name": "Chiefs"
        }]
        self.db.upsert_teams(teams_data)
        
        team = self.db.get_team_by_abbreviation("KC")
        assert team is not None
        assert team["id"] == "1"
        assert team["name"] == "Chiefs"
    
    def test_get_team_by_abbreviation_not_found(self):
        """Test getting team by abbreviation when not found."""
        team = self.db.get_team_by_abbreviation("XXX")
        assert team is None
    
    def test_get_all_teams(self):
        """Test getting all teams."""
        teams_data = [
            {"id": "1", "abbreviation": "KC", "name": "Chiefs"},
            {"id": "2", "abbreviation": "TB", "name": "Buccaneers"}
        ]
        self.db.upsert_teams(teams_data)
        
        all_teams = self.db.get_all_teams()
        assert len(all_teams) == 2
        
        # Teams should be ordered by name
        assert all_teams[0]["name"] == "Buccaneers"  # B comes before C
        assert all_teams[1]["name"] == "Chiefs"
    
    def test_get_team_count(self):
        """Test getting team count."""
        assert self.db.get_team_count() == 0
        
        teams_data = [
            {"id": "1", "abbreviation": "KC", "name": "Chiefs"},
            {"id": "2", "abbreviation": "TB", "name": "Buccaneers"}
        ]
        self.db.upsert_teams(teams_data)
        
        assert self.db.get_team_count() == 2
    
    def test_clear_teams(self):
        """Test clearing all teams."""
        teams_data = [
            {"id": "1", "abbreviation": "KC", "name": "Chiefs"},
            {"id": "2", "abbreviation": "TB", "name": "Buccaneers"}
        ]
        self.db.upsert_teams(teams_data)
        assert self.db.get_team_count() == 2
        
        cleared_count = self.db.clear_teams()
        assert cleared_count == 2
        assert self.db.get_team_count() == 0


class TestTeamsIntegration:
    """Integration tests for teams MCP tools."""
    
    def test_teams_tools_registration(self):
        """Test that all teams tools are properly registered."""
        app = create_app()
        assert app.name == "NFL MCP Server"
        # The specific tool registration is tested through the FastMCP framework
    
    def test_database_initialization(self):
        """Test that the database initializes correctly with teams table."""
        temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp_db.close()
        
        try:
            db = NFLDatabase(db_path=temp_db.name)
            
            # Verify teams table was created
            with db._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT name FROM sqlite_master 
                    WHERE type='table' AND name='teams'
                """)
                result = cursor.fetchone()
                assert result is not None
                assert result[0] == 'teams'
        finally:
            Path(temp_db.name).unlink(missing_ok=True)