"""
Demonstration of error recovery strategies with retry logic.

This module shows how to use the retry functionality for resilient API calls.
"""

import httpx
from typing import Optional

from .config import get_http_headers, create_http_client
from .errors import (
    create_success_response, RetryConfig, with_retry
)


@with_retry(
    retry_config=RetryConfig(
        max_attempts=3,
        base_delay=1.0,
        max_delay=10.0,
        backoff_factor=2.0,
        retry_on_timeout=True,
        retry_on_server_error=True
    ),
    default_data={"nfl_state": None},
    operation_name="fetching NFL state with retry"
)
async def get_nfl_state_with_retry() -> dict:
    """
    Get current NFL state information from Sleeper API with retry logic.
    
    This demonstrates retry functionality for resilient API calls.
    Retries up to 3 times with exponential backoff for timeout and 5xx errors.
    
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


@with_retry(
    retry_config=RetryConfig(
        max_attempts=2,  # Fewer retries for faster operations
        base_delay=0.5,
        max_delay=5.0,
        retry_on_timeout=True,
        retry_on_server_error=False  # Don't retry on server errors for this endpoint
    ),
    default_data={"teams": [], "total_teams": 0},
    operation_name="fetching NFL teams with custom retry"
)
async def get_teams_with_custom_retry() -> dict:
    """
    Get all NFL teams from ESPN API with custom retry configuration.
    
    This demonstrates custom retry configuration for different use cases.
    Uses shorter delays and fewer retries for faster endpoints.
    
    Returns:
        A dictionary containing:
        - teams: List of teams with name and id
        - total_teams: Number of teams returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("nfl_teams")
    
    # Build the ESPN API URL for teams
    url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
    
    async with create_http_client() as client:
        # Fetch the teams from ESPN API
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        
        # Extract teams from the response
        teams_data = data.get('sports', [{}])[0].get('leagues', [{}])[0].get('teams', [])
        
        # Process teams to extract key information
        processed_teams = []
        for team in teams_data:
            team_info = team.get('team', {})
            processed_team = {
                "id": team_info.get('id', ''),
                "abbreviation": team_info.get('abbreviation', ''),
                "name": team_info.get('name', ''),
                "displayName": team_info.get('displayName', ''),
                "shortDisplayName": team_info.get('shortDisplayName', ''),
                "location": team_info.get('location', ''),
                "color": team_info.get('color', ''),
                "alternateColor": team_info.get('alternateColor', ''),
                "logo": team_info.get('logo', '')
            }
            processed_teams.append(processed_team)
        
        return create_success_response({
            "teams": processed_teams,
            "total_teams": len(processed_teams)
        })