"""
Test the opponent_analysis_tools module functionality.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from nfl_mcp import opponent_analysis_tools


class TestOpponentAnalyzerModule:
    """Test the opponent_analysis_tools module functionality."""
    
    def test_module_imports_successfully(self):
        """Test that the opponent_analysis_tools module can be imported."""
        assert hasattr(opponent_analysis_tools, 'analyze_opponent')
        assert hasattr(opponent_analysis_tools, 'OpponentAnalyzer')
    
    def test_opponent_analyzer_initialization(self):
        """Test OpponentAnalyzer class can be initialized."""
        analyzer = opponent_analysis_tools.OpponentAnalyzer()
        assert analyzer is not None
        assert hasattr(analyzer, 'position_weights')
        assert hasattr(analyzer, 'weakness_thresholds')
        assert 'QB' in analyzer.position_weights
        assert 'RB' in analyzer.position_weights
    
    def test_assess_position_strength_empty(self):
        """Test position assessment with no players."""
        analyzer = opponent_analysis_tools.OpponentAnalyzer()
        
        assessment = analyzer._assess_position_strength([], "RB")
        
        assert assessment['strength_score'] == 0
        assert assessment['depth_count'] == 0
        assert assessment['weakness_level'] == 'critical'
        assert len(assessment['concerns']) > 0
        assert 'No players at position' in assessment['concerns']
    
    def test_assess_position_strength_strong(self):
        """Test position assessment with strong position group."""
        analyzer = opponent_analysis_tools.OpponentAnalyzer()
        
        # Create 4 healthy players with good snap counts
        players = [
            {
                "player_id": "1",
                "full_name": "Player 1",
                "position": "RB",
                "snap_pct": 80.0,
                "practice_status": "Full"
            },
            {
                "player_id": "2",
                "full_name": "Player 2",
                "position": "RB",
                "snap_pct": 70.0,
                "practice_status": "Full"
            },
            {
                "player_id": "3",
                "full_name": "Player 3",
                "position": "RB",
                "snap_pct": 50.0
            },
            {
                "player_id": "4",
                "full_name": "Player 4",
                "position": "RB",
                "snap_pct": 30.0
            }
        ]
        
        assessment = analyzer._assess_position_strength(players, "RB")
        
        assert assessment['strength_score'] >= 70
        assert assessment['depth_count'] == 4
        assert assessment['weakness_level'] == 'strong'
        assert assessment['injury_concerns'] == 0
    
    def test_assess_position_strength_weak(self):
        """Test position assessment with weak position group."""
        analyzer = opponent_analysis_tools.OpponentAnalyzer()
        
        # Create weak position: shallow depth, injured players
        players = [
            {
                "player_id": "1",
                "full_name": "Injured Player",
                "position": "WR",
                "snap_pct": 35.0,
                "practice_status": "DNP",
                "usage_trend_overall": "down"
            }
        ]
        
        assessment = analyzer._assess_position_strength(players, "WR")
        
        assert assessment['strength_score'] < 50
        assert assessment['depth_count'] == 1
        assert assessment['weakness_level'] in ['weak', 'critical']
        assert assessment['injury_concerns'] == 1
        assert len(assessment['concerns']) > 0
    
    def test_identify_starter_weaknesses_none(self):
        """Test starter weakness identification with healthy starters."""
        analyzer = opponent_analysis_tools.OpponentAnalyzer()
        
        starters = [
            {
                "player_id": "1",
                "full_name": "Healthy Starter",
                "position": "RB",
                "snap_pct": 75.0,
                "practice_status": "Full",
                "usage_trend_overall": "up"
            }
        ]
        
        weaknesses = analyzer._identify_starter_weaknesses(starters)
        
        assert len(weaknesses) == 0
    
    def test_identify_starter_weaknesses_multiple(self):
        """Test starter weakness identification with vulnerable starters."""
        analyzer = opponent_analysis_tools.OpponentAnalyzer()
        
        starters = [
            {
                "player_id": "1",
                "full_name": "Injured Starter",
                "position": "RB",
                "snap_pct": 45.0,
                "practice_status": "DNP",
                "usage_trend_overall": "down"
            },
            {
                "player_id": "2",
                "full_name": "Limited Starter",
                "position": "WR",
                "snap_pct": 55.0,
                "practice_status": "LP"
            }
        ]
        
        weaknesses = analyzer._identify_starter_weaknesses(starters)
        
        assert len(weaknesses) == 2
        assert weaknesses[0]['severity'] == 'high'  # DNP + low snap + declining
        assert weaknesses[1]['severity'] in ['moderate', 'low']  # LP
        assert any('Did not practice' in w for w in weaknesses[0]['weaknesses'])
    
    def test_generate_exploitation_strategies(self):
        """Test exploitation strategy generation."""
        analyzer = opponent_analysis_tools.OpponentAnalyzer()
        
        # Mock position assessments with weaknesses
        position_assessments = {
            "QB": {
                "strength_score": 75.0,
                "weakness_level": "strong",
                "concerns": []
            },
            "RB": {
                "strength_score": 25.0,
                "weakness_level": "critical",
                "concerns": ["Shallow depth (1 player)", "Injury concerns: Player X"]
            },
            "WR": {
                "strength_score": 40.0,
                "weakness_level": "weak",
                "concerns": ["Low snap share (avg 35%)"]
            }
        }
        
        # Mock starter weaknesses
        starter_weaknesses = [
            {
                "player_id": "1",
                "player_name": "Injured RB",
                "position": "RB",
                "weaknesses": ["Did not practice (DNP)"],
                "severity": "high"
            }
        ]
        
        strategies = analyzer._generate_exploitation_strategies(
            position_assessments,
            starter_weaknesses
        )
        
        assert len(strategies) > 0
        
        # Check for position weakness strategy
        position_strategies = [s for s in strategies if s['category'] == 'position_weakness']
        assert len(position_strategies) > 0
        
        # RB should be identified as critical weakness
        rb_strategy = next((s for s in position_strategies if s['position'] == 'RB'), None)
        assert rb_strategy is not None
        assert rb_strategy['priority'] == 'critical'
        
        # Check for starter vulnerability strategy
        starter_strategies = [s for s in strategies if s['category'] == 'starter_vulnerability']
        assert len(starter_strategies) > 0
    
    def test_analyze_opponent_roster(self):
        """Test complete opponent roster analysis."""
        analyzer = opponent_analysis_tools.OpponentAnalyzer()
        
        # Mock roster data
        roster = {
            "roster_id": 2,
            "owner_id": "user123",
            "players_enriched": [
                {
                    "player_id": "1",
                    "full_name": "QB Player",
                    "position": "QB",
                    "snap_pct": 95.0,
                    "practice_status": "Full"
                },
                {
                    "player_id": "2",
                    "full_name": "Weak RB",
                    "position": "RB",
                    "snap_pct": 30.0,
                    "practice_status": "DNP",
                    "usage_trend_overall": "down"
                },
                {
                    "player_id": "3",
                    "full_name": "WR Player 1",
                    "position": "WR",
                    "snap_pct": 80.0
                },
                {
                    "player_id": "4",
                    "full_name": "WR Player 2",
                    "position": "WR",
                    "snap_pct": 65.0
                }
            ],
            "starters_enriched": [
                {
                    "player_id": "1",
                    "full_name": "QB Player",
                    "position": "QB",
                    "snap_pct": 95.0,
                    "practice_status": "Full"
                },
                {
                    "player_id": "2",
                    "full_name": "Weak RB",
                    "position": "RB",
                    "snap_pct": 30.0,
                    "practice_status": "DNP",
                    "usage_trend_overall": "down"
                }
            ]
        }
        
        analysis = analyzer.analyze_opponent_roster(roster)
        
        assert 'vulnerability_score' in analysis
        assert 'vulnerability_level' in analysis
        assert 'position_assessments' in analysis
        assert 'starter_weaknesses' in analysis
        assert 'exploitation_strategies' in analysis
        
        # Verify vulnerability score is reasonable
        assert 0 <= analysis['vulnerability_score'] <= 100
        
        # Should have identified the weak RB as a starter weakness
        assert len(analysis['starter_weaknesses']) > 0
        
        # Should have generated strategies
        assert len(analysis['exploitation_strategies']) > 0


class TestOpponentAnalysisIntegration:
    """Test the analyze_opponent tool integration with Sleeper API."""
    
    @pytest.mark.asyncio
    async def test_analyze_opponent_missing_league_id(self):
        """Test analyze_opponent with missing league_id."""
        result = await opponent_analysis_tools.analyze_opponent(
            league_id="",
            opponent_roster_id=1
        )
        
        assert result['success'] is False
        assert 'error' in result
        assert 'league_id' in result['error'].lower()
    
    @pytest.mark.asyncio
    async def test_analyze_opponent_missing_roster_id(self):
        """Test analyze_opponent with missing roster_id."""
        result = await opponent_analysis_tools.analyze_opponent(
            league_id="12345",
            opponent_roster_id=None
        )
        
        assert result['success'] is False
        assert 'error' in result
        assert 'roster_id' in result['error'].lower()
    
    @pytest.mark.asyncio
    async def test_analyze_opponent_rosters_fetch_fails(self):
        """Test analyze_opponent when rosters fetch fails."""
        with patch('nfl_mcp.opponent_analysis_tools.get_rosters') as mock_get_rosters:
            mock_get_rosters.return_value = {
                "success": False,
                "error": "API error",
                "rosters": []
            }
            
            result = await opponent_analysis_tools.analyze_opponent(
                league_id="12345",
                opponent_roster_id=2
            )
            
            assert result['success'] is False
            assert 'error' in result
            assert 'Failed to fetch rosters' in result['error']
    
    @pytest.mark.asyncio
    async def test_analyze_opponent_roster_not_found(self):
        """Test analyze_opponent when roster is not found."""
        with patch('nfl_mcp.opponent_analysis_tools.get_rosters') as mock_get_rosters:
            mock_get_rosters.return_value = {
                "success": True,
                "rosters": [
                    {"roster_id": 1, "players_enriched": [], "starters_enriched": []}
                ]
            }
            
            result = await opponent_analysis_tools.analyze_opponent(
                league_id="12345",
                opponent_roster_id=99  # Non-existent roster
            )
            
            assert result['success'] is False
            assert 'error' in result
            assert 'not found' in result['error'].lower()
    
    @pytest.mark.asyncio
    async def test_analyze_opponent_successful_basic(self):
        """Test successful opponent analysis."""
        with patch('nfl_mcp.opponent_analysis_tools.get_rosters') as mock_get_rosters, \
             patch('nfl_mcp.opponent_analysis_tools.get_league_users') as mock_get_users:
            
            mock_get_rosters.return_value = {
                "success": True,
                "rosters": [
                    {
                        "roster_id": 2,
                        "owner_id": "user123",
                        "players_enriched": [
                            {
                                "player_id": "1",
                                "full_name": "Test QB",
                                "position": "QB",
                                "snap_pct": 90.0
                            },
                            {
                                "player_id": "2",
                                "full_name": "Test RB",
                                "position": "RB",
                                "snap_pct": 70.0
                            }
                        ],
                        "starters_enriched": [
                            {
                                "player_id": "1",
                                "full_name": "Test QB",
                                "position": "QB",
                                "snap_pct": 90.0
                            }
                        ]
                    }
                ]
            }
            
            mock_get_users.return_value = {
                "success": True,
                "users": [
                    {"user_id": "user123", "display_name": "Opponent Team"}
                ]
            }
            
            result = await opponent_analysis_tools.analyze_opponent(
                league_id="12345",
                opponent_roster_id=2
            )
            
            assert result['success'] is True
            assert 'vulnerability_score' in result
            assert 'vulnerability_level' in result
            assert 'position_assessments' in result
            assert 'exploitation_strategies' in result
            assert result['opponent_name'] == "Opponent Team"
            assert result['league_id'] == "12345"
    
    @pytest.mark.asyncio
    async def test_analyze_opponent_with_matchup_context(self):
        """Test opponent analysis with matchup context."""
        with patch('nfl_mcp.opponent_analysis_tools.get_rosters') as mock_get_rosters, \
             patch('nfl_mcp.opponent_analysis_tools.get_league_users') as mock_get_users, \
             patch('nfl_mcp.opponent_analysis_tools.get_matchups') as mock_get_matchups:
            
            mock_get_rosters.return_value = {
                "success": True,
                "rosters": [
                    {
                        "roster_id": 2,
                        "owner_id": "user123",
                        "players_enriched": [
                            {
                                "player_id": "1",
                                "full_name": "Test Player",
                                "position": "RB",
                                "snap_pct": 80.0
                            }
                        ],
                        "starters_enriched": [
                            {
                                "player_id": "1",
                                "full_name": "Test Player",
                                "position": "RB",
                                "snap_pct": 80.0
                            }
                        ]
                    }
                ]
            }
            
            mock_get_users.return_value = {"success": True, "users": []}
            
            mock_get_matchups.return_value = {
                "success": True,
                "matchups": [
                    {
                        "roster_id": 2,
                        "matchup_id": 1,
                        "points": 105.5,
                        "custom_points": 110.2
                    }
                ]
            }
            
            result = await opponent_analysis_tools.analyze_opponent(
                league_id="12345",
                opponent_roster_id=2,
                current_week=10
            )
            
            assert result['success'] is True
            assert result['matchup_context'] is not None
            assert result['matchup_context']['week'] == 10
            assert result['matchup_context']['points'] == 105.5


class TestOpponentAnalysisToolRegistry:
    """Test opponent analysis tool registration."""
    
    def test_tool_registry_has_analyze_opponent(self):
        """Test that analyze_opponent is in the tool registry."""
        from nfl_mcp.tool_registry import get_all_tools
        
        tools = get_all_tools()
        tool_names = [tool.__name__ for tool in tools]
        
        assert 'analyze_opponent' in tool_names
    
    @pytest.mark.asyncio
    async def test_tool_registry_analyze_opponent_validation(self):
        """Test tool registry wrapper validates inputs properly."""
        from nfl_mcp.tool_registry import analyze_opponent
        
        # Test with invalid league_id (empty)
        result = await analyze_opponent(league_id="", opponent_roster_id=1)
        assert result['success'] is False
        assert 'error' in result
    
    @pytest.mark.asyncio
    async def test_tool_registry_analyze_opponent_with_week(self):
        """Test tool registry wrapper handles optional current_week."""
        from nfl_mcp.tool_registry import analyze_opponent
        
        with patch('nfl_mcp.opponent_analysis_tools.analyze_opponent') as mock_analyze:
            mock_analyze.return_value = {
                "success": True,
                "vulnerability_score": 60.0
            }
            
            result = await analyze_opponent(
                league_id="12345",
                opponent_roster_id=2,
                current_week=10
            )
            
            # Verify the underlying function was called with correct params
            mock_analyze.assert_called_once()
            call_args = mock_analyze.call_args[1]
            assert call_args['league_id'] == "12345"
            assert call_args['opponent_roster_id'] == 2
            assert call_args['current_week'] == 10
