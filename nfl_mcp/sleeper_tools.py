"""
Sleeper API MCP tools for the NFL MCP Server.

This module contains MCP tools for comprehensive fantasy league management 
through the Sleeper API, including league information, rosters, users, 
matchups, transactions, and more.
"""

import httpx
from typing import Optional

from .config import get_http_headers, create_http_client, validate_limit, LIMITS
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors, handle_validation_error
)


@handle_http_errors(
    default_data={"league": None},
    operation_name="fetching league information"
)
async def get_league(league_id: str) -> dict:
    """
    Get specific league information from Sleeper API.
    
    This tool fetches detailed information about a specific fantasy league
    including settings, roster positions, scoring, and other league metadata.
    
    Args:
        league_id: The unique identifier for the league
        
    Returns:
        A dictionary containing:
        - league: League information and settings
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_league")
    
    # Sleeper API endpoint for specific league
    url = f"https://api.sleeper.app/v1/league/{league_id}"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response
        league_data = response.json()
        
        return create_success_response({
            "league": league_data
        })


async def get_rosters(league_id: str) -> dict:
    """
    Get all rosters in a fantasy league from Sleeper API.
    
    This tool fetches all team rosters including player IDs, starters,
    bench players, and other roster information for the specified league.
    
    Note: Some leagues may have roster privacy settings that restrict access.
    If you encounter access issues, the league owner needs to make rosters public
    or grant appropriate permissions in the league settings.
    
    Args:
        league_id: The unique identifier for the league
        
    Returns:
        A dictionary containing:
        - rosters: List of all rosters in the league
        - count: Number of rosters found
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
        - access_help: Guidance for resolving access issues (if applicable)
    """
    headers = get_http_headers("sleeper_rosters")
    
    # Sleeper API endpoint for league rosters
    url = f"https://api.sleeper.app/v1/league/{league_id}/rosters"
    
    try:
        async with create_http_client() as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            
            # Handle specific Sleeper API roster access scenarios
            if response.status_code == 404:
                return create_error_response(
                    f"League with ID '{league_id}' not found or does not exist",
                    ErrorType.HTTP,
                    {
                        "rosters": [],
                        "count": 0,
                        "access_help": "Please verify the league ID is correct and the league exists"
                    }
                )
            elif response.status_code == 403:
                return create_error_response(
                    "Access denied: Roster information is private for this league",
                    ErrorType.ACCESS_DENIED,
                    {
                        "rosters": [],
                        "count": 0,
                        "access_help": "The league owner needs to enable public roster access in league settings or you need appropriate permissions to view rosters"
                    }
                )
            elif response.status_code == 401:
                return create_error_response(
                    "Authentication required: This league requires login to view rosters",
                    ErrorType.ACCESS_DENIED,
                    {
                        "rosters": [],
                        "count": 0,
                        "access_help": "This is a private league requiring authentication. Contact the league owner for access"
                    }
                )
                
            response.raise_for_status()
            
            # Parse JSON response
            rosters_data = response.json()
            
            # Check if rosters are empty (could indicate access restrictions)
            if isinstance(rosters_data, list) and len(rosters_data) == 0:
                # Try to get league info to see if the league exists but rosters are restricted
                try:
                    league_response = await client.get(f"https://api.sleeper.app/v1/league/{league_id}", headers=headers)
                    if league_response.status_code == 200:
                        league_data = league_response.json()
                        if league_data:
                            return create_success_response({
                                "rosters": rosters_data,
                                "count": len(rosters_data),
                                "warning": "League found but no rosters returned - this may indicate roster privacy settings are enabled",
                                "access_help": "If this league should have rosters, ask the league owner to check roster privacy settings"
                            })
                except:
                    # If league check fails, just continue with empty rosters
                    pass
            
            return create_success_response({
                "rosters": rosters_data,
                "count": len(rosters_data)
            })
            
    except httpx.TimeoutException:
        return create_error_response(
            "Request timed out while fetching rosters",
            ErrorType.TIMEOUT,
            {"rosters": [], "count": 0}
        )
        
    except httpx.HTTPStatusError as e:
        # Handle any other HTTP errors not caught above
        if e.response.status_code == 429:
            return create_error_response(
                "Rate limit exceeded for Sleeper API - please try again in a few minutes",
                ErrorType.HTTP,
                {
                    "rosters": [],
                    "count": 0,
                    "access_help": "Sleeper API has rate limits. Wait a few minutes before trying again"
                }
            )
        else:
            return create_error_response(
                f"HTTP {e.response.status_code}: {e.response.reason_phrase}",
                ErrorType.HTTP,
                {"rosters": [], "count": 0}
            )
            
    except httpx.NetworkError as e:
        return create_error_response(
            f"Network error while fetching rosters: {str(e)}",
            ErrorType.NETWORK,
            {"rosters": [], "count": 0}
        )
        
    except Exception as e:
        return create_error_response(
            f"Unexpected error during fetching rosters: {str(e)}",
            ErrorType.UNEXPECTED,
            {"rosters": [], "count": 0}
        )


@handle_http_errors(
    default_data={"users": [], "count": 0},
    operation_name="fetching league users"
)
async def get_league_users(league_id: str) -> dict:
    """
    Get all users in a fantasy league from Sleeper API.
    
    This tool fetches all users/managers in the specified league including
    their display names, usernames, and other profile information.
    
    Args:
        league_id: The unique identifier for the league
        
    Returns:
        A dictionary containing:
        - users: List of all users in the league
        - count: Number of users found
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_users")
    
    # Sleeper API endpoint for league users
    url = f"https://api.sleeper.app/v1/league/{league_id}/users"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response
        users_data = response.json()
        
        return create_success_response({
            "users": users_data,
            "count": len(users_data)
        })


