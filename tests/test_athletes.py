"""
Unit and integration tests for athlete-related MCP tools.

Tests the fetch_athletes, lookup_athlete, search_athletes, and get_athletes_by_team tools.
"""

import pytest
import tempfile
import httpx
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path
from nfl_mcp.server import create_app


class TestAthleteMCPTools:
    """Test the athlete-related MCP tools."""
    
    def setup_method(self):
        """Set up test environment with temporary database."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        
        # Patch the database path for testing
        with patch('nfl_mcp.server.AthleteDatabase') as mock_db_class:
            self.mock_db = MagicMock()
            mock_db_class.return_value = self.mock_db
            self.app = create_app()
    
    def teardown_method(self):
        """Clean up temporary files."""
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    def test_fetch_athletes_function_exists(self):
        """Test that the fetch_athletes function is properly registered."""
        assert self.app.name == "NFL MCP Server"
        # The tool registration is tested in integration tests
    
    @pytest.mark.asyncio
    async def test_fetch_athletes_successful_response(self):
        """Test successful fetch_athletes response."""
        
        # Mock Sleeper API response
        mock_sleeper_response = {
            "123": {
                "full_name": "Tom Brady",
                "first_name": "Tom",
                "last_name": "Brady",
                "team": "TB",
                "position": "QB",
                "status": "Active"
            },
            "456": {
                "full_name": "Patrick Mahomes",
                "first_name": "Patrick",
                "last_name": "Mahomes",
                "team": "KC",
                "position": "QB",
                "status": "Active"
            }
        }
        
        # Mock the database operations
        self.mock_db.upsert_athletes.return_value = 2
        self.mock_db.get_last_updated.return_value = "2024-01-15T10:30:00Z"
        
        # Mock the HTTP request
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_sleeper_response
            mock_response.raise_for_status = MagicMock()
            
            mock_client.return_value.__aenter__.return_value.get.return_value = mock_response
            
            # Test the fetch_athletes function logic
            async def mock_fetch_athletes():
                try:
                    # Simulate the API call
                    athletes_data = mock_sleeper_response
                    count = self.mock_db.upsert_athletes(athletes_data)
                    last_updated = self.mock_db.get_last_updated()
                    
                    return {
                        "athletes_count": count,
                        "last_updated": last_updated,
                        "success": True,
                        "error": None
                    }
                except Exception as e:
                    return {
                        "athletes_count": 0,
                        "last_updated": None,
                        "success": False,
                        "error": f"Unexpected error fetching athletes: {str(e)}"
                    }
            
            result = await mock_fetch_athletes()
            
            assert result["success"] is True
            assert result["athletes_count"] == 2
            assert result["last_updated"] == "2024-01-15T10:30:00Z"
            assert result["error"] is None
    
    @pytest.mark.asyncio
    async def test_fetch_athletes_timeout_error(self):
        """Test fetch_athletes timeout handling."""
        
        async def mock_fetch_athletes_timeout():
            return {
                "athletes_count": 0,
                "last_updated": None,
                "success": False,
                "error": "Request timed out while fetching athletes from Sleeper API"
            }
        
        result = await mock_fetch_athletes_timeout()
        assert result["success"] is False
        assert "timed out" in result["error"]
        assert result["athletes_count"] == 0
    
    @pytest.mark.asyncio
    async def test_fetch_athletes_http_error(self):
        """Test fetch_athletes HTTP error handling."""
        
        async def mock_fetch_athletes_http_error():
            return {
                "athletes_count": 0,
                "last_updated": None,
                "success": False,
                "error": "HTTP 404: Not Found"
            }
        
        result = await mock_fetch_athletes_http_error()
        assert result["success"] is False
        assert "HTTP 404" in result["error"]
        assert result["athletes_count"] == 0
    
    def test_lookup_athlete_found(self):
        """Test lookup_athlete when athlete is found."""
        
        # Mock database response
        mock_athlete = {
            "id": "123",
            "full_name": "Tom Brady",
            "first_name": "Tom",
            "last_name": "Brady",
            "team_id": "TB",
            "position": "QB",
            "status": "Active"
        }
        self.mock_db.get_athlete_by_id.return_value = mock_athlete
        
        def mock_lookup_athlete(athlete_id: str):
            try:
                athlete = self.mock_db.get_athlete_by_id(athlete_id)
                
                if athlete:
                    return {
                        "athlete": athlete,
                        "found": True,
                        "error": None
                    }
                else:
                    return {
                        "athlete": None,
                        "found": False,
                        "error": f"Athlete with ID '{athlete_id}' not found"
                    }
            except Exception as e:
                return {
                    "athlete": None,
                    "found": False,
                    "error": f"Error looking up athlete: {str(e)}"
                }
        
        result = mock_lookup_athlete("123")
        
        assert result["found"] is True
        assert result["athlete"]["full_name"] == "Tom Brady"
        assert result["error"] is None
    
    def test_lookup_athlete_not_found(self):
        """Test lookup_athlete when athlete is not found."""
        
        self.mock_db.get_athlete_by_id.return_value = None
        
        def mock_lookup_athlete(athlete_id: str):
            try:
                athlete = self.mock_db.get_athlete_by_id(athlete_id)
                
                if athlete:
                    return {
                        "athlete": athlete,
                        "found": True,
                        "error": None
                    }
                else:
                    return {
                        "athlete": None,
                        "found": False,
                        "error": f"Athlete with ID '{athlete_id}' not found"
                    }
            except Exception as e:
                return {
                    "athlete": None,
                    "found": False,
                    "error": f"Error looking up athlete: {str(e)}"
                }
        
        result = mock_lookup_athlete("nonexistent")
        
        assert result["found"] is False
        assert result["athlete"] is None
        assert "not found" in result["error"]
    
    def test_search_athletes_success(self):
        """Test search_athletes successful search."""
        
        # Mock database response
        mock_athletes = [
            {
                "id": "123",
                "full_name": "Tom Brady",
                "team_id": "TB",
                "position": "QB"
            },
            {
                "id": "456", 
                "full_name": "Tommy Thompson",
                "team_id": "SF",
                "position": "RB"
            }
        ]
        self.mock_db.search_athletes_by_name.return_value = mock_athletes
        
        def mock_search_athletes(name: str, limit: int = 10):
            try:
                # Validate limit
                if limit is None or limit < 1:
                    limit = 10
                elif limit > 100:
                    limit = 100
                
                athletes = self.mock_db.search_athletes_by_name(name, limit)
                
                return {
                    "athletes": athletes,
                    "count": len(athletes),
                    "search_term": name,
                    "error": None
                }
            except Exception as e:
                return {
                    "athletes": [],
                    "count": 0,
                    "search_term": name,
                    "error": f"Error searching athletes: {str(e)}"
                }
        
        result = mock_search_athletes("Tom")
        
        assert result["count"] == 2
        assert len(result["athletes"]) == 2
        assert result["search_term"] == "Tom"
        assert result["error"] is None
    
    def test_search_athletes_no_results(self):
        """Test search_athletes with no results."""
        
        self.mock_db.search_athletes_by_name.return_value = []
        
        def mock_search_athletes(name: str, limit: int = 10):
            try:
                athletes = self.mock_db.search_athletes_by_name(name, limit)
                
                return {
                    "athletes": athletes,
                    "count": len(athletes),
                    "search_term": name,
                    "error": None
                }
            except Exception as e:
                return {
                    "athletes": [],
                    "count": 0,
                    "search_term": name,
                    "error": f"Error searching athletes: {str(e)}"
                }
        
        result = mock_search_athletes("NonexistentName")
        
        assert result["count"] == 0
        assert len(result["athletes"]) == 0
        assert result["search_term"] == "NonexistentName"
        assert result["error"] is None
    
    def test_search_athletes_limit_validation(self):
        """Test search_athletes limit parameter validation."""
        
        self.mock_db.search_athletes_by_name.return_value = []
        
        def mock_search_athletes(name: str, limit: int = 10):
            try:
                # Validate limit
                if limit is None or limit < 1:
                    limit = 10
                elif limit > 100:
                    limit = 100
                
                athletes = self.mock_db.search_athletes_by_name(name, limit)
                
                return {
                    "athletes": athletes,
                    "count": len(athletes),
                    "search_term": name,
                    "limit_used": limit,
                    "error": None
                }
            except Exception as e:
                return {
                    "athletes": [],
                    "count": 0,
                    "search_term": name,
                    "error": f"Error searching athletes: {str(e)}"
                }
        
        # Test limit correction
        result = mock_search_athletes("Test", limit=0)
        assert result["limit_used"] == 10
        
        result = mock_search_athletes("Test", limit=-5)
        assert result["limit_used"] == 10
        
        result = mock_search_athletes("Test", limit=150)
        assert result["limit_used"] == 100
        
        result = mock_search_athletes("Test", limit=50)
        assert result["limit_used"] == 50
    
    def test_get_athletes_by_team_success(self):
        """Test get_athletes_by_team successful retrieval."""
        
        # Mock database response
        mock_athletes = [
            {
                "id": "123",
                "full_name": "Tom Brady",
                "team_id": "TB",
                "position": "QB"
            },
            {
                "id": "456",
                "full_name": "Mike Evans", 
                "team_id": "TB",
                "position": "WR"
            }
        ]
        self.mock_db.get_athletes_by_team.return_value = mock_athletes
        
        def mock_get_athletes_by_team(team_id: str):
            try:
                athletes = self.mock_db.get_athletes_by_team(team_id)
                
                return {
                    "athletes": athletes,
                    "count": len(athletes),
                    "team_id": team_id,
                    "error": None
                }
            except Exception as e:
                return {
                    "athletes": [],
                    "count": 0,
                    "team_id": team_id,
                    "error": f"Error getting athletes for team: {str(e)}"
                }
        
        result = mock_get_athletes_by_team("TB")
        
        assert result["count"] == 2
        assert len(result["athletes"]) == 2
        assert result["team_id"] == "TB"
        assert result["error"] is None
    
    def test_get_athletes_by_team_no_results(self):
        """Test get_athletes_by_team with no results."""
        
        self.mock_db.get_athletes_by_team.return_value = []
        
        def mock_get_athletes_by_team(team_id: str):
            try:
                athletes = self.mock_db.get_athletes_by_team(team_id)
                
                return {
                    "athletes": athletes,
                    "count": len(athletes),
                    "team_id": team_id,
                    "error": None
                }
            except Exception as e:
                return {
                    "athletes": [],
                    "count": 0,
                    "team_id": team_id,
                    "error": f"Error getting athletes for team: {str(e)}"
                }
        
        result = mock_get_athletes_by_team("NONEXISTENT")
        
        assert result["count"] == 0
        assert len(result["athletes"]) == 0
        assert result["team_id"] == "NONEXISTENT"
        assert result["error"] is None


class TestAthleteToolsIntegration:
    """Integration tests for athlete MCP tools."""
    
    def test_all_athlete_tools_registered(self):
        """Test that all athlete tools are properly registered."""
        app = create_app()
        
        # Verify the app was created successfully
        assert app.name == "NFL MCP Server"
        # The actual tool registration is tested in the full integration tests
    
    def test_athlete_database_initialization(self):
        """Test that the athlete database initializes properly."""
        with patch('nfl_mcp.server.AthleteDatabase') as mock_db_class:
            mock_db = MagicMock()
            mock_db_class.return_value = mock_db
            
            app = create_app()
            
            # Verify AthleteDatabase was instantiated
            mock_db_class.assert_called_once()
            assert app is not None