"""
Test the trade_analyzer_tools module functionality.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from nfl_mcp import trade_analyzer_tools


class TestTradeAnalyzerModule:
    """Test the trade_analyzer_tools module functionality."""
    
    def test_module_imports_successfully(self):
        """Test that the trade_analyzer_tools module can be imported."""
        assert hasattr(trade_analyzer_tools, 'analyze_trade')
        assert hasattr(trade_analyzer_tools, 'TradeAnalyzer')
    
    def test_trade_analyzer_initialization(self):
        """Test TradeAnalyzer class can be initialized."""
        analyzer = trade_analyzer_tools.TradeAnalyzer()
        assert analyzer is not None
        assert hasattr(analyzer, 'position_tiers')
        assert 'QB' in analyzer.position_tiers
        assert 'RB' in analyzer.position_tiers
    
    def test_calculate_player_value_basic(self):
        """Test basic player value calculation."""
        analyzer = trade_analyzer_tools.TradeAnalyzer()
        
        player = {
            "player_id": "1234",
            "full_name": "Test Player",
            "position": "RB"
        }
        
        value = analyzer._calculate_player_value(player, None, None)
        
        # RB has 1.3 multiplier, so base 50 * 1.3 = 65
        assert value > 0
        assert value <= 100
        assert value == pytest.approx(65.0, abs=0.1)
    
    def test_calculate_player_value_with_practice_status(self):
        """Test player value calculation with injury status."""
        analyzer = trade_analyzer_tools.TradeAnalyzer()
        
        # Player not practicing (DNP)
        player_dnp = {
            "player_id": "1234",
            "full_name": "Injured Player",
            "position": "WR",
            "practice_status": "DNP"
        }
        
        value_dnp = analyzer._calculate_player_value(player_dnp, None, None)
        
        # Player fully practicing
        player_full = {
            "player_id": "5678",
            "full_name": "Healthy Player",
            "position": "WR",
            "practice_status": "Full"
        }
        
        value_full = analyzer._calculate_player_value(player_full, None, None)
        
        # DNP should have significantly lower value than Full
        assert value_dnp < value_full
    
    def test_calculate_player_value_with_usage_trend(self):
        """Test player value calculation with usage trends."""
        analyzer = trade_analyzer_tools.TradeAnalyzer()
        
        # Player with upward trend
        player_up = {
            "player_id": "1234",
            "full_name": "Rising Player",
            "position": "RB",
            "usage_trend_overall": "up"
        }
        
        value_up = analyzer._calculate_player_value(player_up, None, None)
        
        # Player with downward trend
        player_down = {
            "player_id": "5678",
            "full_name": "Declining Player",
            "position": "RB",
            "usage_trend_overall": "down"
        }
        
        value_down = analyzer._calculate_player_value(player_down, None, None)
        
        # Upward trend should have higher value
        assert value_up > value_down
    
    def test_calculate_positional_needs(self):
        """Test positional need calculation."""
        analyzer = trade_analyzer_tools.TradeAnalyzer()
        
        roster = {
            "players_enriched": [
                {"player_id": "1", "position": "QB"},
                {"player_id": "2", "position": "RB"},
                {"player_id": "3", "position": "RB"},
                {"player_id": "4", "position": "WR"},
                {"player_id": "5", "position": "WR"},
                {"player_id": "6", "position": "WR"},
                {"player_id": "7", "position": "TE"}
            ],
            "starters_enriched": [
                {"player_id": "1", "position": "QB"},
                {"player_id": "2", "position": "RB"},
                {"player_id": "4", "position": "WR"}
            ]
        }
        
        needs = analyzer._calculate_positional_needs(roster)
        
        assert "QB" in needs
        assert "RB" in needs
        assert "WR" in needs
        assert "TE" in needs
        
        # Should need QB more (only 1) than WR (has 3)
        assert needs["QB"] > needs["WR"]
    
    def test_evaluate_trade_fairness_balanced(self):
        """Test trade fairness evaluation for balanced trade."""
        analyzer = trade_analyzer_tools.TradeAnalyzer()
        
        team1_gives = [
            {"player_id": "1", "full_name": "Player 1", "position": "RB", "calculated_value": 70}
        ]
        team2_gives = [
            {"player_id": "2", "full_name": "Player 2", "position": "WR", "calculated_value": 72}
        ]
        
        team1_needs = {"RB": 5, "WR": 6}
        team2_needs = {"RB": 7, "WR": 4}
        
        recommendation, fairness_score, details = analyzer._evaluate_trade_fairness(
            team1_gives, team2_gives, team1_needs, team2_needs
        )
        
        assert fairness_score >= 75  # Should be fairly balanced
        assert "fair" in recommendation.lower() or "slightly" in recommendation.lower()
    
    def test_evaluate_trade_fairness_lopsided(self):
        """Test trade fairness evaluation for lopsided trade."""
        analyzer = trade_analyzer_tools.TradeAnalyzer()
        
        team1_gives = [
            {"player_id": "1", "full_name": "Star Player", "position": "RB", "calculated_value": 90}
        ]
        team2_gives = [
            {"player_id": "2", "full_name": "Bench Player", "position": "WR", "calculated_value": 30}
        ]
        
        team1_needs = {"RB": 3, "WR": 5}
        team2_needs = {"RB": 5, "WR": 3}
        
        recommendation, fairness_score, details = analyzer._evaluate_trade_fairness(
            team1_gives, team2_gives, team1_needs, team2_needs
        )
        
        assert fairness_score < 75  # Should be lopsided
        assert "needs_adjustment" in recommendation or "unfair" in recommendation


class TestTradeAnalyzerIntegration:
    """Integration tests for trade analyzer with mocked dependencies."""
    
    @pytest.mark.asyncio
    async def test_analyze_trade_missing_parameters(self):
        """Test analyze_trade with missing required parameters."""
        result = await trade_analyzer_tools.analyze_trade(
            league_id="",
            team1_roster_id=1,
            team2_roster_id=2,
            team1_gives=[],
            team2_gives=["4034"]
        )
        
        assert result["success"] is False
        assert "error" in result
        assert result["recommendation"] is None
    
    @pytest.mark.asyncio
    async def test_analyze_trade_rosters_fetch_fails(self):
        """Test analyze_trade when roster fetch fails."""
        with patch('nfl_mcp.trade_analyzer_tools.get_rosters') as mock_get_rosters:
            mock_get_rosters.return_value = {
                "success": False,
                "error": "API error",
                "rosters": []
            }
            
            result = await trade_analyzer_tools.analyze_trade(
                league_id="12345",
                team1_roster_id=1,
                team2_roster_id=2,
                team1_gives=["4034"],
                team2_gives=["4035"]
            )
            
            assert result["success"] is False
            assert "error" in result
            assert "rosters" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_analyze_trade_roster_not_found(self):
        """Test analyze_trade when roster IDs don't exist."""
        with patch('nfl_mcp.trade_analyzer_tools.get_rosters') as mock_get_rosters:
            mock_get_rosters.return_value = {
                "success": True,
                "rosters": [
                    {"roster_id": 5, "players_enriched": [], "starters_enriched": []},
                    {"roster_id": 6, "players_enriched": [], "starters_enriched": []}
                ]
            }
            
            result = await trade_analyzer_tools.analyze_trade(
                league_id="12345",
                team1_roster_id=1,
                team2_roster_id=2,
                team1_gives=["4034"],
                team2_gives=["4035"]
            )
            
            assert result["success"] is False
            assert "error" in result
            assert "roster" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_analyze_trade_successful_basic(self):
        """Test successful trade analysis with basic data."""
        with patch('nfl_mcp.trade_analyzer_tools.get_rosters') as mock_get_rosters:
            mock_get_rosters.return_value = {
                "success": True,
                "rosters": [
                    {
                        "roster_id": 1,
                        "players_enriched": [
                            {"player_id": "4034", "full_name": "Patrick Mahomes", "position": "QB"},
                            {"player_id": "4035", "full_name": "Christian McCaffrey", "position": "RB"}
                        ],
                        "starters_enriched": [
                            {"player_id": "4034", "full_name": "Patrick Mahomes", "position": "QB"}
                        ]
                    },
                    {
                        "roster_id": 2,
                        "players_enriched": [
                            {"player_id": "4036", "full_name": "Justin Jefferson", "position": "WR"},
                            {"player_id": "4037", "full_name": "Travis Kelce", "position": "TE"}
                        ],
                        "starters_enriched": [
                            {"player_id": "4036", "full_name": "Justin Jefferson", "position": "WR"}
                        ]
                    }
                ]
            }
            
            with patch('nfl_mcp.trade_analyzer_tools.get_trending_players') as mock_trending:
                mock_trending.return_value = {
                    "success": True,
                    "trending_players": []
                }
                
                result = await trade_analyzer_tools.analyze_trade(
                    league_id="12345",
                    team1_roster_id=1,
                    team2_roster_id=2,
                    team1_gives=["4034"],
                    team2_gives=["4036"],
                    include_trending=True
                )
                
                assert result["success"] is True
                assert "recommendation" in result
                assert "fairness_score" in result
                assert "team1_analysis" in result
                assert "team2_analysis" in result
                assert "trade_details" in result
                assert "warnings" in result
                
                # Check team1 analysis structure
                assert result["team1_analysis"]["roster_id"] == 1
                assert len(result["team1_analysis"]["gives"]) == 1
                assert len(result["team1_analysis"]["receives"]) == 1
                assert "positional_needs" in result["team1_analysis"]
                
                # Check team2 analysis structure
                assert result["team2_analysis"]["roster_id"] == 2
                assert len(result["team2_analysis"]["gives"]) == 1
                assert len(result["team2_analysis"]["receives"]) == 1
    
    @pytest.mark.asyncio
    async def test_analyze_trade_with_injured_player_warning(self):
        """Test trade analysis generates warning for injured players."""
        with patch('nfl_mcp.trade_analyzer_tools.get_rosters') as mock_get_rosters:
            mock_get_rosters.return_value = {
                "success": True,
                "rosters": [
                    {
                        "roster_id": 1,
                        "players_enriched": [
                            {
                                "player_id": "4034",
                                "full_name": "Injured Player",
                                "position": "RB",
                                "practice_status": "DNP"
                            }
                        ],
                        "starters_enriched": []
                    },
                    {
                        "roster_id": 2,
                        "players_enriched": [
                            {"player_id": "4036", "full_name": "Healthy Player", "position": "WR"}
                        ],
                        "starters_enriched": []
                    }
                ]
            }
            
            with patch('nfl_mcp.trade_analyzer_tools.get_trending_players') as mock_trending:
                mock_trending.return_value = {"success": False}
                
                result = await trade_analyzer_tools.analyze_trade(
                    league_id="12345",
                    team1_roster_id=1,
                    team2_roster_id=2,
                    team1_gives=["4034"],
                    team2_gives=["4036"],
                    include_trending=False
                )
                
                assert result["success"] is True
                assert len(result["warnings"]) > 0
                # Should have a warning about DNP status
                dnp_warnings = [w for w in result["warnings"] if "DNP" in w]
                assert len(dnp_warnings) > 0
    
    @pytest.mark.asyncio
    async def test_analyze_trade_multi_player_trade(self):
        """Test trade analysis with multiple players on each side."""
        with patch('nfl_mcp.trade_analyzer_tools.get_rosters') as mock_get_rosters:
            mock_get_rosters.return_value = {
                "success": True,
                "rosters": [
                    {
                        "roster_id": 1,
                        "players_enriched": [
                            {"player_id": "1", "full_name": "Player 1", "position": "RB"},
                            {"player_id": "2", "full_name": "Player 2", "position": "WR"},
                            {"player_id": "3", "full_name": "Player 3", "position": "RB"}
                        ],
                        "starters_enriched": []
                    },
                    {
                        "roster_id": 2,
                        "players_enriched": [
                            {"player_id": "4", "full_name": "Player 4", "position": "QB"},
                            {"player_id": "5", "full_name": "Player 5", "position": "TE"}
                        ],
                        "starters_enriched": []
                    }
                ]
            }
            
            with patch('nfl_mcp.trade_analyzer_tools.get_trending_players') as mock_trending:
                mock_trending.return_value = {"success": False}
                
                result = await trade_analyzer_tools.analyze_trade(
                    league_id="12345",
                    team1_roster_id=1,
                    team2_roster_id=2,
                    team1_gives=["1", "2"],
                    team2_gives=["4", "5"],
                    include_trending=False
                )
                
                assert result["success"] is True
                assert len(result["team1_analysis"]["gives"]) == 2
                assert len(result["team1_analysis"]["receives"]) == 2
                assert len(result["team2_analysis"]["gives"]) == 2
                assert len(result["team2_analysis"]["receives"]) == 2