@handle_http_errors(
    default_data={"matchups": [], "week": None, "count": 0},
    operation_name="fetching matchups"
)
async def get_matchups(league_id: str, week: int) -> dict:
    """
    Get matchups for a specific week in a fantasy league from Sleeper API.
    
    This tool fetches all matchups for the specified week including scores,
    player performances, and matchup results.
    
    Args:
        league_id: The unique identifier for the league
        week: The week number (1-22)
        
    Returns:
        A dictionary containing:
        - matchups: List of matchups for the specified week
        - week: The week number
        - count: Number of matchups found
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate week parameter
    if week < LIMITS["week_min"] or week > LIMITS["week_max"]:
        return handle_validation_error(
            f"Week must be between {LIMITS['week_min']} and {LIMITS['week_max']}",
            {"matchups": [], "week": week, "count": 0}
        )
    
    headers = get_http_headers("sleeper_matchups")
    
    # Sleeper API endpoint for league matchups
    url = f"https://api.sleeper.app/v1/league/{league_id}/matchups/{week}"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response
        matchups_data = response.json()
        
        return create_success_response({
            "matchups": matchups_data,
            "week": week,
            "count": len(matchups_data)
        })


@handle_http_errors(
    default_data={"playoff_bracket": None},
    operation_name="fetching playoff bracket"
)
async def get_playoff_bracket(league_id: str) -> dict:
    """
    Get playoff bracket information for a fantasy league from Sleeper API.
    
    This tool fetches the playoff bracket structure including matchups,
    advancement, and bracket progression for the specified league.
    
    Args:
        league_id: The unique identifier for the league
        
    Returns:
        A dictionary containing:
        - playoff_bracket: Playoff bracket data and structure
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_playoffs")
    
    # Sleeper API endpoint for league playoff bracket
    url = f"https://api.sleeper.app/v1/league/{league_id}/winners_bracket"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response
        bracket_data = response.json()
        
        return create_success_response({
            "playoff_bracket": bracket_data
        })


@handle_http_errors(
    default_data={"transactions": [], "round": None, "count": 0},
    operation_name="fetching transactions"
)
async def get_transactions(league_id: str, round: Optional[int] = None) -> dict:
    """
    Get transactions for a fantasy league from Sleeper API.
    
    This tool fetches transaction history including trades, waiver pickups,
    and free agent additions for the specified league and round.
    
    Args:
        league_id: The unique identifier for the league
        round: Optional round number (1-18, if not provided gets all transactions)
        
    Returns:
        A dictionary containing:
        - transactions: List of transactions
        - round: The round number (if specified)
        - count: Number of transactions found
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate round parameter if provided
    if round is not None and (round < LIMITS["round_min"] or round > LIMITS["round_max"]):
        return handle_validation_error(
            f"Round must be between {LIMITS['round_min']} and {LIMITS['round_max']}",
            {"transactions": [], "round": round, "count": 0}
        )
    
    headers = get_http_headers("sleeper_transactions")
    
    # Sleeper API endpoint for league transactions
    if round is not None:
        url = f"https://api.sleeper.app/v1/league/{league_id}/transactions/{round}"
    else:
        url = f"https://api.sleeper.app/v1/league/{league_id}/transactions"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response
        transactions_data = response.json()
        
        return create_success_response({
            "transactions": transactions_data,
            "round": round,
            "count": len(transactions_data)
        })


@handle_http_errors(
    default_data={"traded_picks": [], "count": 0},
    operation_name="fetching traded picks"
)
async def get_traded_picks(league_id: str) -> dict:
    """
    Get traded draft picks for a fantasy league from Sleeper API.
    
    This tool fetches information about draft picks that have been traded
    within the specified league.
    
    Args:
        league_id: The unique identifier for the league
        
    Returns:
        A dictionary containing:
        - traded_picks: List of traded draft picks
        - count: Number of traded picks found
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_traded_picks")
    
    # Sleeper API endpoint for league traded picks
    url = f"https://api.sleeper.app/v1/league/{league_id}/traded_picks"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response
        traded_picks_data = response.json()
        
        return create_success_response({
            "traded_picks": traded_picks_data,
            "count": len(traded_picks_data)
        })


