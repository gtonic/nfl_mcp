"""
Tests for lineup_optimizer_tools module - start/sit recommendations.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import asdict

from nfl_mcp import lineup_optimizer_tools
from nfl_mcp.lineup_optimizer_tools import (
    LineupOptimizer,
    PlayerAnalysis,
    StartSitDecision,
    ConfidenceLevel,
    CONFIDENCE_WEIGHTS,
    MATCHUP_TIER_SCORES,
    INJURY_STATUS_SCORES,
    PRACTICE_STATUS_SCORES,
    USAGE_TREND_SCORES,
    get_start_sit_recommendation,
    get_roster_recommendations,
    compare_players_for_slot,
    analyze_full_lineup,
)


class TestPlayerAnalysis:
    """Test PlayerAnalysis dataclass."""
    
    def test_player_analysis_defaults(self):
        """Test default values for PlayerAnalysis."""
        analysis = PlayerAnalysis(
            player_name="Test Player",
            player_id="123",
            position="WR",
            team="KC",
            opponent="LV"
        )
        
        assert analysis.matchup_rank == 16
        assert analysis.matchup_tier == "neutral"
        assert analysis.target_share == 0.0
        assert analysis.snap_percentage == 0.0
        assert analysis.injury_status == "healthy"
        assert analysis.decision == "start"
        assert analysis.confidence == 50.0
    
    def test_player_analysis_to_dict(self):
        """Test to_dict conversion."""
        analysis = PlayerAnalysis(
            player_name="Test Player",
            player_id="123",
            position="WR",
            team="KC",
            opponent="LV",
            confidence=75.5
        )
        
        result = analysis.to_dict()
        
        assert isinstance(result, dict)
        assert result["player_name"] == "Test Player"
        assert result["confidence"] == 75.5
        assert "reasoning" in result


class TestConfidenceWeights:
    """Test that confidence weights are properly configured."""
    
    def test_weights_sum_to_one(self):
        """Verify weights sum to 1.0 for proper scoring."""
        total = sum(CONFIDENCE_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001
    
    def test_all_weight_factors_present(self):
        """Verify all expected weight factors are defined."""
        expected = ["matchup", "usage", "health", "projection", "trend"]
        for factor in expected:
            assert factor in CONFIDENCE_WEIGHTS


class TestMatchupTierScores:
    """Test matchup tier scoring."""
    
    def test_smash_highest_score(self):
        """Smash matchups should have highest score."""
        assert MATCHUP_TIER_SCORES["smash"] == max(MATCHUP_TIER_SCORES.values())
    
    def test_elite_lowest_score(self):
        """Elite defenses should have lowest score."""
        assert MATCHUP_TIER_SCORES["elite"] == min(MATCHUP_TIER_SCORES.values())
    
    def test_tier_ordering(self):
        """Verify tier scores are properly ordered."""
        assert MATCHUP_TIER_SCORES["smash"] > MATCHUP_TIER_SCORES["favorable"]
        assert MATCHUP_TIER_SCORES["favorable"] > MATCHUP_TIER_SCORES["neutral"]
        assert MATCHUP_TIER_SCORES["neutral"] > MATCHUP_TIER_SCORES["tough"]
        assert MATCHUP_TIER_SCORES["tough"] > MATCHUP_TIER_SCORES["elite"]


class TestInjuryStatusScores:
    """Test injury status scoring."""
    
    def test_healthy_highest_score(self):
        """Healthy status should have highest score."""
        assert INJURY_STATUS_SCORES["healthy"] == 100
    
    def test_out_zero_score(self):
        """Out status should have zero score."""
        assert INJURY_STATUS_SCORES["out"] == 0
        assert INJURY_STATUS_SCORES["ir"] == 0
    
    def test_questionable_moderate_score(self):
        """Questionable should have moderate score."""
        assert 40 <= INJURY_STATUS_SCORES["questionable"] <= 70


class TestLineupOptimizer:
    """Test LineupOptimizer class."""
    
    def test_optimizer_initialization(self):
        """Test optimizer initializes correctly."""
        optimizer = LineupOptimizer(db=MagicMock(), defense_analyzer=MagicMock())
        assert optimizer.db is not None
        assert optimizer.defense_analyzer is not None
    
    def test_calculate_confidence_healthy_smash(self):
        """Test confidence calculation for healthy player with smash matchup."""
        optimizer = LineupOptimizer(db=None, defense_analyzer=None)
        
        analysis = PlayerAnalysis(
            player_name="Star Player",
            player_id="1",
            position="WR",
            team="KC",
            opponent="LV",
            matchup_tier="smash",
            matchup_rank=30,
            snap_percentage=90,
            target_share=25,
            injury_status="healthy",
            practice_status="full",
            projected_points=18.0,
            usage_trend="upward"
        )
        
        confidence, level, reasoning = optimizer.calculate_confidence(analysis)
        
        assert confidence >= 70  # Should be high confidence
        assert level in ["high", "medium"]
        assert len(reasoning) > 0
    
    def test_calculate_confidence_injured_tough_matchup(self):
        """Test confidence calculation for injured player with tough matchup."""
        optimizer = LineupOptimizer(db=None, defense_analyzer=None)
        
        analysis = PlayerAnalysis(
            player_name="Injured Player",
            player_id="2",
            position="RB",
            team="MIA",
            opponent="SF",
            matchup_tier="elite",
            matchup_rank=3,
            snap_percentage=40,
            injury_status="questionable",
            practice_status="limited",
            usage_trend="downward"
        )
        
        confidence, level, reasoning = optimizer.calculate_confidence(analysis)
        
        assert confidence < 50  # Should be low confidence
        assert "⚠️" in str(reasoning)  # Should have warning indicators
    
    def test_determine_decision_must_start(self):
        """Test must_start decision for high confidence + good matchup."""
        optimizer = LineupOptimizer(db=None, defense_analyzer=None)
        
        decision = optimizer.determine_decision(
            confidence=85,
            matchup_tier="smash",
            health_score=100
        )
        
        assert decision == "must_start"
    
    def test_determine_decision_must_sit_injured(self):
        """Test must_sit decision for injured player."""
        optimizer = LineupOptimizer(db=None, defense_analyzer=None)
        
        decision = optimizer.determine_decision(
            confidence=60,
            matchup_tier="neutral",
            health_score=20  # Out or IR
        )
        
        assert decision == "must_sit"
    
    def test_determine_decision_flex(self):
        """Test flex decision for medium confidence."""
        optimizer = LineupOptimizer(db=None, defense_analyzer=None)
        
        decision = optimizer.determine_decision(
            confidence=55,
            matchup_tier="neutral",
            health_score=100
        )
        
        assert decision == "flex"
    
    @pytest.mark.asyncio
    async def test_analyze_player_basic(self):
        """Test basic player analysis."""
        optimizer = LineupOptimizer(db=None, defense_analyzer=None)
        
        analysis = await optimizer.analyze_player(
            player_name="Test Player",
            player_id="123",
            position="WR",
            team="KC",
            opponent="LV"
        )
        
        assert analysis.player_name == "Test Player"
        assert analysis.position == "WR"
        assert analysis.team == "KC"
        assert analysis.opponent == "LV"
        assert analysis.decision in ["must_start", "start", "flex", "sit", "must_sit"]
        assert 0 <= analysis.confidence <= 100
    
    @pytest.mark.asyncio
    async def test_analyze_player_with_usage_data(self):
        """Test player analysis with usage data."""
        optimizer = LineupOptimizer(db=None, defense_analyzer=None)
        
        usage_data = {
            "target_share": 28.5,
            "snap_percentage": 95,
            "usage_trend": "upward"
        }
        
        analysis = await optimizer.analyze_player(
            player_name="High Usage Player",
            player_id="456",
            position="WR",
            team="MIA",
            opponent="NE",
            usage_data=usage_data
        )
        
        assert analysis.target_share == 28.5
        assert analysis.snap_percentage == 95
        assert analysis.usage_trend == "upward"
    
    @pytest.mark.asyncio
    async def test_analyze_roster(self):
        """Test roster analysis groups players by position."""
        optimizer = LineupOptimizer(db=None, defense_analyzer=None)
        
        players = [
            {"name": "QB1", "player_id": "1", "position": "QB", "team": "KC", "opponent": "LV"},
            {"name": "WR1", "player_id": "2", "position": "WR", "team": "MIA", "opponent": "NE"},
            {"name": "WR2", "player_id": "3", "position": "WR", "team": "SF", "opponent": "ARI"},
        ]
        
        results = await optimizer.analyze_roster(players)
        
        assert "QB" in results
        assert "WR" in results
        assert len(results["QB"]) == 1
        assert len(results["WR"]) == 2


class TestGetStartSitRecommendation:
    """Test get_start_sit_recommendation MCP tool."""
    
    @pytest.mark.asyncio
    async def test_recommendation_returns_dict(self):
        """Test function returns proper structure."""
        result = await get_start_sit_recommendation(
            player_name="Test Player",
            position="WR",
            team="KC",
            opponent="LV"
        )
        
        assert "success" in result
        assert result["success"] is True
        assert "recommendation" in result
        assert "confidence" in result
        assert "reasoning" in result
    
    @pytest.mark.asyncio
    async def test_recommendation_with_usage_data(self):
        """Test recommendation with optional usage data."""
        result = await get_start_sit_recommendation(
            player_name="High Usage Player",
            position="WR",
            team="MIA",
            opponent="NE",
            target_share=28.5,
            snap_percentage=95
        )
        
        assert result["success"] is True
        assert result["confidence"] > 0


class TestGetRosterRecommendations:
    """Test get_roster_recommendations MCP tool."""
    
    @pytest.mark.asyncio
    async def test_roster_recommendations_multiple_players(self):
        """Test recommendations for multiple players."""
        players = [
            {"name": "QB1", "position": "QB", "team": "KC", "opponent": "LV"},
            {"name": "WR1", "position": "WR", "team": "MIA", "opponent": "NE"},
        ]
        
        result = await get_roster_recommendations(players=players)
        
        assert result["success"] is True
        assert result["total_analyzed"] == 2
        assert "recommendations" in result
        assert "by_position" in result
    
    @pytest.mark.asyncio
    async def test_roster_recommendations_empty_list(self):
        """Test error handling for empty player list."""
        from nfl_mcp.tool_registry import get_roster_recommendations as wrapper_func
        
        result = await wrapper_func(players=[])
        
        assert result["success"] is False
        assert "No players provided" in result.get("error", "")


class TestComparePlayersForSlot:
    """Test compare_players_for_slot MCP tool."""
    
    @pytest.mark.asyncio
    async def test_compare_two_players(self):
        """Test comparing two players."""
        players = [
            {"name": "Player A", "position": "WR", "team": "KC", "opponent": "LV"},
            {"name": "Player B", "position": "WR", "team": "MIA", "opponent": "NE"},
        ]
        
        result = await compare_players_for_slot(players=players, slot="WR2")
        
        assert result["success"] is True
        assert "winner" in result
        assert "comparison" in result
        assert "confidence_gap" in result
        assert "verdict" in result
        assert len(result["comparison"]) == 2
    
    @pytest.mark.asyncio
    async def test_compare_insufficient_players(self):
        """Test error handling when fewer than 2 players."""
        from nfl_mcp.tool_registry import compare_players_for_slot as wrapper_func
        
        result = await wrapper_func(players=[{"name": "Only One"}])
        
        assert result["success"] is False
        assert "Need at least 2 players" in result.get("error", "")


class TestAnalyzeFullLineup:
    """Test analyze_full_lineup MCP tool."""
    
    @pytest.mark.asyncio
    async def test_analyze_full_lineup_basic(self):
        """Test full lineup analysis."""
        lineup = {
            "QB": [{"name": "QB1", "team": "KC", "opponent": "LV"}],
            "RB": [
                {"name": "RB1", "team": "BAL", "opponent": "CIN"},
                {"name": "RB2", "team": "ATL", "opponent": "NO"}
            ],
            "WR": [
                {"name": "WR1", "position": "WR", "team": "MIA", "opponent": "NE"},
                {"name": "WR2", "position": "WR", "team": "SF", "opponent": "ARI"}
            ],
            "BENCH": [
                {"name": "Bench1", "position": "WR", "team": "DEN", "opponent": "SEA"}
            ]
        }
        
        result = await analyze_full_lineup(lineup=lineup)
        
        assert result["success"] is True
        assert "starters" in result
        assert "bench" in result
        assert "lineup_grade" in result
        assert "average_confidence" in result
        assert result["lineup_grade"] in ["A", "B", "C", "D", "F", "N/A"]
    
    @pytest.mark.asyncio
    async def test_analyze_full_lineup_empty(self):
        """Test error handling for empty lineup."""
        from nfl_mcp.tool_registry import analyze_full_lineup as wrapper_func
        
        result = await wrapper_func(lineup={})
        
        assert result["success"] is False
        assert "No lineup provided" in result.get("error", "")


class TestToolRegistryIntegration:
    """Test that lineup optimizer tools are properly registered."""
    
    def test_lineup_tools_in_registry(self):
        """Test all lineup tools are registered."""
        from nfl_mcp.tool_registry import get_all_tools
        
        tools = get_all_tools()
        tool_names = [t.__name__ for t in tools]
        
        assert "get_start_sit_recommendation" in tool_names
        assert "get_roster_recommendations" in tool_names
        assert "compare_players_for_slot" in tool_names
        assert "analyze_full_lineup" in tool_names
    
    def test_lineup_tools_import(self):
        """Test lineup_optimizer_tools module imports cleanly."""
        from nfl_mcp import lineup_optimizer_tools
        
        assert hasattr(lineup_optimizer_tools, 'get_start_sit_recommendation')
        assert hasattr(lineup_optimizer_tools, 'get_roster_recommendations')
        assert hasattr(lineup_optimizer_tools, 'compare_players_for_slot')
        assert hasattr(lineup_optimizer_tools, 'analyze_full_lineup')
        assert hasattr(lineup_optimizer_tools, 'LineupOptimizer')
        assert hasattr(lineup_optimizer_tools, 'PlayerAnalysis')


class TestEnums:
    """Test enum definitions."""
    
    def test_start_sit_decision_values(self):
        """Test StartSitDecision enum values."""
        assert StartSitDecision.MUST_START.value == "must_start"
        assert StartSitDecision.START.value == "start"
        assert StartSitDecision.FLEX.value == "flex"
        assert StartSitDecision.SIT.value == "sit"
        assert StartSitDecision.MUST_SIT.value == "must_sit"
    
    def test_confidence_level_values(self):
        """Test ConfidenceLevel enum values."""
        assert ConfidenceLevel.HIGH.value == "high"
        assert ConfidenceLevel.MEDIUM.value == "medium"
        assert ConfidenceLevel.LOW.value == "low"
