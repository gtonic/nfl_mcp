"""
Sleeper API MCP tools for the NFL MCP Server.

This module contains MCP tools for comprehensive fantasy league management 
through the Sleeper API, including league information, rosters, users, 
matchups, transactions, and more.
"""

import httpx
import logging
from typing import Optional

from .config import get_http_headers, create_http_client, validate_limit, LIMITS, LONG_TIMEOUT
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors, handle_validation_error
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Player enrichment helpers
# ---------------------------------------------------------------------------
def _init_db():
    try:
        from .database import NFLDatabase
        return NFLDatabase()
    except Exception as e:
        logger.debug(f"NFLDatabase init failed (enrichment disabled): {e}")
        return None

def _enrich_single(nfl_db, pid, cache):
    if pid in cache:
        return cache[pid]
    athlete = {}
    if nfl_db:
        try:
            athlete = nfl_db.get_athlete_by_id(pid) or {}
        except Exception as e:
            logger.debug(f"Lookup failed for {pid}: {e}")
    data = {"player_id": pid, "full_name": athlete.get("full_name"), "position": athlete.get("position")}
    cache[pid] = data
    return data

def _enrich_id_list(nfl_db, ids):
    cache = {}
    return [_enrich_single(nfl_db, pid, cache) for pid in (ids or [])]


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
            
            # Enrich player IDs with name/position (additive, keeps original lists)
            try:
                from .database import NFLDatabase
                nfl_db = NFLDatabase()
                cache = {}

                def enrich_players(player_ids):
                    enriched = []
                    for pid in player_ids or []:
                        if pid in cache:
                            enriched.append(cache[pid])
                            continue
                        athlete = nfl_db.get_athlete_by_id(pid) or {}
                        obj = {
                            "player_id": pid,
                            "full_name": athlete.get("full_name"),
                            "position": athlete.get("position")
                        }
                            
                        cache[pid] = obj
                        enriched.append(obj)
                    return enriched

                if isinstance(rosters_data, list):
                    for roster in rosters_data:
                        if isinstance(roster, dict):
                            if "players" in roster and isinstance(roster["players"], list):
                                roster["players_enriched"] = enrich_players(roster["players"])
                            if "starters" in roster and isinstance(roster["starters"], list):
                                roster["starters_enriched"] = enrich_players(roster["starters"])
            except Exception as enrich_error:
                logger.debug(f"Roster enrichment skipped due to error: {enrich_error}")

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
        
        # Add enrichment for players/starters
        try:
            nfl_db = _init_db()
            cache = {}
            if isinstance(matchups_data, list):
                for m in matchups_data:
                    if not isinstance(m, dict):
                        continue
                    if isinstance(m.get("players"), list):
                        m["players_enriched"] = [_enrich_single(nfl_db, pid, cache) for pid in m["players"]]
                    if isinstance(m.get("starters"), list):
                        m["starters_enriched"] = [_enrich_single(nfl_db, pid, cache) for pid in m["starters"]]
        except Exception as e:
            logger.debug(f"Matchup enrichment skipped: {e}")

        return create_success_response({
            "matchups": matchups_data,
            "week": week,
            "count": len(matchups_data)
        })


@handle_http_errors(
    default_data={"playoff_bracket": None, "bracket_type": None},
    operation_name="fetching playoff bracket"
)
async def get_playoff_bracket(league_id: str, bracket_type: str = "winners") -> dict:
    """Get playoff bracket information (winners or losers) for a Sleeper league.

    Sleeper exposes two brackets: winners_bracket and losers_bracket. This function
    allows selecting which one to retrieve while keeping backward compatibility
    (defaulting to the winners bracket if not specified).

    Args:
        league_id: League identifier
        bracket_type: Which bracket to fetch ("winners" | "losers"), defaults to "winners"

    Returns:
        success response with keys:
        - playoff_bracket: list bracket structure
        - bracket_type: which bracket was fetched
    """
    bracket_type_normalized = bracket_type.lower().strip()
    if bracket_type_normalized not in {"winners", "losers"}:
        return handle_validation_error(
            "bracket_type must be one of: winners, losers",
            {"playoff_bracket": None, "bracket_type": bracket_type}
        )

    headers = get_http_headers("sleeper_playoffs")
    path = "winners_bracket" if bracket_type_normalized == "winners" else "losers_bracket"
    url = f"https://api.sleeper.app/v1/league/{league_id}/{path}"

    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        bracket_data = response.json()
        return create_success_response({
            "playoff_bracket": bracket_data,
            "bracket_type": bracket_type_normalized
        })