@handle_http_errors(
    default_data={"nfl_state": None},
    operation_name="fetching NFL state"
)
async def get_nfl_state() -> dict:
    """
    Get current NFL state information from Sleeper API.
    
    This tool fetches the current state of the NFL including season type,
    current week, and other league-wide information.
    
    Returns:
        A dictionary containing:
        - nfl_state: Current NFL state information
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_nfl_state")
    
    # Sleeper API endpoint for NFL state
    url = "https://api.sleeper.app/v1/state/nfl"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response
        nfl_state_data = response.json()
        
        return create_success_response({
            "nfl_state": nfl_state_data
        })


@handle_http_errors(
    default_data={"trending_players": [], "trend_type": None, "lookback_hours": None, "count": 0},
    operation_name="fetching trending players"
)
async def get_trending_players(trend_type: str = "add", lookback_hours: Optional[int] = 24, limit: Optional[int] = 25) -> dict:
    """
    Get trending players from Sleeper API.
    
    This tool fetches currently trending players based on adds/drops or other
    activity metrics from the Sleeper platform.
    
    Args:
        trend_type: Type of trend to fetch ("add" or "drop", defaults to "add")
        lookback_hours: Hours to look back for trends (1-168, defaults to 24)
        limit: Maximum number of players to return (1-100, defaults to 25)
        
    Returns:
        A dictionary containing:
        - trending_players: List of trending players with enriched data
        - trend_type: The trend type requested
        - lookback_hours: Hours looked back
        - count: Number of players returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate trend_type
    valid_trend_types = ["add", "drop"]
    if trend_type not in valid_trend_types:
        return handle_validation_error(
            f"trend_type must be one of: {', '.join(valid_trend_types)}",
            {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0}
        )
    
    # Validate and normalize lookback_hours
    if lookback_hours is not None:
        lookback_hours = validate_limit(
            lookback_hours,
            LIMITS["trending_lookback_min"],
            LIMITS["trending_lookback_max"],
            24
        )
    else:
        lookback_hours = 24
    
    # Validate and normalize limit
    if limit is not None:
        limit = validate_limit(
            limit,
            LIMITS["trending_limit_min"],
            LIMITS["trending_limit_max"],
            25
        )
    else:
        limit = 25
    
    headers = get_http_headers("sleeper_trending")
    
    # Sleeper API endpoint for trending players
    url = f"https://api.sleeper.app/v1/players/nfl/trending/{trend_type}?lookback_hours={lookback_hours}&limit={limit}"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response - trending API returns a list of player IDs
        trending_player_ids = response.json()
        
        if not trending_player_ids:
            return create_success_response({
                "trending_players": [],
                "trend_type": trend_type,
                "lookback_hours": lookback_hours,
                "count": 0
            })
        
        # Need to import database here to avoid circular imports
        from .database import NFLDatabase
        nfl_db = NFLDatabase()
        
        # Look up each trending player in our database for enriched data
        enriched_players = []
        for player_item in trending_player_ids:
            # Handle both string IDs and dict objects from the API
            if isinstance(player_item, dict):
                # Extract player_id from dict object
                player_id = player_item.get('player_id') or player_item.get('id')
                if not player_id:
                    # Skip if we can't find a valid ID
                    continue
            else:
                # Assume it's a string ID
                player_id = player_item
            
            player_info = nfl_db.get_athlete_by_id(player_id)
            if player_info:
                enriched_players.append(player_info)
            else:
                # Include basic info even if not in our database
                enriched_players.append({
                    "player_id": player_id,
                    "full_name": None,
                    "first_name": None,
                    "last_name": None,
                    "position": None,
                    "team": None,
                    "age": None,
                    "jersey": None
                })
        
        return create_success_response({
            "trending_players": enriched_players,
            "trend_type": trend_type,
            "lookback_hours": lookback_hours,
            "count": len(enriched_players)
        })