class TestTradeAnalyzerToolRegistry:
    """Test trade analyzer tool registration and validation."""
    
    @pytest.mark.asyncio
    async def test_tool_registry_has_analyze_trade(self):
        """Test that analyze_trade is registered in tool_registry."""
        from nfl_mcp import tool_registry
        
        assert hasattr(tool_registry, 'analyze_trade')
        func = getattr(tool_registry, 'analyze_trade')
        assert callable(func)
    
    @pytest.mark.asyncio
    async def test_tool_registry_analyze_trade_validation(self):
        """Test tool_registry analyze_trade validates inputs."""
        from nfl_mcp import tool_registry
        
        # Test with invalid league_id (too long)
        result = await tool_registry.analyze_trade(
            league_id="a" * 100,  # Too long
            team1_roster_id=1,
            team2_roster_id=2,
            team1_gives=["4034"],
            team2_gives=["4035"]
        )
        
        assert result["success"] is False
        assert "error" in result
    
    @pytest.mark.asyncio
    async def test_tool_registry_analyze_trade_empty_lists(self):
        """Test tool_registry analyze_trade rejects empty player lists."""
        from nfl_mcp import tool_registry
        
        # Test with empty team1_gives
        result = await tool_registry.analyze_trade(
            league_id="12345",
            team1_roster_id=1,
            team2_roster_id=2,
            team1_gives=[],
            team2_gives=["4035"]
        )
        
        assert result["success"] is False
        assert "error" in result
        assert "empty" in result["error"].lower()
