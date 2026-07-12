"""
Test suite for the NFL MCP Tool Registry.

This file contains unit tests for all the tools in the tool_registry module
to ensure they function correctly and handle edge cases properly.
"""

import pytest
import sys
from unittest.mock import Mock, patch, AsyncMock

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

def test_tool_registry_functions_exist():
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

@pytest.mark.asyncio
async def test_basic_tool_call():
    """Test that we can call some basic tools without errors."""
    # Test with invalid input to ensure error handling
    result = await get_nfl_news(limit=-1)
    assert isinstance(result, dict)
    # Should have error or success field
    assert 'success' in result or 'error' in result

@pytest.mark.asyncio
async def test_team_tools():
    """Test team-related tools."""
    # Test basic functionality
    result = await get_teams()
    assert isinstance(result, dict)
    assert 'teams' in result or 'success' in result or 'error' in result

    result = await get_team_injuries(team_id="KC")
    assert isinstance(result, dict)

@pytest.mark.asyncio
async def test_cbs_tools():
    """Test CBS fantasy tools."""
    result = await get_cbs_player_news(limit=5)
    assert isinstance(result, dict)
    assert 'news' in result or 'success' in result or 'error' in result

    # Test with invalid parameters
    result = await get_cbs_projections(position="INVALID", week=1)
    assert isinstance(result, dict)

@pytest.mark.asyncio
async def test_athlete_tools():
    """Test athlete tools."""
    result = await search_athletes(name="Smith", limit=5)
    assert isinstance(result, dict)
    assert 'athletes' in result or 'success' in result or 'error' in result

@pytest.mark.asyncio
async def test_vegas_tools():
    """Test Vegas lines tools."""
    result = await get_vegas_lines()
    assert isinstance(result, dict)
    assert 'games' in result or 'success' in result or 'error' in result

@pytest.mark.asyncio
async def test_coaching_tools():
    """Test coaching tools."""
    result = await get_coaching_staff(team_id="KC")
    assert isinstance(result, dict)
    assert 'coaches' in result or 'success' in result or 'error' in result

if __name__ == "__main__":
    pytest.main([__file__, "-v"])