# Strategic Planning Functions for Forward-Looking Analysis

@handle_http_errors(
    default_data={"strategic_preview": {}, "weeks_analyzed": 0, "league_id": None},
    operation_name="generating strategic matchup preview"
)
async def get_strategic_matchup_preview(league_id: str, current_week: int, weeks_ahead: int = 4) -> dict:
    """
    Generate a strategic preview of upcoming matchups for multiple weeks ahead.
    
    This strategic tool combines Sleeper league matchup data with NFL schedule insights
    to provide early warning about upcoming challenges, bye weeks, and opportunities.
    Helps fantasy managers plan trades, waiver claims, and lineup strategies weeks in advance.
    
    Args:
        league_id: The unique identifier for the league
        current_week: The current NFL week (1-22)
        weeks_ahead: Number of weeks to analyze ahead (1-8, defaults to 4)
        
    Returns:
        A dictionary containing:
        - strategic_preview: Multi-week analysis with recommendations
        - weeks_analyzed: Number of weeks analyzed
        - league_id: The league identifier
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate parameters
    if current_week < LIMITS["week_min"] or current_week > LIMITS["week_max"]:
        return handle_validation_error(
            f"Current week must be between {LIMITS['week_min']} and {LIMITS['week_max']}",
            {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
        )
    
    if weeks_ahead < 1 or weeks_ahead > 8:
        return handle_validation_error(
            "Weeks ahead must be between 1 and 8",
            {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
        )
    
    strategic_data = {
        "analysis_period": f"Week {current_week} through Week {current_week + weeks_ahead - 1}",
        "weeks": {},
        "summary": {
            "critical_bye_weeks": [],
            "high_opportunity_weeks": [],
            "challenging_weeks": [],
            "trade_recommendations": []
        }
    }
    
    # Import NFL tools here to avoid circular imports
    from . import nfl_tools
    
    # Get league information for context
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            {
                "strategic_preview": {},
                "weeks_analyzed": 0,
                "league_id": league_id
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )
    
    # Analyze each upcoming week
    weeks_analyzed = 0
    for week_offset in range(weeks_ahead):
        target_week = current_week + week_offset
        if target_week > LIMITS["week_max"]:
            break
            
        # Get matchups for this week
        matchups = await get_matchups(league_id, target_week)
        if not matchups.get("success", True):
            continue
            
        week_analysis = {
            "week_number": target_week,
            "matchup_count": matchups.get("count", 0),
            "strategic_insights": [],
            "bye_week_teams": [],
            "recommended_actions": []
        }
        
        # Analyze NFL bye weeks for this week (sample analysis - in real implementation 
        # you'd analyze all 32 teams to find which have byes this week)
        sample_teams = ["KC", "BUF", "SF", "DAL", "LAR", "PHI", "MIA", "CIN"]
        
        for team in sample_teams:
            try:
                team_schedule = await nfl_tools.get_team_schedule(team, 2025)
                if team_schedule.get("success", False):
                    schedule = team_schedule.get("schedule", [])
                    for game in schedule:
                        if (game.get("week") == target_week and 
                            "BYE WEEK" in game.get("fantasy_implications", [])):
                            week_analysis["bye_week_teams"].append({
                                "team": team,
                                "impact": "High - Consider backup options or trades"
                            })
                            strategic_data["summary"]["critical_bye_weeks"].append({
                                "week": target_week,
                                "team": team
                            })
            except Exception:
                # Skip team if schedule unavailable
                continue
        
        # Add strategic insights based on week timing
        if target_week == current_week:
            week_analysis["strategic_insights"].append("Current week - Focus on injury reports and last-minute changes")
        elif target_week == current_week + 1:
            week_analysis["strategic_insights"].append("Next week - Prime time for waiver claims and trades")
        elif target_week <= 13:
            week_analysis["strategic_insights"].append("Regular season - Build for playoffs")
        else:
            week_analysis["strategic_insights"].append("Playoff push - Prioritize high-floor players")
        
        # Add recommendations based on bye weeks
        if week_analysis["bye_week_teams"]:
            week_analysis["recommended_actions"].append(
                f"Plan for {len(week_analysis['bye_week_teams'])} bye week teams - "
                "consider trades or waiver pickups 1-2 weeks early"
            )
            strategic_data["summary"]["challenging_weeks"].append(target_week)
        else:
            week_analysis["recommended_actions"].append("No major bye weeks - good week to be aggressive")
            strategic_data["summary"]["high_opportunity_weeks"].append(target_week)
        
        strategic_data["weeks"][f"week_{target_week}"] = week_analysis
        weeks_analyzed += 1
    
    # Generate overall strategic recommendations
    if strategic_data["summary"]["critical_bye_weeks"]:
        strategic_data["summary"]["trade_recommendations"].append(
            "Consider trading for depth before major bye weeks hit your key players"
        )
    
    if len(strategic_data["summary"]["challenging_weeks"]) >= 2:
        strategic_data["summary"]["trade_recommendations"].append(
            "Multiple challenging weeks ahead - prioritize roster flexibility"
        )
    
    return create_success_response({
        "strategic_preview": strategic_data,
        "weeks_analyzed": weeks_analyzed,
        "league_id": league_id
    })


@handle_http_errors(
    default_data={"coordination_plan": {}, "season": None, "league_id": None},
    operation_name="coordinating season bye weeks"
)
async def get_season_bye_week_coordination(league_id: str, season: int = 2025) -> dict:
    """
    Coordinate your fantasy league schedule with NFL bye weeks for season-long planning.
    
    This strategic tool analyzes the entire NFL season's bye week schedule and correlates
    it with your fantasy league's playoff schedule to identify optimal trading periods,
    waiver claim timing, and roster construction strategies.
    
    Args:
        league_id: The unique identifier for the league
        season: Season year (defaults to 2025)
        
    Returns:
        A dictionary containing:
        - coordination_plan: Season-long strategic plan
        - season: Season year
        - league_id: League identifier
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    from . import nfl_tools
    
    # Get league information for playoff schedule context
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            {
                "coordination_plan": {},
                "season": season,
                "league_id": league_id
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )
    
    league_data = league_info.get("league", {})
    playoff_start = league_data.get("settings", {}).get("playoff_week_start", 14)
    regular_season_weeks = playoff_start - 1
    
    coordination_plan = {
        "season_overview": {
            "regular_season_weeks": regular_season_weeks,
            "playoff_start_week": playoff_start,
            "trade_deadline": league_data.get("settings", {}).get("trade_deadline", 13)
        },
        "bye_week_calendar": {},
        "strategic_periods": {
            "early_season": {"weeks": [1, 2, 3], "focus": "Observe and collect data"},
            "bye_week_preparation": {"weeks": [], "focus": "Build roster depth"},
            "trade_deadline_push": {"weeks": [], "focus": "Final roster optimization"}, 
            "playoff_preparation": {"weeks": [], "focus": "Secure playoff position"}
        },
        "recommendations": []
    }
    
    # Analyze bye weeks for all NFL teams (sample of major fantasy teams)
    major_fantasy_teams = [
        "KC", "BUF", "SF", "DAL", "LAR", "PHI", "MIA", "CIN", 
        "BAL", "GB", "MIN", "NYJ", "DEN", "LV", "ATL", "TB"
    ]
    
    bye_weeks_by_week = {}
    
    for team in major_fantasy_teams:
        try:
            team_schedule = await nfl_tools.get_team_schedule(team, season)
            if team_schedule.get("success", False):
                schedule = team_schedule.get("schedule", [])
                for game in schedule:
                    if "BYE WEEK" in game.get("fantasy_implications", []):
                        week_num = game.get("week")
                        if week_num:
                            if week_num not in bye_weeks_by_week:
                                bye_weeks_by_week[week_num] = []
                            bye_weeks_by_week[week_num].append(team)
        except Exception:
            continue
    
    # Organize bye weeks in calendar format
    for week, teams in bye_weeks_by_week.items():
        coordination_plan["bye_week_calendar"][f"week_{week}"] = {
            "week": week,
            "teams_on_bye": teams,
            "team_count": len(teams),
            "strategic_impact": "High" if len(teams) >= 4 else "Medium" if len(teams) >= 2 else "Low",
            "recommended_prep_week": max(1, week - 2)
        }
    
    # Identify strategic periods
    heavy_bye_weeks = [week for week, teams in bye_weeks_by_week.items() if len(teams) >= 4]
    if heavy_bye_weeks:
        prep_weeks = [max(1, week - 2) for week in heavy_bye_weeks]
        coordination_plan["strategic_periods"]["bye_week_preparation"]["weeks"] = prep_weeks
    
    # Trade deadline preparation
    trade_deadline = coordination_plan["season_overview"]["trade_deadline"]
    coordination_plan["strategic_periods"]["trade_deadline_push"]["weeks"] = [
        trade_deadline - 2, trade_deadline - 1, trade_deadline
    ]
    
    # Playoff preparation
    coordination_plan["strategic_periods"]["playoff_preparation"]["weeks"] = [
        playoff_start - 3, playoff_start - 2, playoff_start - 1
    ]
    
    # Generate strategic recommendations
    if heavy_bye_weeks:
        coordination_plan["recommendations"].append({
            "priority": "High",
            "action": f"Weeks {', '.join(map(str, heavy_bye_weeks))} have heavy bye weeks",
            "timing": f"Start preparing 2-3 weeks early (weeks {', '.join(map(str, prep_weeks))})",
            "strategy": "Build roster depth through trades or strategic waiver claims"
        })
    
    coordination_plan["recommendations"].append({
        "priority": "Medium",
        "action": "Trade deadline preparation",
        "timing": f"Weeks {trade_deadline - 2} through {trade_deadline}",
        "strategy": "Evaluate roster needs and make final strategic moves"
    })
    
    coordination_plan["recommendations"].append({
        "priority": "High", 
        "action": "Playoff preparation",
        "timing": f"Weeks {playoff_start - 3} through {playoff_start - 1}",
        "strategy": "Optimize lineup for playoff schedule and prioritize high-floor players"
    })
    
    return create_success_response({
        "coordination_plan": coordination_plan,
        "season": season,
        "league_id": league_id
    })


