"""Tests for vegas_tools module - Vegas lines integration."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta

from nfl_mcp.vegas_tools import (
    VegasLinesAnalyzer,
    get_game_environment_tier,
    calculate_implied_team_total,
    get_game_script_projection,
    get_vegas_lines,
    get_game_environment,
    analyze_roster_vegas,
    get_stack_opportunities,
    TEAM_ABBREVIATIONS,
    ABBREVIATION_TO_FULL,
)


class TestHelperFunctions:
    """Test helper functions for Vegas lines processing."""
    
    def test_game_environment_tier_shootout(self):
        """Test shootout tier (O/U >= 50)."""
        result = get_game_environment_tier(52.5)
        assert result["tier"] == "shootout"
        assert result["indicator"] == "🔥"
        assert "shootout" in result["description"].lower()
        assert "+15%" in result["qb_boost"]
    
    def test_game_environment_tier_high_scoring(self):
        """Test high-scoring tier (46-49.5)."""
        result = get_game_environment_tier(47.5)
        assert result["tier"] == "high_scoring"
        assert result["indicator"] == "📈"
        assert "+8%" in result["qb_boost"]
    
    def test_game_environment_tier_average(self):
        """Test average tier (41-45.5)."""
        result = get_game_environment_tier(44.0)
        assert result["tier"] == "average"
        assert result["indicator"] == "➡️"
        assert "0%" in result["qb_boost"]
    
    def test_game_environment_tier_low_scoring(self):
        """Test low-scoring tier (37-40.5)."""
        result = get_game_environment_tier(38.5)
        assert result["tier"] == "low_scoring"
        assert result["indicator"] == "📉"
        assert "-5%" in result["qb_boost"]
    
    def test_game_environment_tier_defensive_battle(self):
        """Test defensive battle tier (< 37)."""
        result = get_game_environment_tier(35.0)
        assert result["tier"] == "defensive_battle"
        assert result["indicator"] == "🛡️"
        assert "-10%" in result["qb_boost"]
    
    def test_calculate_implied_team_total_favorite(self):
        """Test implied total calculation for favorite."""
        # Total = 48, spread = -6.5 (favorite)
        # Implied = (48 + 6.5) / 2 = 27.25
        result = calculate_implied_team_total(48.0, -6.5, is_favorite=True)
        assert result == 27.2  # Rounded to 1 decimal
    
    def test_calculate_implied_team_total_underdog(self):
        """Test implied total calculation for underdog."""
        # Total = 48, spread = 6.5 (underdog)
        # Implied = (48 - 6.5) / 2 = 20.75
        result = calculate_implied_team_total(48.0, 6.5, is_favorite=False)
        assert result == 20.8  # Rounded to 1 decimal
    
    def test_calculate_implied_total_pick_em(self):
        """Test implied total for pick'em game."""
        # Total = 44, spread = 0
        result = calculate_implied_team_total(44.0, 0, is_favorite=True)
        assert result == 22.0
    
    def test_game_script_projection_heavy_favorite(self):
        """Test game script for heavy favorite (spread <= -10)."""
        result = get_game_script_projection(-10.5)
        assert result["projection"] == "likely_blowout_win"
        assert result["indicator"] == "💨"
        assert "positive" in result["rb_impact"].lower()
    
    def test_game_script_projection_heavy_underdog(self):
        """Test game script for heavy underdog (spread >= 10)."""
        result = get_game_script_projection(10.5)
        assert result["projection"] == "likely_blowout_loss"
        assert result["indicator"] == "⚠️"
        assert "negative" in result["rb_impact"].lower()
    
    def test_game_script_projection_solid_favorite(self):
        """Test game script for solid favorite (spread -6 to -9.5)."""
        result = get_game_script_projection(-7.0)
        assert result["projection"] == "solid_favorite"
        assert result["indicator"] == "✅"
    
    def test_game_script_projection_slight_favorite(self):
        """Test game script for slight favorite (spread -3 to -5.5)."""
        result = get_game_script_projection(-4.0)
        assert result["projection"] == "slight_favorite"
    
    def test_game_script_projection_toss_up(self):
        """Test game script for toss-up (spread < 3)."""
        result = get_game_script_projection(-2.5)
        assert result["projection"] == "toss_up"
        assert result["indicator"] == "⚖️"


