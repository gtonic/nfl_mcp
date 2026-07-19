"""Tests for nfl_tools module."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nfl_mcp.nfl_tools import (
    get_nfl_news,
    get_teams,
    fetch_teams,
    get_depth_chart,
    get_team_injuries,
    get_team_player_stats,
    get_nfl_standings,
    get_team_schedule,
    get_league_leaders,
    get_current_season_and_week,
)


class TestGetNflNews:
    """Test get_nfl_news function."""

    @pytest.mark.asyncio
    async def test_get_nfl_news_success(self):
        """Test successful NFL news retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "articles": [
                {
                    "headline": "Test Article",
                    "description": "Test description",
                    "published": "2026-01-01",
                    "type": "news",
                }
            ]
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_nfl_news(limit=1)
            
            assert result["success"] is True
            assert result["total_articles"] == 1
            assert result["articles"][0]["headline"] == "Test Article"

    @pytest.mark.asyncio
    async def test_get_nfl_news_default_limit(self):
        """Test default limit is applied."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"articles": []}
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_nfl_news()
            
            assert result["success"] is True


class TestGetTeams:
    """Test get_teams function."""

    @pytest.mark.asyncio
    async def test_get_teams_success(self):
        """Test successful teams retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "sports": [
                {
                    "leagues": [
                        {
                            "teams": [
                                {
                                    "team": {
                                        "id": "1",
                                        "abbreviation": "KC",
                                        "name": "Chiefs",
                                        "displayName": "Kansas City Chiefs"
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_teams()
            
            assert result["success"] is True
            assert result["total_teams"] == 1
            assert result["teams"][0]["abbreviation"] == "KC"


class TestGetDepthChart:
    """Test get_depth_chart function."""

    @pytest.mark.asyncio
    async def test_get_depth_chart_success(self):
        """Test successful depth chart retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = """
        <html>
            <h1>Kansas City Chiefs</h1>
            <table>
                <tr><td>QB</td><td>P. Mahomes</td></tr>
                <tr><td>RB</td><td>I. Pacheco</td></tr>
            </table>
        </html>
        """
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_depth_chart("KC")
            
            assert result["success"] is True
            assert result["team_id"] == "KC"

    @pytest.mark.asyncio
    async def test_get_depth_chart_invalid_team(self):
        """Test with invalid team ID."""
        result = await get_depth_chart("")
        assert result["success"] is False


class TestGetTeamInjuries:
    """Test get_team_injuries function."""

    @pytest.mark.asyncio
    async def test_get_team_injuries_success(self):
        """Test successful injury report retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {
                    "athlete": {
                        "displayName": "Player 1",
                        "id": "1",
                        "position": {"abbreviation": "RB"}
                    },
                    "status": {"name": "Questionable"},
                    "description": "Ankle injury",
                    "date": "2026-01-01",
                    "type": {"name": "Ankle"}
                }
            ]
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_team_injuries("KC")
            
            assert result["success"] is True
            assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_get_team_injuries_404(self):
        """Test injury report with 404 response."""
        import httpx
        
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=mock_response
        )
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_team_injuries("INVALID")
            
            assert result["success"] is True  # Handled gracefully
            assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_get_team_injuries_invalid_team(self):
        """Test with invalid team ID."""
        result = await get_team_injuries("")
        assert result["success"] is False


class TestGetTeamPlayerStats:
    """Test get_team_player_stats function."""

    @pytest.mark.asyncio
    async def test_get_team_player_stats_success(self):
        """Test successful player stats retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {
                    "id": "1",
                    "displayName": "P. Mahomes",
                    "jersey": "15",
                    "position": {"abbreviation": "QB"},
                    "age": 28,
                    "experience": {"years": 6},
                    "active": True,
                    "team": {"displayName": "Kansas City Chiefs"}
                }
            ]
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_team_player_stats("KC")
            
            assert result["success"] is True
            assert result["count"] == 1
            assert result["season"] == 2026  # Default season

    @pytest.mark.asyncio
    async def test_get_team_player_stats_with_season(self):
        """Test player stats with custom season."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"items": []}
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_team_player_stats("KC", season=2025)
            
            assert result["success"] is True
            assert result["season"] == 2025


class TestGetNflStandings:
    """Test get_nfl_standings function."""

    @pytest.mark.asyncio
    async def test_get_nfl_standings_success(self):
        """Test successful standings retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "children": [
                {
                    "team": {
                        "id": "1",
                        "displayName": "Kansas City Chiefs",
                        "abbreviation": "KC"
                    },
                    "stats": [
                        {"name": "wins", "value": 14},
                        {"name": "losses", "value": 3}
                    ]
                }
            ]
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_nfl_standings()
            
            assert result["success"] is True
            assert result["count"] == 1
            assert result["standings"][0]["wins"] == 14
            assert result["standings"][0]["losses"] == 3

    @pytest.mark.asyncio
    async def test_get_nfl_standings_default_season(self):
        """Test standings with default season."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"children": []}
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_nfl_standings()
            
            assert result["success"] is True
            assert result["season"] == 2026


class TestGetTeamSchedule:
    """Test get_team_schedule function."""

    @pytest.mark.asyncio
    async def test_get_team_schedule_success(self):
        """Test successful schedule retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "team": {"displayName": "Kansas City Chiefs"},
            "events": [
                {
                    "id": "1",
                    "date": "2026-09-07",
                    "week": {"number": 1},
                    "season": {"type": {"name": "Preseason"}},
                    "competitions": [
                        {
                            "competitors": [
                                {
                                    "team": {"abbreviation": "KC", "displayName": "Kansas City"},
                                    "homeAway": "home"
                                },
                                {
                                    "team": {"abbreviation": "DET", "displayName": "Detroit"}
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_team_schedule("KC")
            
            assert result["success"] is True
            assert result["count"] == 1
            assert result["team_id"] == "KC"

    @pytest.mark.asyncio
    async def test_get_team_schedule_invalid_team(self):
        """Test with invalid team ID."""
        result = await get_team_schedule("")
        assert result["success"] is False


class TestGetLeagueLeaders:
    """Test get_league_leaders function."""

    @pytest.mark.asyncio
    async def test_get_league_leaders_invalid_category(self):
        """Test with invalid category."""
        result = await get_league_leaders(category="invalid")
        assert result["success"] is False
        assert "validation" in result["error_type"].lower()

    @pytest.mark.asyncio
    async def test_get_league_leaders_no_category(self):
        """Test with empty category."""
        result = await get_league_leaders(category="")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_get_league_leaders_valid_category(self):
        """Test with valid category."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "categories": [
                {
                    "name": "Passing Yards",
                    "displayName": "Passing Yards",
                    "leaders": []
                }
            ]
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.nfl_tools.create_http_client', return_value=mock_client):
            result = await get_league_leaders(category="pass")
            
            # Should succeed (even if no leaders found)
            assert result["success"] is True
            assert result["season"] == 2026


class TestGetCurrentSeasonAndWeek:
    """Test get_current_season_and_week function."""

    @pytest.mark.asyncio
    async def test_get_current_season_and_week_success(self):
        """Test successful season/week detection."""
        mock_state = {
            "success": True,
            "nfl_state": {
                "season": 2026,
                "week": 5
            }
        }
        
        async def mock_get_nfl_state():
            return mock_state
        
        with patch('nfl_mcp.sleeper_tools.get_nfl_state', side_effect=mock_get_nfl_state):
            season, week = await get_current_season_and_week()
            
            assert season == 2026
            assert week == 5

    @pytest.mark.asyncio
    async def test_get_current_season_and_week_failure(self):
        """Test season/week detection failure returns defaults."""
        async def mock_get_nfl_state():
            raise Exception("API error")
        
        with patch('nfl_mcp.sleeper_tools.get_nfl_state', side_effect=mock_get_nfl_state):
            season, week = await get_current_season_and_week()
            
            # Should return current year and week 0
            assert season is not None
            assert week == 0
