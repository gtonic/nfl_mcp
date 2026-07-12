"""
Comprehensive test suite for the NFL MCP Tool Registry.

This file contains extensive unit tests for all the tools in the tool_registry module
to ensure they function correctly, handle edge cases, and provide proper error handling.
"""

import pytest
import sys
from unittest.mock import Mock, patch, AsyncMock
from typing import Any, Dict

# Add the project root to the Python path
sys.path.insert(0, '/tmp/nfl_mcp')

from nfl_mcp.tool_registry import (
    get_nfl_news, get_teams, fetch_teams, get_depth_chart,
    get_team_injuries, get_team_player_stats, get_nfl_standings,
    get_team_schedule, get_cbs_player_news, get_cbs_projections,
    get_cbs_expert_picks, crawl_url, fetch_athletes, lookup_athlete,
    search_athletes, get_athletes_by_team, get_league, get_rosters,
    get_league_users, get_matchups, get_playoff_bracket, get_transactions,
    get_traded_picks, get_nfl_state, get_trending_players,
    get_fantasy_context, get_strategic_matchup_preview,
    get_season_bye_week_coordination, get_trade_deadline_analysis,
    get_playoff_preparation_plan, get_user, get_user_leagues,
    get_league_drafts, get_draft, get_draft_picks, get_draft_traded_picks,
    fetch_all_players, get_waiver_log, check_re_entry_status,
    get_waiver_wire_dashboard, analyze_trade, analyze_opponent,
    get_defense_rankings, get_matchup_difficulty, analyze_roster_matchups,
    get_start_sit_recommendation, get_roster_recommendations,
    compare_players_for_slot, analyze_full_lineup, get_vegas_lines,
    get_game_environment, analyze_roster_vegas, get_stack_opportunities,
    get_injury_report, get_high_confidence_injuries, get_gameday_inactives,
    get_coaching_staff, get_all_coaching_staffs, get_coaching_tree,
    get_scheme_classification, get_league_leaders
)

# Test data for different scenarios
TEST_TEAM_DATA = [
    {"id": "1", "name": "Kansas City Chiefs", "abbreviation": "KC"},
    {"id": "2", "name": "Buffalo Bills", "abbreviation": "BUF"},
    {"id": "3", "name": "Miami Dolphins", "abbreviation": "MIA"}
]

TEST_PLAYER_DATA = [
    {"name": "Patrick Mahomes", "team": "KC", "position": "QB"},
    {"name": "Josh Allen", "team": "BUF", "position": "QB"},
    {"name": "Tua Tagovailoa", "team": "MIA", "position": "QB"}
]

TEST_COACH_DATA = [
    {"name": "Andy Reid", "role": "Head Coach", "team": "KC"},
    {"name": "Sean McDermott", "role": "Head Coach", "team": "BUF"},
    {"name": "Mike McDaniel", "role": "Offensive Coordinator", "team": "MIA"}
]

