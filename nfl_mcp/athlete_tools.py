"""
Athlete-related MCP tools for the NFL MCP Server.

This module contains MCP tools for fetching, searching, and managing NFL athlete data.
"""

import httpx
from typing import Optional

from .config import get_http_headers, create_http_client, validate_limit, LIMITS, LONG_TIMEOUT


async def fetch_athletes(nfl_db) -> dict:
    """
    Fetch all NFL players from Sleeper API and store them in the local database.
    
    This tool fetches the complete athlete roster from Sleeper's API and 
    upserts the data into the SQLite database for fast local lookups.
    
    Args:
        nfl_db: The NFLDatabase instance to store data in
    
    Returns:
        A dictionary containing:
        - athletes_count: Number of athletes processed
        - last_updated: Timestamp of the update
        - success: Whether the fetch was successful
        - error: Error message (if any)
    """
    try:
        headers = get_http_headers("athletes")
        
        # Sleeper API endpoint for all players
        url = "https://api.sleeper.app/v1/players/nfl"
        
        async with create_http_client(LONG_TIMEOUT) as client:
            # Fetch the athletes from Sleeper API
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            
            # Parse JSON response
            athletes_data = response.json()
            
            # Store in database
            count = nfl_db.upsert_athletes(athletes_data)
            last_updated = nfl_db.get_last_updated()
            
            return {
                "athletes_count": count,
                "last_updated": last_updated,
                "success": True,
                "error": None
            }
            
    except httpx.TimeoutException:
        return {
            "athletes_count": 0,
            "last_updated": None,
            "success": False,
            "error": "Request timed out while fetching athletes from Sleeper API"
        }
    except httpx.HTTPStatusError as e:
        return {
            "athletes_count": 0,
            "last_updated": None,
            "success": False,
            "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
        }
    except Exception as e:
        return {
            "athletes_count": 0,
            "last_updated": None,
            "success": False,
            "error": f"Unexpected error fetching athletes: {str(e)}"
        }


def lookup_athlete(nfl_db, athlete_id: str) -> dict:
    """
    Look up an athlete by their ID.
    
    This tool queries the local database for an athlete with the given ID
    and returns their information including name, team, position, etc.
    
    Args:
        nfl_db: The NFLDatabase instance to query
        athlete_id: The unique identifier for the athlete
        
    Returns:
        A dictionary containing:
        - athlete: Athlete information (if found)
        - found: Whether the athlete was found
        - error: Error message (if any)
    """
    try:
        athlete = nfl_db.get_athlete_by_id(athlete_id)
        
        if athlete:
            return {
                "athlete": athlete,
                "found": True,
                "error": None
            }
        else:
            return {
                "athlete": None,
                "found": False,
                "error": f"Athlete with ID '{athlete_id}' not found"
            }
            
    except Exception as e:
        return {
            "athlete": None,
            "found": False,
            "error": f"Error looking up athlete: {str(e)}"
        }


def search_athletes(nfl_db, name: str, limit: Optional[int] = 10) -> dict:
    """
    Search for athletes by name (partial match supported).
    
    This tool searches the local database for athletes whose names match
    the given search term, supporting partial matches.
    
    Args:
        nfl_db: The NFLDatabase instance to query
        name: Name or partial name to search for
        limit: Maximum number of results to return (default: 10)
        
    Returns:
        A dictionary containing:
        - athletes: List of matching athletes
        - count: Number of athletes found
        - search_term: The search term used
        - error: Error message (if any)
    """
    try:
        # Validate limit using shared validation
        limit = validate_limit(
            limit,
            LIMITS["athletes_search_min"],
            LIMITS["athletes_search_max"],
            LIMITS["athletes_search_default"]
        )
        
        athletes = nfl_db.search_athletes_by_name(name, limit)
        
        return {
            "athletes": athletes,
            "count": len(athletes),
            "search_term": name,
            "error": None
        }
        
    except Exception as e:
        return {
            "athletes": [],
            "count": 0,
            "search_term": name,
            "error": f"Error searching athletes: {str(e)}"
        }


def get_athletes_by_team(nfl_db, team_id: str) -> dict:
    """
    Get all athletes for a specific team.
    
    This tool retrieves all athletes associated with a given team ID
    from the local database.
    
    Args:
        nfl_db: The NFLDatabase instance to query
        team_id: The team identifier (e.g., "SF", "DAL", "NE")
        
    Returns:
        A dictionary containing:
        - athletes: List of athletes on the team
        - count: Number of athletes found
        - team_id: The team ID searched for
        - error: Error message (if any)
    """
    try:
        athletes = nfl_db.get_athletes_by_team(team_id)
        
        return {
            "athletes": athletes,
            "count": len(athletes),
            "team_id": team_id,
            "error": None
        }
        
    except Exception as e:
        return {
            "athletes": [],
            "count": 0,
            "team_id": team_id,
            "error": f"Error getting athletes for team: {str(e)}"
        }