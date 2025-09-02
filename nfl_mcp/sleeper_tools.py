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


@handle_http_errors(
    default_data={"rosters": [], "count": 0},
    operation_name="fetching rosters"
)
async def get_rosters(league_id: str) -> dict:
    """
    Get all rosters in a fantasy league from Sleeper API.
    
    This tool fetches all team rosters including player IDs, starters,
    bench players, and other roster information for the specified league.
    
    Args:
        league_id: The unique identifier for the league
        
    Returns:
        A dictionary containing:
        - rosters: List of all rosters in the league
        - count: Number of rosters found
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_rosters")
    
    # Sleeper API endpoint for league rosters
    url = f"https://api.sleeper.app/v1/league/{league_id}/rosters"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response
        rosters_data = response.json()
        
        return create_success_response({
            "rosters": rosters_data,
            "count": len(rosters_data)
        })


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