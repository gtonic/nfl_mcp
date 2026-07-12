"""Tests for trade_analyzer_tools module."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from nfl_mcp.trade_analyzer_tools import TradeAnalyzer, analyze_trade


class TestTradeAnalyzer:
    """Test TradeAnalyzer class."""

    @pytest.fixture
    def analyzer(self):
        return TradeAnalyzer()

    def test_calculate_player_value_base(self, analyzer):
        """Test base player value calculation."""
        player = {"position": "RB", "player_id": "1"}
        value = analyzer._calculate_player_value(player, None)
        assert value > 0
        assert value <= 100

    def test_calculate_player_value_position_multiplier(self, analyzer):
        """Test position-based value adjustment."""
        rb = {"position": "RB", "player_id": "1"}
        qb = {"position": "QB", "player_id": "1"}
        k = {"position": "K", "player_id": "1"}
        
        rb_value = analyzer._calculate_player_value(rb, None)
        qb_value = analyzer._calculate_player_value(qb, None)
        k_value = analyzer._calculate_player_value(k, None)
        
        # RB should be worth more than QB due to scarcity
        assert rb_value >= qb_value

    def test_calculate_player_value_trending(self, analyzer):
        """Test trending player value boost."""
        player = {
            "position": "RB",
            "player_id": "1"
        }
        trending_data = {
            "trending_players": [
                {"player_id": "1", "count": 5}
            ]
        }
        
        value_with_trending = analyzer._calculate_player_value(player, None, trending_data)
        value_without_trending = analyzer._calculate_player_value(player, None, None)
        
        assert value_with_trending > value_without_trending

    def test_calculate_player_value_injury(self, analyzer):
        """Test injury status affects value."""
        healthy = {"position": "RB", "player_id": "1", "practice_status": "Full"}
        dnp = {"position": "RB", "player_id": "1", "practice_status": "DNP"}
        lp = {"position": "RB", "player_id": "1", "practice_status": "LP"}
        
        healthy_value = analyzer._calculate_player_value(healthy, None)
        dnp_value = analyzer._calculate_player_value(dnp, None)
        lp_value = analyzer._calculate_player_value(lp, None)
        
        assert healthy_value > dnp_value
        assert lp_value > dnp_value

    def test_calculate_positional_needs(self, analyzer):
        """Test positional needs calculation."""
        roster = {
            "players_enriched": [
                {"position": "RB"},
                {"position": "RB"},
                {"position": "WR"},
                {"position": "WR"},
                {"position": "QB"},
            ],
            "starters_enriched": [
                {"position": "QB"},
                {"position": "RB"},
                {"position": "WR"},
            ]
        }
        
        needs = analyzer._calculate_positional_needs(roster)
        
        assert "RB" in needs
        assert "WR" in needs
        assert "QB" in needs

    def test_evaluate_trade_fairness_fair(self, analyzer):
        """Test fair trade evaluation."""
        team1_gives = [{"calculated_value": 80}]
        team2_gives = [{"calculated_value": 78}]
        team1_needs = {"RB": 5, "WR": 3}
        team2_needs = {"WR": 4, "TE": 6}
        
        recommendation, score, details = analyzer._evaluate_trade_fairness(
            team1_gives, team2_gives, team1_needs, team2_needs
        )
        
        assert recommendation in ["fair", "slightly_favors_team_1", "slightly_favors_team_2"]
        assert score >= 75


class TestAnalyzeTrade:
    """Test analyze_trade async function."""

    @pytest.mark.asyncio
    async def test_analyze_trade_missing_params(self):
        """Test with missing required parameters."""
        result = await analyze_trade("", 1, 2, [], ["123"])
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_trade_rosters_not_found(self):
        """Test with non-existent rosters."""
        mock_result = {"success": True, "rosters": [{"roster_id": "1"}, {"roster_id": "2"}]}
        
        async def mock_get_rosters(league_id):
            return mock_result
        
        with patch('nfl_mcp.trade_analyzer_tools.get_rosters', side_effect=mock_get_rosters):
            result = await analyze_trade("league1", 999, 888, ["1"], ["2"])
            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_trade_success(self):
        """Test successful trade analysis."""
        mock_roster1 = {
            "roster_id": 1,
            "players_enriched": [
                {"player_id": "1", "full_name": "Player 1", "position": "RB"},
                {"player_id": "2", "full_name": "Player 2", "position": "WR"},
            ],
            "starters_enriched": []
        }
        mock_roster2 = {
            "roster_id": 2,
            "players_enriched": [
                {"player_id": "3", "full_name": "Player 3", "position": "QB"},
                {"player_id": "4", "full_name": "Player 4", "position": "TE"},
            ],
            "starters_enriched": []
        }
        
        async def mock_get_rosters(league_id):
            return {"success": True, "rosters": [mock_roster1, mock_roster2]}
        
        async def mock_get_trending(nfl_db, *args):
            return {"success": True, "trending_players": []}
        
        with patch('nfl_mcp.trade_analyzer_tools.get_rosters', side_effect=mock_get_rosters):
            with patch('nfl_mcp.trade_analyzer_tools.get_trending_players', side_effect=mock_get_trending):
                result = await analyze_trade("league1", 1, 2, ["1", "2"], ["3", "4"])
                
                assert result["success"] is True
                assert result["data"]["recommendation"] is not None
                assert result["data"]["fairness_score"] is not None
                assert "team1_analysis" in result["data"]
                assert "team2_analysis" in result["data"]

    @pytest.mark.asyncio
    async def test_analyze_trade_warnings(self):
        """Test that warnings are generated for problematic trades."""
        mock_roster1 = {
            "roster_id": 1,
            "players_enriched": [
                {"player_id": "1", "full_name": "Injured Player", "position": "RB", "practice_status": "DNP"},
            ],
            "starters_enriched": []
        }
        mock_roster2 = {
            "roster_id": 2,
            "players_enriched": [
                {"player_id": "2", "full_name": "Player 2", "position": "QB"},
            ],
            "starters_enriched": []
        }
        
        async def mock_get_rosters(league_id):
            return {"success": True, "rosters": [mock_roster1, mock_roster2]}
        
        async def mock_get_trending(nfl_db, *args):
            return {"success": True, "trending_players": []}
        
        with patch('nfl_mcp.trade_analyzer_tools.get_rosters', side_effect=mock_get_rosters):
            with patch('nfl_mcp.trade_analyzer_tools.get_trending_players', side_effect=mock_get_trending):
                result = await analyze_trade("league1", 1, 2, ["1"], ["2"])
                
                assert result["success"] is True
                # Should have warnings about injury
                assert len(result["data"]["warnings"]) > 0
                assert any("DNP" in w for w in result["data"]["warnings"])