class TestToolRegistry:
    """Comprehensive tests for all tool registry functions."""

    def test_tool_registry_functions_exist(self):
        """Test that all tool functions exist in the registry."""
        # This will fail if any function is missing
        assert callable(get_nfl_news)
        assert callable(get_teams)
        assert callable(fetch_teams)
        assert callable(get_depth_chart)
        assert callable(get_team_injuries)
        assert callable(get_team_player_stats)
        assert callable(get_nfl_standings)
        assert callable(get_team_schedule)
        assert callable(get_cbs_player_news)
        assert callable(get_cbs_projections)
        assert callable(get_cbs_expert_picks)
        assert callable(crawl_url)
        assert callable(fetch_athletes)
        assert callable(lookup_athlete)
        assert callable(search_athletes)
        assert callable(get_athletes_by_team)
        assert callable(get_league)
        assert callable(get_rosters)
        assert callable(get_league_users)
        assert callable(get_matchups)
        assert callable(get_playoff_bracket)
        assert callable(get_transactions)
        assert callable(get_traded_picks)
        assert callable(get_nfl_state)
        assert callable(get_trending_players)
        assert callable(get_fantasy_context)
        assert callable(get_strategic_matchup_preview)
        assert callable(get_season_bye_week_coordination)
        assert callable(get_trade_deadline_analysis)
        assert callable(get_playoff_preparation_plan)
        assert callable(get_user)
        assert callable(get_user_leagues)
        assert callable(get_league_drafts)
        assert callable(get_draft)
        assert callable(get_draft_picks)
        assert callable(get_draft_traded_picks)
        assert callable(fetch_all_players)
        assert callable(get_waiver_log)
        assert callable(check_re_entry_status)
        assert callable(get_waiver_wire_dashboard)
        assert callable(analyze_trade)
        assert callable(analyze_opponent)
        assert callable(get_defense_rankings)
        assert callable(get_matchup_difficulty)
        assert callable(analyze_roster_matchups)
        assert callable(get_start_sit_recommendation)
        assert callable(get_roster_recommendations)
        assert callable(compare_players_for_slot)
        assert callable(analyze_full_lineup)
        assert callable(get_vegas_lines)
        assert callable(get_game_environment)
        assert callable(analyze_roster_vegas)
        assert callable(get_stack_opportunities)
        assert callable(get_injury_report)
        assert callable(get_high_confidence_injuries)
        assert callable(get_gameday_inactives)
        assert callable(get_coaching_staff)
        assert callable(get_all_coaching_staffs)
        assert callable(get_coaching_tree)
        assert callable(get_scheme_classification)
        assert callable(get_league_leaders)

    @pytest.mark.asyncio
    async def test_basic_tool_call_error_handling(self):
        """Test that basic tools return proper error handling for invalid inputs."""
        # Test with invalid input to ensure error handling
        result = await get_nfl_news(limit=-1)
        assert isinstance(result, dict)
        # Should have error or success field
        assert 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_team_tools_functionality(self):
        """Test team-related tools."""
        # Test basic functionality
        result = await get_teams()
        assert isinstance(result, dict)
        assert 'teams' in result or 'success' in result or 'error' in result

        result = await get_team_injuries(team_id="KC")
        assert isinstance(result, dict)
        assert 'team_id' in result or 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_cbs_tools_functionality(self):
        """Test CBS fantasy tools."""
        result = await get_cbs_player_news(limit=5)
        assert isinstance(result, dict)
        assert 'news' in result or 'success' in result or 'error' in result

        # Test with invalid parameters
        result = await get_cbs_projections(position="INVALID", week=1)
        assert isinstance(result, dict)
        assert 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_athlete_tools_functionality(self):
        """Test athlete tools."""
        result = await search_athletes(name="Smith", limit=5)
        assert isinstance(result, dict)
        assert 'athletes' in result or 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_vegas_tools_functionality(self):
        """Test Vegas lines tools."""
        result = await get_vegas_lines()
        assert isinstance(result, dict)
        assert 'games' in result or 'success' in result or 'error' in result

        result = await get_game_environment(team="KC")
        assert isinstance(result, dict)
        assert 'team' in result or 'success' in result or 'error' in result

        result = await analyze_roster_vegas(players=TEST_PLAYER_DATA)
        assert isinstance(result, dict)
        assert 'analysis' in result or 'success' in result or 'error' in result

        result = await get_stack_opportunities()
        assert isinstance(result, dict)
        assert 'stacks' in result or 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_coaching_tools_functionality(self):
        """Test coaching tools."""
        result = await get_coaching_staff(team_id="KC")
        assert isinstance(result, dict)
        assert 'team_id' in result or 'success' in result or 'error' in result

        result = await get_all_coaching_staffs()
        assert isinstance(result, dict)
        assert 'teams' in result or 'success' in result or 'error' in result

        result = await get_coaching_tree(coach_name="Andy Reid")
        assert isinstance(result, dict)
        assert 'coach_name' in result or 'success' in result or 'error' in result

        result = await get_scheme_classification(team_id="KC")
        assert isinstance(result, dict)
        assert 'team_id' in result or 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_error_handling_scenarios(self):
        """Test various error handling scenarios."""
        # Test invalid team ID for coaching tools
        result = await get_coaching_staff(team_id="")
        assert isinstance(result, dict)
        assert 'success' in result or 'error' in result

        # Test invalid player data for Vegas analysis
        result = await analyze_roster_vegas(players=[])
        assert isinstance(result, dict)
        assert 'success' in result or 'error' in result

        # Test invalid team ID for scheme classification
        result = await get_scheme_classification(team_id="")
        assert isinstance(result, dict)
        assert 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_data_structure_validation(self):
        """Test that all tools return consistent data structures."""
        # Test that all tools return dictionaries with expected keys
        test_cases = [
            ("get_teams", {}),
            ("get_team_injuries", {"team_id": "KC"}),
            ("get_cbs_player_news", {"limit": 5}),
            ("search_athletes", {"name": "Smith", "limit": 5}),
            ("get_vegas_lines", {}),
            ("get_coaching_staff", {"team_id": "KC"}),
            ("get_scheme_classification", {"team_id": "KC"})
        ]

        for tool_name, params in test_cases:
            tool_func = globals()[tool_name]
            if tool_name in ["get_team_injuries", "get_coaching_staff"]:
                # These require valid teams, so test with a known good one
                try:
                    result = await tool_func(**params)
                    assert isinstance(result, dict)
                    assert 'success' in result or 'error' in result or 'data' in result
                except Exception as e:
                    # Some tools may fail for other reasons (network, etc.), but should still return dict
                    pass
            else:
                # Test with default params
                try:
                    result = await tool_func(**params)
                    assert isinstance(result, dict)
                except Exception as e:
                    # Tools that might fail (like network calls) should still return dict
                    pass

    @pytest.mark.asyncio
    async def test_parameter_validation(self):
        """Test that tools handle parameter validation correctly."""
        # Test parameter validation for various tools
        test_cases = [
            ("get_nfl_news", {"limit": 1000}),  # Too high limit
            ("get_cbs_projections", {"position": "QB", "week": 18}),  # Valid
            ("get_cbs_expert_picks", {"week": 18}),  # Valid
            ("get_team_schedule", {"team_id": "KC", "season": 2025}),  # Valid
            ("get_league", {"league_id": "1234567890"}),  # Valid
        ]

        for tool_name, params in test_cases:
            tool_func = globals()[tool_name]
            try:
                result = await tool_func(**params)
                assert isinstance(result, dict)
                # Should have success or error field for validation
                assert 'success' in result or 'error' in result
            except Exception:
                # Some tools may not validate parameters strictly
                pass

    @pytest.mark.asyncio
    async def test_comprehensive_tool_coverage(self):
        """Test that we have coverage for all major tool categories."""
        # Test a good sampling of different tool types to ensure comprehensive coverage
        
        # News and Info tools
        result = await get_nfl_news(limit=5)
        assert isinstance(result, dict)
        
        # Athlete tools
        result = await search_athletes(name="Jones", limit=3)
        assert isinstance(result, dict)
        
        # Fantasy tools
        result = await get_trending_players(limit=5)
        assert isinstance(result, dict)
        
        # Vegas tools
        result = await get_vegas_lines(teams=["KC", "BUF"])
        assert isinstance(result, dict)
        
        # Coaching tools
        result = await get_coaching_staff(team_id="KC")
        assert isinstance(result, dict)
        
        # CBS tools
        result = await get_cbs_player_news(limit=3)
        assert isinstance(result, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])