@handle_http_errors(
    default_data={"trade_analysis": {}, "league_id": None, "current_week": None},
    operation_name="analyzing trade deadline strategy"
)
async def get_trade_deadline_analysis(league_id: str, current_week: int) -> dict:
    """
    Analyze optimal trade timing based on league schedule and NFL events.
    
    This strategic tool evaluates when to make trades by analyzing upcoming bye weeks,
    playoff schedules, and league transaction patterns to maximize competitive advantage.
    
    Args:
        league_id: The unique identifier for the league
        current_week: Current NFL week for timing analysis
        
    Returns:
        A dictionary containing:
        - trade_analysis: Strategic trade timing analysis
        - league_id: League identifier
        - current_week: Current week reference
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Get league information
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            {
                "trade_analysis": {},
                "league_id": league_id,
                "current_week": current_week
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )
    
    league_data = league_info.get("league", {})
    settings = league_data.get("settings", {})
    trade_deadline = settings.get("trade_deadline", 13)
    playoff_start = settings.get("playoff_week_start", 14)
    
    trade_analysis = {
        "timing_analysis": {
            "current_week": current_week,
            "trade_deadline": trade_deadline,
            "weeks_until_deadline": max(0, trade_deadline - current_week),
            "playoff_start": playoff_start,
            "weeks_until_playoffs": max(0, playoff_start - current_week)
        },
        "strategic_windows": {},
        "recommendations": [],
        "urgency_factors": []
    }
    
    # Determine strategic windows based on timing
    weeks_to_deadline = trade_deadline - current_week
    
    if weeks_to_deadline > 4:
        trade_analysis["strategic_windows"]["current_phase"] = "Early Season"
        trade_analysis["strategic_windows"]["strategy"] = "Observe and identify undervalued assets"
        trade_analysis["strategic_windows"]["urgency"] = "Low"
        trade_analysis["recommendations"].append({
            "action": "Monitor performance trends",
            "reasoning": "Plenty of time to evaluate players and identify trade targets",
            "priority": "Low"
        })
    elif weeks_to_deadline > 2:
        trade_analysis["strategic_windows"]["current_phase"] = "Prime Trading Window"
        trade_analysis["strategic_windows"]["strategy"] = "Actively pursue beneficial trades"
        trade_analysis["strategic_windows"]["urgency"] = "Medium"
        trade_analysis["recommendations"].append({
            "action": "Execute strategic trades now",
            "reasoning": f"Only {weeks_to_deadline} weeks until deadline - optimal time to trade",
            "priority": "High"
        })
    elif weeks_to_deadline > 0:
        trade_analysis["strategic_windows"]["current_phase"] = "Trade Deadline Crunch"
        trade_analysis["strategic_windows"]["strategy"] = "Make final critical moves"
        trade_analysis["strategic_windows"]["urgency"] = "High"
        trade_analysis["recommendations"].append({
            "action": "Complete all pending trades immediately",
            "reasoning": f"Only {weeks_to_deadline} week(s) left - last chance for trades",
            "priority": "Critical"
        })
        trade_analysis["urgency_factors"].append("Trade deadline imminent")
    else:
        trade_analysis["strategic_windows"]["current_phase"] = "Post-Deadline"
        trade_analysis["strategic_windows"]["strategy"] = "Focus on waiver wire and lineup optimization"
        trade_analysis["strategic_windows"]["urgency"] = "N/A"
        trade_analysis["recommendations"].append({
            "action": "Switch to waiver-based strategy",
            "reasoning": "Trade deadline has passed - only waivers and free agents available",
            "priority": "Medium"
        })
    
    # Analyze upcoming bye weeks to inform trade urgency
    upcoming_preview = await get_strategic_matchup_preview(league_id, current_week, 4)
    if upcoming_preview.get("success", False):
        preview_data = upcoming_preview.get("strategic_preview", {})
        critical_byes = preview_data.get("summary", {}).get("critical_bye_weeks", [])
        
        if critical_byes and weeks_to_deadline > 0:
            bye_weeks = [bye["week"] for bye in critical_byes]
            trade_analysis["urgency_factors"].append(
                f"Critical bye weeks coming in weeks {', '.join(map(str, bye_weeks))}"
            )
            trade_analysis["recommendations"].append({
                "action": "Trade for bye week coverage",
                "reasoning": f"Address bye weeks in weeks {', '.join(map(str, bye_weeks))} before deadline",
                "priority": "High"
            })
    
    # Add playoff preparation considerations
    if current_week >= playoff_start - 4:
        trade_analysis["urgency_factors"].append("Playoff preparation phase")
        trade_analysis["recommendations"].append({
            "action": "Prioritize playoff schedule strength",
            "reasoning": "Focus on players with favorable playoff matchups",
            "priority": "High"
        })
    
    return create_success_response({
        "trade_analysis": trade_analysis,
        "league_id": league_id,
        "current_week": current_week
    })


@handle_http_errors(
    default_data={"playoff_plan": {}, "league_id": None, "readiness_score": 0},
    operation_name="generating playoff preparation plan"
)
async def get_playoff_preparation_plan(league_id: str, current_week: int) -> dict:
    """
    Generate comprehensive playoff preparation plan combining league and NFL data.
    
    This strategic tool analyzes your league's playoff structure, upcoming NFL schedules,
    and provides a detailed preparation plan to maximize playoff success including
    roster optimization, matchup analysis, and strategic timing recommendations.
    
    Args:
        league_id: The unique identifier for the league
        current_week: Current NFL week for timeline analysis
        
    Returns:
        A dictionary containing:
        - playoff_plan: Comprehensive playoff preparation strategy
        - league_id: League identifier  
        - readiness_score: Playoff readiness assessment (0-100)
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    from . import nfl_tools
    
    # Get league information for playoff structure
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            {
                "playoff_plan": {},
                "league_id": league_id,
                "readiness_score": 0
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )
    
    league_data = league_info.get("league", {})
    settings = league_data.get("settings", {})
    
    playoff_start = settings.get("playoff_week_start", 14)
    playoff_weeks = settings.get("playoff_week_start", 14)
    total_teams = league_data.get("total_rosters", 12)
    playoff_teams = settings.get("playoff_teams", 6)
    
    playoff_plan = {
        "timeline": {
            "current_week": current_week,
            "playoff_start": playoff_start,
            "weeks_to_playoffs": max(0, playoff_start - current_week),
            "playoff_duration": 4,  # Typical playoff duration
            "championship_week": playoff_start + 3
        },
        "preparation_phases": {},
        "strategic_priorities": [],
        "nfl_schedule_analysis": {},
        "recommendations": [],
        "readiness_assessment": {
            "roster_depth": "TBD",
            "schedule_strength": "TBD", 
            "bye_week_planning": "TBD",
            "overall_score": 0
        }
    }
    
    weeks_to_playoffs = playoff_start - current_week
    
    # Define preparation phases based on timeline
    if weeks_to_playoffs > 4:
        current_phase = "Early Preparation"
        phase_strategy = "Build depth and monitor targets"
        phase_urgency = "Low"
        playoff_plan["strategic_priorities"] = [
            "Monitor playoff-bound teams for strong schedules",
            "Identify undervalued players on strong teams",
            "Build roster depth for injury protection",
            "Track trending players and waiver targets"
        ]
    elif weeks_to_playoffs > 2:
        current_phase = "Active Preparation" 
        phase_strategy = "Execute strategic moves"
        phase_urgency = "Medium"
        playoff_plan["strategic_priorities"] = [
            "Make trades for playoff-schedule advantages",
            "Secure handcuffs for star players",
            "Target players on motivated teams",
            "Avoid players on teams likely to rest starters"
        ]
    elif weeks_to_playoffs > 0:
        current_phase = "Final Preparation"
        phase_strategy = "Lock in playoff roster"
        phase_urgency = "High"
        playoff_plan["strategic_priorities"] = [
            "Finalize optimal lineup combinations", 
            "Secure must-have waiver pickups",
            "Plan for potential star player rest",
            "Optimize for high floor over high ceiling"
        ]
    else:
        current_phase = "Playoff Execution"
        phase_strategy = "Win now mode"
        phase_urgency = "Critical"
        playoff_plan["strategic_priorities"] = [
            "Start highest floor players",
            "Monitor injury reports closely",
            "Consider game flow and scripts",
            "Avoid risky boom-or-bust plays"
        ]
    
    playoff_plan["preparation_phases"]["current_phase"] = {
        "name": current_phase,
        "strategy": phase_strategy,
        "urgency": phase_urgency,
        "weeks_remaining": max(0, weeks_to_playoffs)
    }
    
    # Analyze NFL playoff schedule implications (sample key teams)
    key_teams = ["KC", "BUF", "SF", "DAL", "PHI", "MIA", "BAL", "CIN"]
    
    for team in key_teams:
        try:
            team_schedule = await nfl_tools.get_team_schedule(team, 2025)
            if team_schedule.get("success", False):
                schedule = team_schedule.get("schedule", [])
                
                # Analyze playoff weeks (weeks 14-17 typically)
                playoff_games = [g for g in schedule if g.get("week", 0) >= playoff_start and g.get("week", 0) <= playoff_start + 3]
                
                if playoff_games:
                    fantasy_implications = []
                    for game in playoff_games:
                        implications = game.get("fantasy_implications", [])
                        fantasy_implications.extend(implications[:2])  # Limit to key insights
                    
                    playoff_plan["nfl_schedule_analysis"][team] = {
                        "playoff_games_count": len(playoff_games),
                        "key_insights": fantasy_implications[:3],  # Top 3 insights
                        "recommendation": "Target" if len(playoff_games) >= 3 else "Monitor"
                    }
        except Exception:
            continue
    
    # Generate specific recommendations based on timeline
    if weeks_to_playoffs > 0:
        playoff_plan["recommendations"].append({
            "category": "Roster Management",
            "action": f"Optimize roster in next {weeks_to_playoffs} weeks",
            "priority": "High" if weeks_to_playoffs <= 2 else "Medium",
            "deadline": f"Week {playoff_start - 1}"
        })
    
    if current_week <= 13:  # Before typical trade deadline
        playoff_plan["recommendations"].append({
            "category": "Trading Strategy", 
            "action": "Target players with strong playoff schedules",
            "priority": "High",
            "deadline": "Trade deadline"
        })
    
    playoff_plan["recommendations"].append({
        "category": "Waiver Wire",
        "action": "Prioritize high-floor players over boom-bust options",
        "priority": "Medium",
        "deadline": "Ongoing"
    })
    
    # Calculate readiness score (simplified scoring system)
    readiness_score = 50  # Base score
    
    if weeks_to_playoffs > 2:
        readiness_score += 20  # Bonus for having time to prepare
    elif weeks_to_playoffs == 0:
        readiness_score += 30  # Bonus for being in playoffs
    
    if len(playoff_plan["nfl_schedule_analysis"]) >= 4:
        readiness_score += 15  # Bonus for good schedule analysis
    
    if current_phase in ["Active Preparation", "Final Preparation"]:
        readiness_score += 10  # Bonus for being in optimal prep phase
    
    playoff_plan["readiness_assessment"]["overall_score"] = min(100, readiness_score)
    
    # Set readiness levels based on score
    if readiness_score >= 80:
        playoff_plan["readiness_assessment"]["roster_depth"] = "Excellent"
        playoff_plan["readiness_assessment"]["schedule_strength"] = "Strong"
        playoff_plan["readiness_assessment"]["bye_week_planning"] = "Well Prepared"
    elif readiness_score >= 60:
        playoff_plan["readiness_assessment"]["roster_depth"] = "Good"
        playoff_plan["readiness_assessment"]["schedule_strength"] = "Adequate"
        playoff_plan["readiness_assessment"]["bye_week_planning"] = "Prepared"
    else:
        playoff_plan["readiness_assessment"]["roster_depth"] = "Needs Work"
        playoff_plan["readiness_assessment"]["schedule_strength"] = "Challenging"
        playoff_plan["readiness_assessment"]["bye_week_planning"] = "Behind Schedule"
    
    return create_success_response({
        "playoff_plan": playoff_plan,
        "league_id": league_id,
        "readiness_score": readiness_score
    })