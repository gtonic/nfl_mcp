"""Tests for lineup_optimizer_tools module."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nfl_mcp.lineup_optimizer_tools import (
    LineupOptimizer, PlayerAnalysis,
    get_lineup_optimizer,
    get_start_sit_recommendation,
    get_roster_recommendations,
    compare_players_for_slot,
    analyze_full_lineup,
    CONFIDENCE_WEIGHTS,
    INJURY_STATUS_SCORES,
    PRACTICE_STATUS_SCORES,
    MATCHUP_TIER_SCORES,
)


class TestPlayerAnalysis:
    """Test PlayerAnalysis dataclass."""

    def test_player_analysis_default_values(self):
        """Test default values for PlayerAnalysis."""
        analysis = PlayerAnalysis(
            player_name="Test Player",
            player_id="1",
            position="QB",
            team="KC",
            opponent="LV"
        )
        
        assert analysis.matchup_rank == 16
        assert analysis.matchup_tier == "neutral"
        assert analysis.decision == "start"
        assert analysis.confidence == 50.0
        assert analysis.projected_points == 0.0

    def test_player_analysis_to_dict(self):
        """Test PlayerAnalysis.to_dict method."""
        analysis = PlayerAnalysis(
            player_name="Test Player",
            player_id="1",
            position="QB",
            team="KC",
            opponent="LV",
            confidence=75.5,
            decision="must_start"
        )
        
        d = analysis.to_dict()
        
        assert d["player_name"] == "Test Player"
        assert d["confidence"] == 75.5
        assert d["decision"] == "must_start"
        assert "reasoning" in d


class TestConfidenceWeightValues:
    """Test weight and score constants."""

    def test_confidence_weights_sum_to_one(self):
        """Test that confidence weights (fractions) sum to 1.0."""
        total = sum(CONFIDENCE_WEIGHTS.values())
        assert total == pytest.approx(1.0)

    def test_injury_status_scores_complete(self):
        """Test injury status scores have expected values."""
        assert INJURY_STATUS_SCORES["healthy"] == 100
        assert INJURY_STATUS_SCORES["out"] == 0

    def test_matchup_tier_scores_complete(self):
        """Test matchup tier scores."""
        assert MATCHUP_TIER_SCORES["smash"] == 95
        assert MATCHUP_TIER_SCORES["elite"] == 10


class TestLineupOptimizer:
    """Test LineupOptimizer class."""

    @pytest.fixture
    def optimizer(self):
        return LineupOptimizer(db=None)

    def test_calculate_confidence_fully_healthy(self, optimizer):
        """Test confidence calculation for healthy player with good stats."""
        analysis = PlayerAnalysis(
            player_name="Test Player",
            player_id="1",
            position="RB",
            team="KC",
            opponent="LV",
            matchup_rank=5,
            matchup_tier="smash",
            snap_percentage=90.0,
            target_share=30.0,
            injury_status="healthy",
            practice_status="full",
            projected_points=18.0,
            usage_trend="upward"
        )
        
        confidence, level, reasoning = optimizer.calculate_confidence(analysis)
        
        assert confidence >= 70  # Should be high confidence
        assert level == "high"
        assert len(reasoning) > 0

    def test_calculate_confidence_injured(self, optimizer):
        """Test confidence calculation for injured player."""
        analysis = PlayerAnalysis(
            player_name="Injured Player",
            player_id="2",
            position="WR",
            team="KC",
            opponent="LV",
            matchup_rank=10,
            matchup_tier="neutral",
            snap_percentage=50.0,
            injury_status="questionable",
            practice_status="dnp",
            projected_points=10.0,
            usage_trend="stable"
        )
        
        confidence, level, reasoning = optimizer.calculate_confidence(analysis)
        
        assert confidence < 60  # Lower confidence due to injury
        assert any("injury" in r.lower() or "practice" in r.lower() for r in reasoning)

    def test_determine_decision_must_start(self, optimizer):
        """Test must_start decision."""
        decision = optimizer.determine_decision(85.0, "smash", 100.0)
        assert decision == "must_start"

    def test_determine_decision_must_sit(self, optimizer):
        """Test must_sit decision."""
        decision = optimizer.determine_decision(30.0, "elite", 20.0)
        assert decision == "must_sit"

    def test_determine_decision_flex(self, optimizer):
        """Test flex decision."""
        decision = optimizer.determine_decision(60.0, "neutral", 80.0)
        assert decision == "flex"

    def test_determine_decision_sit(self, optimizer):
        """Test sit decision."""
        decision = optimizer.determine_decision(40.0, "neutral", 80.0)
        assert decision == "sit"

    def test_determine_decision_auto_sit_injured(self, optimizer):
        """Test auto-sit for injured players."""
        decision = optimizer.determine_decision(80.0, "smash", 20.0)
        assert decision == "must_sit"


class TestGetLineupOptimizer:
    """Test get_lineup_optimizer singleton."""

    def test_singleton_behavior(self):
        """Test that get_lineup_optimizer returns same instance."""
        optimizer1 = get_lineup_optimizer()
        optimizer2 = get_lineup_optimizer()
        assert optimizer1 is optimizer2


class TestStartSitRecommendation:
    """Test get_start_sit_recommendation function."""

    @pytest.mark.asyncio
    async def test_start_sit_recommendation_with_data(self):
        """Test start/sit recommendation with full data."""
        result = await get_start_sit_recommendation(
            player_name="Tyreek Hill",
            position="WR",
            team="MIA",
            opponent="NE",
            target_share=28.5,
            snap_percentage=95.0,
            injury_status="healthy",
            practice_status="full",
            projected_points=22.5
        )
        
        assert result["success"] is True
        assert "recommendation" in result
        assert "confidence" in result
        assert "reasoning" in result

    @pytest.mark.asyncio
    async def test_start_sit_recommendation_minimal(self):
        """Test start/sit recommendation with minimal data."""
        result = await get_start_sit_recommendation(
            player_name="Test Player",
            position="QB",
            team="KC",
            opponent="LV"
        )
        
        assert result["success"] is True
        assert "recommendation" in result


class TestGetRosterRecommendations:
    """Test get_roster_recommendations function."""

    @pytest.mark.asyncio
    async def test_roster_recommendations_empty_list(self):
        """Test with empty player list."""
        result = await get_roster_recommendations([])
        
        assert result["success"] is False
        assert "validation" in result["error_type"].lower()

    @pytest.mark.asyncio
    async def test_roster_recommendations_single_player(self):
        """Test with single player."""
        players = [
            {
                "name": "Patrick Mahomes",
                "position": "QB",
                "team": "KC",
                "opponent": "LV",
                "projection": {"projected_points": 25.0}
            }
        ]
        
        result = await get_roster_recommendations(players)
        
        assert result["success"] is True
        assert "recommendations" in result
        assert len(result["recommendations"]) == 1

    @pytest.mark.asyncio
    async def test_roster_recommendations_multiple_players(self):
        """Test with multiple players."""
        players = [
            {
                "name": f"Player {i}",
                "position": ["QB", "RB", "WR", "TE"][i % 4],
                "team": "KC",
                "opponent": "LV"
            }
            for i in range(4)
        ]
        
        result = await get_roster_recommendations(players)
        
        assert result["success"] is True
        assert result["total_analyzed"] == 4


class TestComparePlayersForSlot:
    """Test compare_players_for_slot function."""

    @pytest.mark.asyncio
    async def test_compare_players_less_than_two(self):
        """Test comparison with less than 2 players."""
        result = await compare_players_for_slot([
            {"name": "Player 1", "position": "WR", "team": "KC", "opponent": "LV"}
        ])
        
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_compare_players_success(self):
        """Test successful player comparison."""
        players = [
            {"name": "Tyreek Hill", "position": "WR", "team": "MIA", "opponent": "NE"},
            {"name": "Stefon Diggs", "position": "WR", "team": "BUF", "opponent": "PHI"}
        ]
        
        result = await compare_players_for_slot(players)
        
        assert result["success"] is True
        assert "winner" in result
        assert "comparison" in result
        assert len(result["comparison"]) == 2
        assert "verdict" in result


class TestAnalyzeFullLineup:
    """Test analyze_full_lineup function."""

    @pytest.mark.asyncio
    async def test_analyze_lineup_empty(self):
        """Test with empty lineup."""
        result = await analyze_full_lineup({})
        
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_lineup_with_starters(self):
        """Test lineup analysis with starters."""
        lineup = {
            "QB": [{"name": "Patrick Mahomes", "position": "QB", "team": "KC", "opponent": "LV"}],
            "RB": [{"name": "Isiah Pacheco", "position": "RB", "team": "KC", "opponent": "LV"}],
            "WR": [{"name": "DeAndre Hopkins", "position": "WR", "team": "KC", "opponent": "LV"}],
            "TE": [{"name": "Travis Kelce", "position": "TE", "team": "KC", "opponent": "LV"}],
            "BENCH": [{"name": "Bench Player", "position": "RB", "team": "KC", "opponent": "LV"}]
        }
        
        result = await analyze_full_lineup(lineup)
        
        assert result["success"] is True
        assert "starters" in result
        assert "bench" in result
        assert "lineup_grade" in result
        assert "total_projected" in result

    @pytest.mark.asyncio
    async def test_analyze_lineup_with_flex(self):
        """Test lineup analysis with flex position."""
        lineup = {
            "QB": [{"name": "Mahomes", "position": "QB", "team": "KC", "opponent": "LV"}],
            "FLEX": [{"name": "Pacheco", "position": "RB", "team": "KC", "opponent": "LV"}],
        }
        
        result = await analyze_full_lineup(lineup)
        
        assert result["success"] is True
        assert "starters" in result


class TestAutoProjection:
    """The optimizer fills projected points itself when the caller doesn't."""

    def _fresh_defense(self):
        # Own analyzer instance (NOT the global singleton) with a no-network fetch,
        # so mutating it can't leak into other tests.
        from unittest.mock import AsyncMock
        from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
        da = DefenseRankingsAnalyzer(db=None)
        da.fetch_defense_rankings = AsyncMock(return_value={})
        return da

    @pytest.mark.asyncio
    async def test_analyze_player_auto_projects(self):
        from unittest.mock import patch
        from nfl_mcp import lineup_optimizer_tools as lo

        opt = lo.LineupOptimizer(db=None, auto_project=True, defense_analyzer=self._fresh_defense())

        class FakeEngine:
            async def project_many(self, players):
                return {"projections": [{"projected_points": 18.5, "floor": 12.0, "ceiling": 25.0}]}

        with patch("nfl_mcp.projections.get_projection_engine", return_value=FakeEngine()):
            analysis = await opt.analyze_player("Some WR", "", "WR", "MIA", "NE")

        assert analysis.projected_points == 18.5
        assert analysis.floor == 12.0
        assert analysis.ceiling == 25.0

    @pytest.mark.asyncio
    async def test_auto_project_disabled_leaves_zero(self):
        from nfl_mcp import lineup_optimizer_tools as lo

        opt = lo.LineupOptimizer(db=None, auto_project=False, defense_analyzer=self._fresh_defense())
        analysis = await opt.analyze_player("Some WR", "", "WR", "MIA", "NE")
        assert analysis.projected_points == 0.0
