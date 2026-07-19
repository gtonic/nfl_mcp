"""Tests for trade_analyzer_tools module."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from nfl_mcp.trade_analyzer_tools import (
    TradeAnalyzer, analyze_trade, ESTIMATED_REPLACEMENT_VALUE,
)


class _FakeService:
    """Minimal stand-in for PlayerValuesService (id-based lookup only)."""

    def __init__(self, by_id):
        self._by_id = {str(k): v for k, v in by_id.items()}

    async def get_values(self, **kwargs):
        return {"source": "fantasycalc", "stale": False,
                "list": list(self._by_id.values()), "by_id": self._by_id}

    def lookup(self, indexed, player_id=None, name=None, position=None):
        return self._by_id.get(str(player_id))


class TestTradeAnalyzer:
    """Test TradeAnalyzer class (real market-value based)."""

    @pytest.fixture
    def analyzer(self):
        return TradeAnalyzer()

    def test_calculate_player_value_uses_market_value(self, analyzer):
        """A player in the consensus list uses its real market value."""
        svc = _FakeService({"1": {"value": 5000, "overall_rank": 20, "position_rank": 4}})
        player = {"position": "RB", "player_id": "1"}
        value, source, market = analyzer._calculate_player_value(player, svc, {})
        assert source == "fantasycalc"
        assert value == pytest.approx(5000)
        assert market["overall_rank"] == 20

    def test_calculate_player_value_estimated_when_missing(self, analyzer):
        """A player outside the value list falls back to replacement level."""
        svc = _FakeService({})
        player = {"position": "RB", "player_id": "999"}
        value, source, market = analyzer._calculate_player_value(player, svc, {})
        assert source == "estimated"
        assert value == pytest.approx(ESTIMATED_REPLACEMENT_VALUE)
        assert market is None

    def test_calculate_player_value_injury_modifier(self, analyzer):
        """DNP practice status reduces value vs a healthy player."""
        svc = _FakeService({"1": {"value": 5000}})
        healthy = {"position": "RB", "player_id": "1"}
        dnp = {"position": "RB", "player_id": "1", "practice_status": "DNP"}
        lp = {"position": "RB", "player_id": "1", "practice_status": "LP"}
        hv, _, _ = analyzer._calculate_player_value(healthy, svc, {})
        dv, _, _ = analyzer._calculate_player_value(dnp, svc, {})
        lv, _, _ = analyzer._calculate_player_value(lp, svc, {})
        assert hv > dv
        assert lv > dv

    def test_calculate_player_value_usage_trend_modifier(self, analyzer):
        """Rising usage is worth more than declining usage."""
        svc = _FakeService({"1": {"value": 5000}})
        up = {"position": "RB", "player_id": "1", "usage_trend_overall": "up"}
        down = {"position": "RB", "player_id": "1", "usage_trend_overall": "down"}
        uv, _, _ = analyzer._calculate_player_value(up, svc, {})
        dv, _, _ = analyzer._calculate_player_value(down, svc, {})
        assert uv > dv

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

        async def mock_get_league(league_id):
            return {"success": True, "league": {"scoring_settings": {"rec": 1.0},
                    "roster_positions": ["QB", "RB", "WR", "TE"], "total_rosters": 12,
                    "settings": {"type": 0}}}

        svc = _FakeService({
            "1": {"value": 6000, "overall_rank": 5, "position_rank": 2},
            "2": {"value": 5500, "overall_rank": 8, "position_rank": 3},
            "3": {"value": 6200, "overall_rank": 4, "position_rank": 1},
            "4": {"value": 5300, "overall_rank": 9, "position_rank": 1},
        })

        with patch('nfl_mcp.trade_analyzer_tools.get_rosters', side_effect=mock_get_rosters), \
             patch('nfl_mcp.trade_analyzer_tools.get_league', side_effect=mock_get_league), \
             patch('nfl_mcp.trade_analyzer_tools.get_trending_players', side_effect=mock_get_trending), \
             patch('nfl_mcp.trade_analyzer_tools.get_values_service', return_value=svc):
            result = await analyze_trade("league1", 1, 2, ["1", "2"], ["3", "4"])

            assert result["success"] is True
            assert result["recommendation"] is not None
            assert result["fairness_score"] is not None
            assert "team1_analysis" in result
            assert "team2_analysis" in result
            assert result["value_source"] == "fantasycalc"

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

        async def mock_get_league(league_id):
            return {"success": True, "league": {"scoring_settings": {"rec": 1.0},
                    "roster_positions": ["QB", "RB", "WR", "TE"], "total_rosters": 12,
                    "settings": {"type": 0}}}

        svc = _FakeService({
            "1": {"value": 6000, "overall_rank": 5, "position_rank": 2},
            "2": {"value": 5800, "overall_rank": 6, "position_rank": 1},
        })

        with patch('nfl_mcp.trade_analyzer_tools.get_rosters', side_effect=mock_get_rosters), \
             patch('nfl_mcp.trade_analyzer_tools.get_league', side_effect=mock_get_league), \
             patch('nfl_mcp.trade_analyzer_tools.get_trending_players', side_effect=mock_get_trending), \
             patch('nfl_mcp.trade_analyzer_tools.get_values_service', return_value=svc):
            result = await analyze_trade("league1", 1, 2, ["1"], ["2"])

            assert result["success"] is True
            # Should have warnings about injury
            assert len(result["warnings"]) > 0
            assert any("DNP" in w for w in result["warnings"])