class TestTeamMappings:
    """Test team name mappings."""
    
    def test_team_abbreviations_complete(self):
        """Verify all 32 teams are mapped."""
        assert len(TEAM_ABBREVIATIONS) == 32
    
    def test_reverse_mapping(self):
        """Verify reverse mapping works."""
        assert ABBREVIATION_TO_FULL["KC"] == "Kansas City Chiefs"
        assert ABBREVIATION_TO_FULL["WSH"] == "Washington Commanders"
    
    def test_washington_mapping(self):
        """Verify Washington uses WSH abbreviation."""
        assert TEAM_ABBREVIATIONS["Washington Commanders"] == "WSH"


class TestVegasLinesAnalyzer:
    """Test VegasLinesAnalyzer class."""
    
    def test_init_no_api_key(self):
        """Test initialization without API key."""
        with patch.dict('os.environ', {}, clear=True):
            # Explicitly pass None to avoid picking up any env var
            analyzer = VegasLinesAnalyzer(api_key=None)
            # The analyzer will try os.getenv in __init__, so mock that too
        analyzer = VegasLinesAnalyzer(api_key=None)
        assert analyzer.api_key is None
    
    def test_init_with_api_key(self):
        """Test initialization with API key."""
        analyzer = VegasLinesAnalyzer(api_key="test_key_123")
        assert analyzer.api_key == "test_key_123"
    
    def test_normalize_team_standard(self):
        """Test standard team normalization."""
        analyzer = VegasLinesAnalyzer()
        assert analyzer._normalize_team("kc") == "KC"
        assert analyzer._normalize_team("KC") == "KC"
    
    def test_normalize_team_washington_variations(self):
        """Test Washington team code normalization."""
        analyzer = VegasLinesAnalyzer()
        assert analyzer._normalize_team("WAS") == "WSH"
        assert analyzer._normalize_team("WSH") == "WSH"
        assert analyzer._normalize_team("washington") == "WSH"
    
    def test_normalize_team_jacksonville_variations(self):
        """Test Jacksonville team code normalization."""
        analyzer = VegasLinesAnalyzer()
        assert analyzer._normalize_team("JAC") == "JAX"
        assert analyzer._normalize_team("JAX") == "JAX"
    
    def test_normalize_team_raiders_variations(self):
        """Test Raiders team code normalization."""
        analyzer = VegasLinesAnalyzer()
        assert analyzer._normalize_team("LV") == "LV"
        assert analyzer._normalize_team("OAK") == "LV"
    
    def test_get_team_abbrev(self):
        """Test full name to abbreviation conversion."""
        analyzer = VegasLinesAnalyzer()
        assert analyzer._get_team_abbrev("Kansas City Chiefs") == "KC"
        assert analyzer._get_team_abbrev("Tampa Bay Buccaneers") == "TB"
    
    def test_get_game_lines_not_found(self):
        """Test fallback when team not in cache."""
        analyzer = VegasLinesAnalyzer()
        result = analyzer.get_game_lines("KC")
        
        assert result["is_fallback"] is True
        assert result["total"] == 45.0  # Default neutral
        assert result["home_spread"] == 0
    
    def test_get_game_lines_from_cache(self):
        """Test getting lines from cache."""
        analyzer = VegasLinesAnalyzer()
        analyzer._lines_cache = {
            "KC": {
                "home_team": "KC",
                "away_team": "BUF",
                "total": 52.5,
                "home_spread": -3.0,
                "is_fallback": False
            }
        }
        
        result = analyzer.get_game_lines("KC")
        assert result["total"] == 52.5
        assert result["home_spread"] == -3.0
    
    @pytest.mark.asyncio
    async def test_fetch_current_lines_no_api_key(self):
        """Test fallback when no API key configured."""
        analyzer = VegasLinesAnalyzer(api_key=None)
        result = await analyzer.fetch_current_lines()
        
        # Should return empty fallback
        assert result == {}
    
    @pytest.mark.asyncio
    async def test_fetch_current_lines_cached(self):
        """Test cache usage for lines."""
        analyzer = VegasLinesAnalyzer(api_key="test_key")
        
        # Set cache
        analyzer._lines_cache = {"KC": {"total": 50.0}}
        analyzer._cache_time = datetime.now(timezone.utc)
        
        result = await analyzer.fetch_current_lines()
        
        # Should return cached data without API call
        assert result == {"KC": {"total": 50.0}}
    
    @pytest.mark.asyncio
    async def test_fetch_current_lines_api_success(self):
        """Test successful API fetch."""
        analyzer = VegasLinesAnalyzer(api_key="test_key")
        
        mock_response_data = [
            {
                "id": "game1",
                "sport_key": "americanfootball_nfl",
                "commence_time": "2024-12-15T18:00:00Z",
                "home_team": "Kansas City Chiefs",
                "away_team": "Buffalo Bills",
                "bookmakers": [
                    {
                        "key": "fanduel",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": "Kansas City Chiefs", "price": -110, "point": -3.5},
                                    {"name": "Buffalo Bills", "price": -110, "point": 3.5}
                                ]
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": -110, "point": 52.5},
                                    {"name": "Under", "price": -110, "point": 52.5}
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_response_data
        mock_response.headers = {"x-requests-remaining": "450"}
        mock_response.raise_for_status = MagicMock()
        
        with patch("nfl_mcp.vegas_tools.httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client_instance.get = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client.return_value.__aexit__ = AsyncMock()
            
            result = await analyzer.fetch_current_lines()
        
        assert "BUF@KC" in result
        game = result["BUF@KC"]
        assert game["home_team"] == "KC"
        assert game["away_team"] == "BUF"
        assert game["total"] == 52.5
        assert game["home_spread"] == -3.5
        assert game["home_is_favorite"] is True
    
    @pytest.mark.asyncio
    async def test_fetch_current_lines_api_error(self):
        """Test fallback on API error."""
        import httpx
        analyzer = VegasLinesAnalyzer(api_key="test_key")
        
        # Mock httpx to raise an HTTPError
        with patch.object(httpx, 'AsyncClient') as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("API Error"))
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client_class.return_value.__aexit__.return_value = None
            
            result = await analyzer.fetch_current_lines()
        
        # Should return empty fallback
        assert result == {}


class TestGetVegasLines:
    """Test get_vegas_lines MCP tool."""
    
    @pytest.mark.asyncio
    async def test_get_vegas_lines_all_games(self):
        """Test fetching all games."""
        mock_lines = {
            "BUF@KC": {
                "home_team": "KC",
                "away_team": "BUF",
                "total": 52.5,
                "home_spread": -3.0,
                "game_environment": get_game_environment_tier(52.5)
            },
            "DAL@PHI": {
                "home_team": "PHI",
                "away_team": "DAL",
                "total": 45.0,
                "home_spread": -4.5,
                "game_environment": get_game_environment_tier(45.0)
            },
            "KC": {"home_team": "KC"},  # Team index
            "BUF": {"home_team": "KC"},
            "PHI": {"home_team": "PHI"},
            "DAL": {"home_team": "PHI"}
        }
        
        with patch("nfl_mcp.vegas_tools.get_vegas_analyzer") as mock_analyzer:
            analyzer_instance = MagicMock()
            analyzer_instance.fetch_current_lines = AsyncMock(return_value=mock_lines)
            mock_analyzer.return_value = analyzer_instance
            
            result = await get_vegas_lines()
        
        assert result["success"] is True
        assert result["total_games"] == 2
        assert len(result["games"]) == 2
    
    @pytest.mark.asyncio
    async def test_get_vegas_lines_filtered(self):
        """Test filtering by teams."""
        mock_lines = {
            "BUF@KC": {
                "home_team": "KC",
                "away_team": "BUF",
                "total": 52.5,
                "home_spread": -3.0,
                "game_environment": get_game_environment_tier(52.5)
            },
            "DAL@PHI": {
                "home_team": "PHI",
                "away_team": "DAL",
                "total": 45.0,
                "home_spread": -4.5,
                "game_environment": get_game_environment_tier(45.0)
            }
        }
        
        with patch("nfl_mcp.vegas_tools.get_vegas_analyzer") as mock_analyzer:
            analyzer_instance = MagicMock()
            analyzer_instance.fetch_current_lines = AsyncMock(return_value=mock_lines)
            analyzer_instance._normalize_team = lambda t: t.upper()
            mock_analyzer.return_value = analyzer_instance
            
            result = await get_vegas_lines(teams=["KC"])
        
        assert result["success"] is True
        assert result["total_games"] == 1
        assert result["games"][0]["home_team"] == "KC"


class TestGetGameEnvironment:
    """Test get_game_environment MCP tool."""
    
    @pytest.mark.asyncio
    async def test_get_game_environment_home_team(self):
        """Test game environment for home team."""
        mock_lines = {
            "KC": {
                "home_team": "KC",
                "away_team": "BUF",
                "total": 52.5,
                "home_spread": -3.0,
                "away_spread": 3.0,
                "home_implied_total": 27.8,
                "away_implied_total": 24.8,
                "home_is_favorite": True,
                "game_environment": get_game_environment_tier(52.5),
                "home_game_script": get_game_script_projection(-3.0),
                "away_game_script": get_game_script_projection(3.0),
                "commence_time": "2024-12-15T18:00:00Z",
                "is_fallback": False
            }
        }
        
        with patch("nfl_mcp.vegas_tools.get_vegas_analyzer") as mock_analyzer:
            analyzer_instance = MagicMock()
            analyzer_instance.fetch_current_lines = AsyncMock(return_value=mock_lines)
            analyzer_instance._normalize_team = lambda t: t.upper()
            analyzer_instance.get_game_lines = MagicMock(return_value=mock_lines["KC"])
            mock_analyzer.return_value = analyzer_instance
            
            result = await get_game_environment(team="KC")
        
        assert result["success"] is True
        assert result["team"] == "KC"
        assert result["opponent"] == "BUF"
        assert result["is_home"] is True
        assert result["total"] == 52.5
        assert result["implied_total"] == 27.8
        assert result["is_favorite"] is True
        assert len(result["recommendations"]) > 0


class TestAnalyzeRosterVegas:
    """Test analyze_roster_vegas MCP tool."""
    
    @pytest.mark.asyncio
    async def test_analyze_roster_multiple_players(self):
        """Test analyzing multiple players."""
        players = [
            {"name": "Patrick Mahomes", "team": "KC", "position": "QB"},
            {"name": "Derrick Henry", "team": "BAL", "position": "RB"}
        ]
        
        mock_lines = {
            "KC": {
                "home_team": "KC",
                "away_team": "BUF",
                "total": 52.5,
                "home_implied_total": 27.8,
                "away_implied_total": 24.8,
                "home_spread": -3.0,
                "game_environment": get_game_environment_tier(52.5),
                "is_fallback": False
            },
            "BAL": {
                "home_team": "BAL",
                "away_team": "PIT",
                "total": 42.0,
                "home_implied_total": 24.0,
                "away_implied_total": 18.0,
                "home_spread": -6.0,
                "game_environment": get_game_environment_tier(42.0),
                "is_fallback": False
            }
        }
        
        with patch("nfl_mcp.vegas_tools.get_vegas_analyzer") as mock_analyzer:
            analyzer_instance = MagicMock()
            analyzer_instance.fetch_current_lines = AsyncMock(return_value=mock_lines)
            analyzer_instance._normalize_team = lambda t: t.upper()
            analyzer_instance.get_game_lines = lambda t, l: mock_lines.get(t, {})
            mock_analyzer.return_value = analyzer_instance
            
            result = await analyze_roster_vegas(players=players)
        
        assert result["success"] is True
        assert result["total_analyzed"] == 2
        assert len(result["analysis"]) == 2
        
        # KC player should be in best environments
        kc_analysis = [a for a in result["analysis"] if a["team"] == "KC"][0]
        assert kc_analysis["environment_tier"] == "shootout"
    
    @pytest.mark.asyncio
    async def test_analyze_roster_empty_players(self):
        """Test with empty player list."""
        result = await analyze_roster_vegas(players=[])
        
        assert result["success"] is False
        assert "No players provided" in result["error"]


class TestGetStackOpportunities:
    """Test get_stack_opportunities MCP tool."""
    
    @pytest.mark.asyncio
    async def test_get_stack_opportunities_found(self):
        """Test finding stack opportunities."""
        mock_lines = {
            "BUF@KC": {
                "home_team": "KC",
                "away_team": "BUF",
                "total": 52.5,
                "home_implied_total": 27.8,
                "away_implied_total": 24.8,
                "game_environment": get_game_environment_tier(52.5)
            },
            "DAL@PHI": {
                "home_team": "PHI",
                "away_team": "DAL",
                "total": 45.0,
                "home_implied_total": 24.8,
                "away_implied_total": 20.2,
                "game_environment": get_game_environment_tier(45.0)
            }
        }
        
        with patch("nfl_mcp.vegas_tools.get_vegas_analyzer") as mock_analyzer:
            analyzer_instance = MagicMock()
            analyzer_instance.fetch_current_lines = AsyncMock(return_value=mock_lines)
            mock_analyzer.return_value = analyzer_instance
            
            result = await get_stack_opportunities(min_total=48.0)
        
        assert result["success"] is True
        assert result["total_opportunities"] == 1
        assert len(result["stacks"]) == 1
        assert result["stacks"][0]["game"] == "BUF@KC"
    
    @pytest.mark.asyncio
    async def test_get_stack_opportunities_none_found(self):
        """Test when no games meet threshold."""
        mock_lines = {
            "DAL@PHI": {
                "home_team": "PHI",
                "away_team": "DAL",
                "total": 45.0,
                "home_implied_total": 24.8,
                "away_implied_total": 20.2,
                "game_environment": get_game_environment_tier(45.0)
            }
        }
        
        with patch("nfl_mcp.vegas_tools.get_vegas_analyzer") as mock_analyzer:
            analyzer_instance = MagicMock()
            analyzer_instance.fetch_current_lines = AsyncMock(return_value=mock_lines)
            mock_analyzer.return_value = analyzer_instance
            
            result = await get_stack_opportunities(min_total=50.0)
        
        assert result["success"] is True
        assert result["total_opportunities"] == 0


class TestIntegrationScenarios:
    """Test realistic integration scenarios."""
    
    @pytest.mark.asyncio
    async def test_high_total_game_analysis(self):
        """Test analyzing a high-total shootout game."""
        # KC vs BUF - expected shootout
        total = 54.5
        spread = -2.5  # KC slight favorite
        
        env = get_game_environment_tier(total)
        home_implied = calculate_implied_team_total(total, spread, is_favorite=True)
        away_implied = calculate_implied_team_total(total, spread, is_favorite=False)
        game_script = get_game_script_projection(spread)
        
        assert env["tier"] == "shootout"
        assert home_implied > 27  # Good implied total
        assert away_implied > 25  # Also good for underdog
        assert game_script["projection"] == "toss_up"  # Close game
    
    @pytest.mark.asyncio
    async def test_defensive_battle_analysis(self):
        """Test analyzing a low-scoring defensive game."""
        # NYJ vs NE - low total
        total = 36.5
        spread = 3.0  # NYJ underdog
        
        env = get_game_environment_tier(total)
        home_implied = calculate_implied_team_total(total, spread, is_favorite=True)
        away_implied = calculate_implied_team_total(total, spread, is_favorite=False)
        
        assert env["tier"] == "defensive_battle"
        assert "-10%" in env["qb_boost"]  # Bad for QBs
        assert home_implied < 20  # Low expected scoring
        assert away_implied < 18
    
    @pytest.mark.asyncio
    async def test_blowout_game_script(self):
        """Test game script for expected blowout."""
        # Heavy favorite scenario
        spread = -14.0
        
        game_script = get_game_script_projection(spread)
        
        assert game_script["projection"] == "likely_blowout_win"
        assert "positive" in game_script["rb_impact"].lower()  # Good for RBs
        assert "limited" in game_script["pass_impact"].lower()  # May limit pass volume
