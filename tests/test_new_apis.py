"""Tests for new fantasy intelligence APIs."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from nfl_mcp.nfl_tools import get_team_injuries, get_team_player_stats, get_nfl_standings


class TestTeamInjuries:
    """Test cases for team injuries API."""
    
    @pytest.mark.asyncio
    async def test_get_team_injuries_success(self):
        """Test successful injury report fetch."""
        mock_response_data = {
            "items": [
                {
                    "athlete": {
                        "displayName": "Patrick Mahomes",
                        "id": "3139477",
                        "position": {"abbreviation": "QB"}
                    },
                    "team": {
                        "displayName": "Kansas City Chiefs"
                    },
                    "status": {"name": "Questionable"},
                    "description": "Ankle injury",
                    "date": "2025-01-13",
                    "type": {"name": "Ankle"}
                }
            ]
        }
        
        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        
        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client
            
            result = await get_team_injuries("KC", 10)
            
            assert result["success"] is True
            assert result["team_id"] == "KC"
            assert result["count"] == 1
            assert len(result["injuries"]) == 1
            
            injury = result["injuries"][0]
            assert injury["player_name"] == "Patrick Mahomes"
            assert injury["position"] == "QB"
            assert injury["status"] == "Questionable"
            assert injury["severity"] == "Medium"  # Questionable should be medium severity

    @pytest.mark.asyncio
    async def test_get_team_injuries_invalid_team(self):
        """Test injury report with invalid team ID."""
        result = await get_team_injuries("", 10)
        
        assert result["success"] is False
        assert "Team ID is required" in result["error"]

    @pytest.mark.asyncio
    async def test_get_team_injuries_404_error(self):
        """Test injury report when team not found."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=mock_response
        )
        
        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client
            
            result = await get_team_injuries("XXX", 10)
            
            assert result["success"] is True  # We handle 404s gracefully
            assert result["count"] == 0
            assert "No injury data found" in result["message"]


class TestTeamPlayerStats:
    """Test cases for team player statistics API."""
    
    @pytest.mark.asyncio
    async def test_get_team_player_stats_success(self):
        """Test successful player stats fetch."""
        mock_response_data = {
            "items": [
                {
                    "id": "3139477",
                    "displayName": "Patrick Mahomes",
                    "jersey": "15",
                    "position": {"abbreviation": "QB"},
                    "age": 29,
                    "experience": {"years": 7},
                    "active": True,
                    "team": {"displayName": "Kansas City Chiefs"}
                },
                {
                    "id": "4035687",
                    "displayName": "Travis Kelce", 
                    "jersey": "87",
                    "position": {"abbreviation": "TE"},
                    "age": 35,
                    "experience": {"years": 12},
                    "active": True,
                    "team": {"displayName": "Kansas City Chiefs"}
                }
            ]
        }
        
        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        
        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client
            
            result = await get_team_player_stats("KC", 2025, 2, 50)
            
            assert result["success"] is True
            assert result["team_id"] == "KC"
            assert result["season"] == 2025
            assert result["season_type"] == 2
            assert result["count"] == 2
            
            # Check QB is fantasy relevant
            qb_player = next(p for p in result["player_stats"] if p["position"] == "QB")
            assert qb_player["fantasy_relevant"] is True
            assert qb_player["player_name"] == "Patrick Mahomes"
            
            # Check TE is fantasy relevant
            te_player = next(p for p in result["player_stats"] if p["position"] == "TE")
            assert te_player["fantasy_relevant"] is True
            assert te_player["player_name"] == "Travis Kelce"

    @pytest.mark.asyncio
    async def test_get_team_player_stats_invalid_team(self):
        """Test player stats with invalid team ID."""
        result = await get_team_player_stats(None, 2025, 2, 50)
        
        assert result["success"] is False
        assert "Team ID is required" in result["error"]


class TestNFLStandings:
    """Test cases for NFL standings API."""
    
    @pytest.mark.asyncio
    async def test_get_nfl_standings_success(self):
        """Test successful standings fetch."""
        mock_response_data = {
            "children": [
                {
                    "team": {
                        "id": "12",
                        "displayName": "Kansas City Chiefs",
                        "abbreviation": "KC"
                    },
                    "stats": [
                        {"name": "wins", "value": 15},
                        {"name": "losses", "value": 2},
                        {"name": "ties", "value": 0},
                        {"name": "winPercent", "value": 0.882}
                    ]
                },
                {
                    "team": {
                        "id": "16", 
                        "displayName": "New York Giants",
                        "abbreviation": "NYG"
                    },
                    "stats": [
                        {"name": "wins", "value": 3},
                        {"name": "losses", "value": 14},
                        {"name": "ties", "value": 0},
                        {"name": "winPercent", "value": 0.176}
                    ]
                }
            ]
        }
        
        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        
        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client
            
            result = await get_nfl_standings(2025, 2, None)
            
            assert result["success"] is True
            assert result["season"] == 2025
            assert result["season_type"] == 2
            assert result["count"] == 2
            
            # Check high-win team context
            kc_team = next(t for t in result["standings"] if t["abbreviation"] == "KC")
            assert kc_team["wins"] == 15
            assert kc_team["motivation_level"] == "Low (Playoff lock)"
            assert "rest starters" in kc_team["fantasy_context"]
            
            # Check low-win team context  
            nyg_team = next(t for t in result["standings"] if t["abbreviation"] == "NYG")
            assert nyg_team["wins"] == 3
            assert nyg_team["motivation_level"] == "Medium (Development mode)"
            assert "evaluate young players" in nyg_team["fantasy_context"]

    @pytest.mark.asyncio
    async def test_get_nfl_standings_defaults(self):
        """Test standings with default parameters."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"children": []}
        mock_response.raise_for_status = MagicMock()
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        
        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client
            
            result = await get_nfl_standings()
            
            assert result["success"] is True
            assert result["season"] == 2025  # Default
            assert result["season_type"] == 2  # Default
            assert result["group"] is None  # Default