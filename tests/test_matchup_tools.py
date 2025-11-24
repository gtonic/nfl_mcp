"""
Tests for matchup_tools module - defense vs position rankings and matchup difficulty analysis.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from nfl_mcp import matchup_tools
from nfl_mcp.matchup_tools import (
    DefenseRankingsAnalyzer,
    _get_matchup_tier,
    _get_tier_color,
    ESPN_TEAM_MAP,
    MATCHUP_TIERS,
    get_defense_rankings,
    get_matchup_difficulty,
    analyze_roster_matchups,
)


class TestMatchupTierFunctions:
    """Test tier calculation functions."""
    
    def test_get_matchup_tier_elite(self):
        """Test elite tier for ranks 1-5."""
        for rank in [1, 2, 3, 4, 5]:
            assert _get_matchup_tier(rank) == "elite"
    
    def test_get_matchup_tier_tough(self):
        """Test tough tier for ranks 6-12."""
        for rank in [6, 7, 10, 12]:
            assert _get_matchup_tier(rank) == "tough"
    
    def test_get_matchup_tier_neutral(self):
        """Test neutral tier for ranks 13-20."""
        for rank in [13, 15, 18, 20]:
            assert _get_matchup_tier(rank) == "neutral"
    
    def test_get_matchup_tier_favorable(self):
        """Test favorable tier for ranks 21-27."""
        for rank in [21, 24, 27]:
            assert _get_matchup_tier(rank) == "favorable"
    
    def test_get_matchup_tier_smash(self):
        """Test smash tier for ranks 28-32."""
        for rank in [28, 30, 32]:
            assert _get_matchup_tier(rank) == "smash"
    
    def test_get_matchup_tier_out_of_range(self):
        """Test neutral fallback for out of range ranks."""
        assert _get_matchup_tier(0) == "neutral"
        assert _get_matchup_tier(33) == "neutral"
    
    def test_get_tier_color_all_tiers(self):
        """Test tier color indicators."""
        assert _get_tier_color("elite") == "🔴"
        assert _get_tier_color("tough") == "🟠"
        assert _get_tier_color("neutral") == "🟡"
        assert _get_tier_color("favorable") == "🟢"
        assert _get_tier_color("smash") == "💚"
        assert _get_tier_color("unknown") == "⚪"


class TestESPNTeamMapping:
    """Test ESPN team mapping."""
    
    def test_team_map_completeness(self):
        """Test that all 32 NFL teams are mapped."""
        assert len(ESPN_TEAM_MAP) == 32
    
    def test_key_teams_present(self):
        """Test specific team mappings."""
        expected_teams = {"KC", "SF", "PHI", "DAL", "BUF", "MIA", "NYJ", "NE", "WSH"}
        team_values = set(ESPN_TEAM_MAP.values())
        for team in expected_teams:
            assert team in team_values


class TestDefenseRankingsAnalyzer:
    """Test DefenseRankingsAnalyzer class."""
    
    def test_analyzer_initialization_with_explicit_none(self):
        """Test analyzer initializes with explicit None db."""
        analyzer = DefenseRankingsAnalyzer(db=MagicMock())
        assert analyzer.db is not None
        assert analyzer._rankings_cache == {}
    
    def test_analyzer_initialization_default(self):
        """Test analyzer auto-initializes database by default."""
        # The default behavior initializes a database
        analyzer = DefenseRankingsAnalyzer()
        # db may or may not be None depending on environment
        assert analyzer._rankings_cache == {}
    
    def test_get_fallback_rankings(self):
        """Test fallback rankings return all teams."""
        analyzer = DefenseRankingsAnalyzer(db=None)
        rankings = analyzer._get_fallback_rankings("QB")
        
        assert len(rankings) == 32
        assert all("team" in r and "rank" in r for r in rankings)
        assert all(r["is_fallback"] is True for r in rankings)
    
    def test_get_matchup_difficulty_valid(self):
        """Test matchup difficulty with valid input."""
        analyzer = DefenseRankingsAnalyzer(db=None)
        
        # Create mock rankings
        mock_rankings = {
            "WR": [
                {"team": "KC", "rank": 30, "matchup_tier": "smash"},
                {"team": "SF", "rank": 5, "matchup_tier": "elite"},
            ]
        }
        
        result = analyzer.get_matchup_difficulty("WR", "KC", mock_rankings)
        assert result["rank"] == 30
        assert result["matchup_tier"] == "smash"
    
    def test_get_matchup_difficulty_team_normalization(self):
        """Test WAS -> WSH normalization."""
        analyzer = DefenseRankingsAnalyzer(db=None)
        
        mock_rankings = {
            "RB": [
                {"team": "WSH", "rank": 15, "matchup_tier": "neutral"},
            ]
        }
        
        # WAS should be normalized to WSH
        result = analyzer.get_matchup_difficulty("RB", "WAS", mock_rankings)
        assert result["rank"] == 15
    
    def test_normalize_team_name(self):
        """Test team name normalization."""
        analyzer = DefenseRankingsAnalyzer(db=None)
        
        # Full names
        assert analyzer._normalize_team_name("Kansas City Chiefs") == "KC"
        assert analyzer._normalize_team_name("San Francisco 49ers") == "SF"
        
        # Abbreviations pass through
        assert analyzer._normalize_team_name("KC") == "KC"
        assert analyzer._normalize_team_name("SF") == "SF"


class TestGetDefenseRankings:
    """Test get_defense_rankings MCP tool."""
    
    @pytest.mark.asyncio
    async def test_get_defense_rankings_returns_dict(self):
        """Test function returns proper structure."""
        with patch.object(DefenseRankingsAnalyzer, 'fetch_defense_rankings', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "QB": [{"team": "KC", "rank": 1}],
                "RB": [{"team": "SF", "rank": 1}],
            }
            
            result = await get_defense_rankings(positions=["QB"])
            
            assert "success" in result
            assert result["success"] is True
            # Data is flattened into the response, not nested in 'data'
            assert "rankings" in result
    
    @pytest.mark.asyncio
    async def test_get_defense_rankings_filters_positions(self):
        """Test position filtering."""
        with patch.object(DefenseRankingsAnalyzer, 'fetch_defense_rankings', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "QB": [{"team": "KC", "rank": 1}],
                "RB": [{"team": "SF", "rank": 1}],
                "WR": [{"team": "DAL", "rank": 1}],
                "TE": [{"team": "PHI", "rank": 1}],
            }
            
            result = await get_defense_rankings(positions=["WR", "RB"])
            
            assert result["success"] is True
            rankings = result["rankings"]
            assert "WR" in rankings
            assert "RB" in rankings
            assert len(rankings) == 2


class TestGetMatchupDifficulty:
    """Test get_matchup_difficulty MCP tool."""
    
    @pytest.mark.asyncio
    async def test_get_matchup_difficulty_valid_input(self):
        """Test with valid position and team."""
        with patch.object(DefenseRankingsAnalyzer, 'fetch_defense_rankings', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "WR": [{"team": "KC", "rank": 28, "matchup_tier": "smash"}]
            }
            
            result = await get_matchup_difficulty(position="WR", opponent_team="KC")
            
            assert result["success"] is True
            assert "matchup" in result
    
    @pytest.mark.asyncio
    async def test_get_matchup_difficulty_invalid_position(self):
        """Test error handling for invalid position."""
        result = await get_matchup_difficulty(position="K", opponent_team="KC")
        
        assert result["success"] is False
        assert "Invalid position" in result.get("error", "")


class TestAnalyzeRosterMatchups:
    """Test analyze_roster_matchups MCP tool."""
    
    @pytest.mark.asyncio
    async def test_analyze_roster_matchups_multiple_players(self):
        """Test analysis of multiple players."""
        with patch.object(DefenseRankingsAnalyzer, 'fetch_defense_rankings', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "QB": [{"team": "LV", "rank": 28, "matchup_tier": "smash"}],
                "WR": [{"team": "NE", "rank": 5, "matchup_tier": "elite"}],
            }
            
            players = [
                {"name": "Patrick Mahomes", "position": "QB", "opponent": "LV"},
                {"name": "Tyreek Hill", "position": "WR", "opponent": "NE"},
            ]
            
            result = await analyze_roster_matchups(players=players)
            
            assert result["success"] is True
            assert result["total_analyzed"] == 2
            assert len(result["analysis"]) == 2
    
    @pytest.mark.asyncio
    async def test_analyze_roster_matchups_empty_list(self):
        """Test error handling for empty player list."""
        from nfl_mcp.tool_registry import analyze_roster_matchups as wrapper_func
        
        result = await wrapper_func(players=[])
        
        assert result["success"] is False
        assert "No players provided" in result.get("error", "")
    
    @pytest.mark.asyncio
    async def test_analyze_roster_matchups_invalid_position(self):
        """Test handling of invalid position in player list."""
        with patch.object(DefenseRankingsAnalyzer, 'fetch_defense_rankings', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {"QB": [], "RB": [], "WR": [], "TE": []}
            
            players = [
                {"name": "Justin Tucker", "position": "K", "opponent": "CLE"},
            ]
            
            result = await analyze_roster_matchups(players=players)
            
            assert result["success"] is True
            # Invalid position should be marked with error
            analysis = result["analysis"][0]
            assert "error" in analysis


class TestToolRegistryIntegration:
    """Test that matchup tools are properly registered."""
    
    def test_matchup_tools_in_registry(self):
        """Test all matchup tools are registered."""
        from nfl_mcp.tool_registry import get_all_tools
        
        tools = get_all_tools()
        tool_names = [t.__name__ for t in tools]
        
        assert "get_defense_rankings" in tool_names
        assert "get_matchup_difficulty" in tool_names
        assert "analyze_roster_matchups" in tool_names
    
    def test_matchup_tools_import(self):
        """Test matchup_tools module imports cleanly."""
        from nfl_mcp import matchup_tools
        
        assert hasattr(matchup_tools, 'get_defense_rankings')
        assert hasattr(matchup_tools, 'get_matchup_difficulty')
        assert hasattr(matchup_tools, 'analyze_roster_matchups')
        assert hasattr(matchup_tools, 'DefenseRankingsAnalyzer')