@handle_http_errors(
    default_data={"transactions": [], "week": None, "count": 0},
    operation_name="fetching transactions"
)
async def get_transactions(league_id: str, round: Optional[int] = None, week: Optional[int] = None) -> dict:
    """Get transactions for a specific week (round) of a Sleeper league.

    The official Sleeper API requires a week (round) path segment. Previous
    implementation allowed calling without a round which is not documented; now
    a week/round is required. For backward compatibility both parameter names
    are accepted; if both are provided they must match.

    Args:
        league_id: League identifier
        round: (Deprecated) alias for week
        week: Week number (1-18 typical). Required.
    """
    # Normalize parameters
    if week is None and round is not None:
        week = round
    elif week is not None and round is not None and week != round:
        return handle_validation_error(
            "Conflicting values provided for week and round; they must match",
            {"transactions": [], "week": week, "count": 0}
        )
    
    if week is None:
        return handle_validation_error(
            "A week (round) parameter is required by Sleeper: provide week= or round=",
            {"transactions": [], "week": None, "count": 0}
        )
    
    if week < LIMITS["round_min"] or week > LIMITS["round_max"]:
        return handle_validation_error(
            f"Week must be between {LIMITS['round_min']} and {LIMITS['round_max']}",
            {"transactions": [], "week": week, "count": 0}
        )

    headers = get_http_headers("sleeper_transactions")
    url = f"https://api.sleeper.app/v1/league/{league_id}/transactions/{week}"

    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        transactions_data = response.json()

        # Enrich player IDs inside adds/drops maps
        try:
            from .database import NFLDatabase
            nfl_db = NFLDatabase()
            cache = {}

            def enrich_player(pid):
                if pid in cache:
                    return cache[pid]
                athlete = nfl_db.get_athlete_by_id(pid) or {}
                data = {"player_id": pid, "full_name": athlete.get("full_name"), "position": athlete.get("position")}
                cache[pid] = data
                return data

            if isinstance(transactions_data, list):
                for tx in transactions_data:
                    if not isinstance(tx, dict):
                        continue
                    adds = tx.get("adds") or {}
                    drops = tx.get("drops") or {}
                    if isinstance(adds, dict):
                        tx["adds_enriched"] = [enrich_player(pid) for pid in adds.keys()]
                    if isinstance(drops, dict):
                        tx["drops_enriched"] = [enrich_player(pid) for pid in drops.keys()]
        except Exception as enrich_error:
            logger.debug(f"Transaction enrichment skipped due to error: {enrich_error}")

        return create_success_response({
            "transactions": transactions_data,
            "week": week,
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
        
        try:
            nfl_db = _init_db()
            cache = {}
            if isinstance(traded_picks_data, list):
                for tp in traded_picks_data:
                    if isinstance(tp, dict) and tp.get("player_id"):
                        tp["player_enriched"] = _enrich_single(nfl_db, tp["player_id"], cache)
        except Exception as e:
            logger.debug(f"Traded pick enrichment skipped: {e}")
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
async def get_trending_players(nfl_db=None, trend_type: str = "add", lookback_hours: Optional[int] = 24, limit: Optional[int] = 25) -> dict:
    """
    Get trending players from Sleeper API.
    
    This tool fetches currently trending players based on adds/drops or other
    activity metrics from the Sleeper platform.
    
    Args:
        nfl_db: NFLDatabase instance to use for player lookups (if None, creates new instance)
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
        raw_items = response.json()  # May be list[dict] or list[str]

        if not raw_items:
            return create_success_response({
                "trending_players": [],
                "trend_type": trend_type,
                "lookback_hours": lookback_hours,
                "count": 0
            })

        if nfl_db is None:
            from .database import NFLDatabase
            nfl_db = NFLDatabase()

        try:
            sample_athletes = nfl_db.search_athletes_by_name("", limit=1)
            if not sample_athletes:
                from . import athlete_tools
                try:
                    logger.info("Database appears empty, attempting to fetch athletes for trending players lookup")
                    await athlete_tools.fetch_athletes(nfl_db)
                except Exception as fetch_error:
                    logger.warning(f"Failed to automatically fetch athletes: {fetch_error}")
        except Exception as db_error:
            logger.warning(f"Could not check database status: {db_error}")

        enriched_players = []
        for item in raw_items:
            if isinstance(item, dict):
                player_id = item.get("player_id") or item.get("id")
                count = item.get("count")  # Sleeper trending provides count
                if not player_id:
                    continue
            else:
                player_id = item
                count = None

            base_info = nfl_db.get_athlete_by_id(player_id) or {
                "player_id": player_id,
                "full_name": None,
                "first_name": None,
                "last_name": None,
                "position": None,
                "team": None,
                "age": None,
                "jersey": None
            }
            enriched_players.append({
                "player_id": player_id,
                "count": count,
                "enriched": base_info
            })

        return create_success_response({
            "trending_players": enriched_players,
            "trend_type": trend_type,
            "lookback_hours": lookback_hours,
            "count": len(enriched_players)
        })


@handle_http_errors(
    default_data={"picks": [], "count": 0},
    operation_name="fetching draft picks"
)
async def get_draft_picks(draft_id: str) -> dict:  # type: ignore[override]
    """Override earlier definition to add enrichment (keeps same name).

    Returns picks with additive `player_enriched` for each pick containing
    player_id if available.
    """
    headers = get_http_headers("sleeper_draft_picks")
    url = f"https://api.sleeper.app/v1/draft/{draft_id}/picks"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        picks = response.json()
        try:
            from .database import NFLDatabase
            nfl_db = NFLDatabase()
            for p in picks:
                if isinstance(p, dict) and p.get("player_id"):
                    athlete = nfl_db.get_athlete_by_id(p["player_id"]) or {}
                    p["player_enriched"] = {
                        "player_id": p["player_id"],
                        "full_name": athlete.get("full_name"),
                        "position": athlete.get("position")
                    }
        except Exception as enrich_error:
            logger.debug(f"Draft pick enrichment skipped: {enrich_error}")
        return create_success_response({
            "picks": picks,
            "count": len(picks)
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


# -------------------------------------------------------------
# NEW: Additional Sleeper endpoints (Users, Drafts, Players)
# -------------------------------------------------------------

@handle_http_errors(
    default_data={"user": None},
    operation_name="fetching user"
)
async def get_user(user_id_or_username: str) -> dict:
    """Fetch a Sleeper user by user_id or username."""
    headers = get_http_headers("sleeper_users")
    url = f"https://api.sleeper.app/v1/user/{user_id_or_username}"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        return create_success_response({"user": response.json()})


@handle_http_errors(
    default_data={"leagues": [], "count": 0, "season": None},
    operation_name="fetching user leagues"
)
async def get_user_leagues(user_id: str, season: int) -> dict:
    """Fetch all leagues for a user for a season."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/user/{user_id}/leagues/nfl/{season}"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        return create_success_response({"leagues": data, "count": len(data), "season": season})


@handle_http_errors(
    default_data={"drafts": [], "count": 0},
    operation_name="fetching league drafts"
)
async def get_league_drafts(league_id: str) -> dict:
    """Fetch all drafts for a league."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/league/{league_id}/drafts"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        return create_success_response({"drafts": data, "count": len(data)})


@handle_http_errors(
    default_data={"draft": None},
    operation_name="fetching draft"
)
async def get_draft(draft_id: str) -> dict:
    """Fetch a specific draft."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/draft/{draft_id}"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        return create_success_response({"draft": response.json()})


@handle_http_errors(
    default_data={"picks": [], "count": 0},
    operation_name="fetching draft picks"
)
async def get_draft_picks(draft_id: str) -> dict:
    """Fetch all picks in a draft."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/draft/{draft_id}/picks"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        return create_success_response({"picks": data, "count": len(data)})


@handle_http_errors(
    default_data={"traded_picks": [], "count": 0},
    operation_name="fetching draft traded picks"
)
async def get_draft_traded_picks(draft_id: str) -> dict:
    """Fetch traded picks for a draft."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/draft/{draft_id}/traded_picks"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        try:
            nfl_db = _init_db()
            cache = {}
            if isinstance(data, list):
                for tp in data:
                    if isinstance(tp, dict) and tp.get("player_id"):
                        tp["player_enriched"] = _enrich_single(nfl_db, tp["player_id"], cache)
        except Exception as e:
            logger.debug(f"Draft traded pick enrichment skipped: {e}")
        return create_success_response({"traded_picks": data, "count": len(data)})


# Player dump caching (large ~5MB) - cache in memory to reduce calls.
_PLAYERS_CACHE = {"data": None, "fetched_at": 0}
_PLAYERS_CACHE_TTL = 60 * 60 * 12  # 12 hours

@handle_http_errors(
    default_data={"players": {}, "cached": False},
    operation_name="fetching all players"
)
async def fetch_all_players(force_refresh: bool = False) -> dict:
    """Fetch the full players map from Sleeper (cached; heavy endpoint).

    Args:
        force_refresh: Ignore cache and refetch.
    """
    import time as _time
    now = _time.time()
    if (
        not force_refresh and _PLAYERS_CACHE["data"] is not None and
        now - _PLAYERS_CACHE["fetched_at"] < _PLAYERS_CACHE_TTL
    ):
        return create_success_response({
            "players": {},  # not returning the large blob again intentionally
            "cached": True,
            "ttl_remaining": int(_PLAYERS_CACHE_TTL - (now - _PLAYERS_CACHE["fetched_at"]))
        })

    headers = get_http_headers("sleeper_league")
    url = "https://api.sleeper.app/v1/players/nfl"
    async with create_http_client(timeout=LONG_TIMEOUT) as client:  # longer timeout
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        _PLAYERS_CACHE["data"] = data
        _PLAYERS_CACHE["fetched_at"] = now
        return create_success_response({
            "players": {},  # avoid huge payload downstream; signal success
            "cached": False,
            "player_count": len(data)
        })