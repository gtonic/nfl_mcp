"""Tests for opponent_analysis_tools module."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from nfl_mcp.opponent_analysis_tools import OpponentAnalyzer, analyze_opponent


class TestOpponentAnalyzer:
    """Test OpponentAnalyzer class."""

    @pytest.fixture
    def analyzer(self):
        return OpponentAnalyzer()

    def test_position_strength_empty_roster(self, analyzer):
        """Test position assessment with empty roster."""
        result = analyzer._assess_position_strength([], "RB")
        
        assert result["strength_score"] == 0
        assert result["depth_count"] == 0
        assert result["weakness_level"] == "critical"
        assert "No players at position" in result["concerns"][0]

    def test_position_strength_strong_roster(self, analyzer):
        """Test position assessment with strong roster."""
        players = [
            {"snap_pct": 85, "practice_status": "E", "usage_trend_overall": "stable"},
            {"snap_pct": 75, "practice_status": "E", "usage_trend_overall": "stable"},
            {"snap_pct": 60, "practice_status": "Q", "usage_trend_overall": "up"},
            {"snap_pct": 40, "practice_status": "E", "usage_trend_overall": "stable"},
        ]
        
        result = analyzer._assess_position_strength(players, "RB")
        
        assert result["strength_score"] >= 50
        assert result["depth_count"] == 4
        assert result["weakness_level"] == "strong"

    def test_position_strength_weak_roster(self, analyzer):
        """Test position assessment with weak roster."""
        players = [
            {"snap_pct": 30, "practice_status": "DNP", "usage_trend_overall": "down"}
        ]
        
        result = analyzer._assess_position_strength(players, "RB")
        
        assert result["weakness_level"] in ["weak", "critical"]
        assert result["injury_concerns"] == 1

    def test_position_strength_injury_concerns(self, analyzer):
        """Test injury concern detection."""
        players = [
            {"snap_pct": 70, "practice_status": "DNP", "usage_trend_overall": "stable"},
            {"snap_pct": 60, "practice_status": "LP", "usage_trend_overall": "stable"},
        ]
        
        result = analyzer._assess_position_strength(players, "WR")
        
        assert result["injury_concerns"] == 2
        assert any("injury" in c.lower() for c in result["concerns"])

    def test_identify_starter_weaknesses(self, analyzer):
        """Test starter weakness identification."""
        starters = [
            {
                "player_id": "1",
                "full_name": "John Smith",
                "position": "RB",
                "practice_status": "DNP",
                "usage_trend_overall": "down",
                "snap_pct": 45.0
            }
        ]
        
        weaknesses = analyzer._identify_starter_weaknesses(starters)
        
        assert len(weaknesses) == 1
        assert weaknesses[0]["player_name"] == "John Smith"
        assert any("DNP" in w for w in weaknesses[0]["weaknesses"])
        assert weaknesses[0]["severity"] == "high"

    def test_identify_starter_weaknesses_clean(self, analyzer):
        """Test starter weakness identification with no issues."""
        starters = [
            {
                "player_id": "1",
                "full_name": "Healthy Player",
                "position": "RB",
                "practice_status": "E",
                "usage_trend_overall": "stable",
                "snap_pct": 90.0
            }
        ]
        
        weaknesses = analyzer._identify_starter_weaknesses(starters)
        
        assert len(weaknesses) == 0

    def test_generate_exploitation_strategies(self, analyzer):
        """Test strategy generation."""
        position_assessments = {
            "RB": {
                "strength_score": 20,
                "weakness_level": "weak",
                "concerns": ["Shallow depth"]
            }
        }
        starter_weaknesses = []
        
        strategies = analyzer._generate_exploitation_strategies(position_assessments, starter_weaknesses)
        
        assert len(strategies) > 0
        assert strategies[0]["category"] == "position_weakness"
        assert strategies[0]["priority"] == "critical"

    def test_analyze_opponent_roster(self, analyzer):
        """Test comprehensive roster analysis."""
        roster = {
            "roster_id": "123",
            "owner_id": "owner1",
            "players_enriched": [
                {"player_id": "1", "full_name": "P1", "position": "RB", "snap_pct": 80, "practice_status": "E", "usage_trend_overall": "stable"},
                {"player_id": "2", "full_name": "P2", "position": "QB", "snap_pct": 30, "practice_status": "DNP", "usage_trend_overall": "down"},
            ],
            "starters_enriched": [
                {"player_id": "1", "full_name": "P1", "position": "RB"},
                {"player_id": "2", "full_name": "P2", "position": "QB"},
            ]
        }
        
        result = analyzer.analyze_opponent_roster(roster)
        
        assert "vulnerability_score" in result
        assert "vulnerability_level" in result
        assert "position_assessments" in result
        assert "exploitation_strategies" in result
        assert result["roster_id"] == "123"


class TestAnalyzeOpponent:
    """Test analyze_opponent async function."""

    @pytest.mark.asyncio
    async def test_analyze_opponent_missing_league_id(self):
        """Test with missing league_id."""
        result = await analyze_opponent("", 1)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_opponent_missing_roster_id(self):
        """Test with missing opponent_roster_id."""
        result = await analyze_opponent("league1", None)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_opponent_roster_not_found(self):
        """Test with non-existent roster."""
        mock_result = {"success": True, "rosters": [{"roster_id": "2"}]}
        
        async def mock_get_rosets(league_id):
            return mock_result
        
        with patch('nfl_mcp.opponent_analysis_tools.get_rosters', side_effect=mock_get_rosets):
            result = await analyze_opponent("league1", 999)
            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_opponent_success(self):
        """Test successful analysis."""
        mock_roster = {
            "roster_id": 1,
            "owner_id": "owner1",
            "players_enriched": [],
            "starters_enriched": []
        }
        mock_users = {"success": True, "users": [{"user_id": "owner1", "display_name": "Test User"}]}
        
        async def mock_get_rosters(league_id):
            return {"success": True, "rosters": [mock_roster]}
        
        async def mock_get_league_users(league_id):
            return mock_users
        
        with patch('nfl_mcp.opponent_analysis_tools.get_rosters', side_effect=mock_get_rosters):
            with patch('nfl_mcp.opponent_analysis_tools.get_league_users', side_effect=mock_get_league_users):
                result = await analyze_opponent("league1", 1)
                
                assert result["success"] is True
                assert result["opponent_name"] == "Test User"
                assert "vulnerability_score" in result
