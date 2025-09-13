"""
NFL-related MCP tools for the NFL MCP Server.

This module contains MCP tools for fetching NFL news, teams data, and depth charts.
"""

import httpx
from typing import Optional, Dict, Any, List
import asyncio
from bs4 import BeautifulSoup

from .config import get_http_headers, create_http_client, validate_limit, LIMITS
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors, handle_validation_error
)


@handle_http_errors(
    default_data={"articles": [], "total_articles": 0},
    operation_name="fetching NFL news"
)
async def get_nfl_news(limit: Optional[int] = 50) -> dict:
    """
    Get the latest NFL news from ESPN API.
    
    This tool fetches current NFL news articles from ESPN's API and returns
    them in a structured format suitable for LLM processing.
    
    Args:
        limit: Maximum number of news articles to retrieve (default: 50, max: 50)
        
    Returns:
        A dictionary containing:
        - articles: List of news articles with title, description, published date, etc.
        - total_articles: Number of articles returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate and cap the limit
    limit = validate_limit(
        limit, 
        LIMITS["nfl_news_min"], 
        LIMITS["nfl_news_max"], 
        LIMITS["nfl_news_max"]
    )
        
    headers = get_http_headers("nfl_news")
    
    # Build the ESPN API URL
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?limit={limit}"
    
    async with create_http_client() as client:
        # Fetch the news from ESPN API
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        
        # Extract articles from the response
        articles = data.get('articles', [])
        
        # Process articles to extract key information
        processed_articles = []
        for article in articles:
            processed_article = {
                "headline": article.get('headline', ''),
                "description": article.get('description', ''),
                "published": article.get('published', ''),
                "type": article.get('type', ''),
                "story": article.get('story', ''),
                "categories": [cat.get('description', '') for cat in article.get('categories', [])],
                "links": article.get('links', {})
            }
            processed_articles.append(processed_article)
        
        return create_success_response({
            "articles": processed_articles,
            "total_articles": len(processed_articles)
        })


@handle_http_errors(
    default_data={"teams": [], "total_teams": 0},
    operation_name="fetching NFL teams"
)
async def get_teams() -> dict:
    """
    Get all NFL teams from ESPN API.
    
    This tool fetches the current NFL teams from ESPN's API and returns
    them in a structured format with team names and IDs.
    
    Returns:
        A dictionary containing:
        - teams: List of teams with name and id
        - total_teams: Number of teams returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("nfl_teams")
    
    # Build the ESPN API URL for teams (fixed to use correct endpoint)
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


@handle_http_errors(
    default_data={"teams_count": 0, "last_updated": None},
    operation_name="fetching teams from ESPN API"
)
async def fetch_teams(nfl_db) -> dict:
    """
    Fetch all NFL teams from ESPN API and store them in the local database.
    
    This tool fetches the complete teams data from ESPN's API and 
    upserts the data into the SQLite database for fast local lookups.
    
    Args:
        nfl_db: The NFLDatabase instance to store data in
    
    Returns:
        A dictionary containing:
        - teams_count: Number of teams processed
        - last_updated: Timestamp of the update
        - success: Whether the fetch was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("nfl_teams")
    
    # ESPN API endpoint for teams
    url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
    
    async with create_http_client() as client:
        # Fetch the teams from ESPN API
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        
        # Extract teams from the response
        teams_data = data.get('sports', [{}])[0].get('leagues', [{}])[0].get('teams', [])
        
        # Process teams to get the team info
        processed_teams = []
        for team in teams_data:
            team_info = team.get('team', {})
            processed_teams.append(team_info)
        
        # Store in database
        count = nfl_db.upsert_teams(processed_teams)
        last_updated = nfl_db.get_teams_last_updated()
        
        return create_success_response({
            "teams_count": count,
            "last_updated": last_updated
        })


@handle_http_errors(
    default_data={"team_id": None, "team_name": None, "depth_chart": []},
    operation_name="fetching depth chart"
)
async def get_depth_chart(team_id: str) -> dict:
    """
    Get the depth chart for a specific NFL team.
    
    This tool fetches the depth chart from ESPN for the specified team,
    showing player positions and depth ordering.
    
    Args:
        team_id: The team abbreviation (e.g., 'KC', 'TB', 'NE')
        
    Returns:
        A dictionary containing:
        - team_id: The team identifier used
        - team_name: The team's full name
        - depth_chart: List of positions with players in depth order
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate team_id
    if not team_id or not isinstance(team_id, str):
        return handle_validation_error(
            "Team ID is required and must be a string",
            {"team_id": team_id, "team_name": None, "depth_chart": []}
        )
    
    headers = get_http_headers("depth_chart")
    
    # Build the ESPN depth chart URL
    url = f"https://www.espn.com/nfl/team/depth/_/name/{team_id.upper()}"
    
    async with create_http_client() as client:
        # Fetch the depth chart page
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        
        # Parse HTML content
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract team name
        team_name = None
        team_header = soup.find('h1')
        if team_header:
            team_name = team_header.get_text(strip=True)
        
        # Extract depth chart information
        depth_chart = []
        
        # Look for depth chart tables or sections
        # ESPN depth chart structure may vary, so we'll look for common patterns
        depth_sections = soup.find_all(['table', 'div'], class_=lambda x: x and 'depth' in x.lower() if x else False)
        
        if not depth_sections:
            # Try alternative selectors
            depth_sections = soup.find_all('table')
        
        for section in depth_sections:
            # Extract position and players
            rows = section.find_all('tr')
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 2:
                    position = cells[0].get_text(strip=True)
                    players = []
                    for cell in cells[1:]:
                        player_text = cell.get_text(strip=True)
                        if player_text and player_text != position:
                            players.append(player_text)
                    
                    if position and players:
                        depth_chart.append({
                            "position": position,
                            "players": players
                        })
        
        return create_success_response({
            "team_id": team_id.upper(),
            "team_name": team_name,
            "depth_chart": depth_chart
        })


@handle_http_errors(
    default_data={"team_id": None, "team_name": None, "injuries": []},
    operation_name="fetching team injuries"
)
async def get_team_injuries(team_id: str, limit: Optional[int] = 50) -> dict:
    """
    Get the current injury report for a specific NFL team.
    
    This tool fetches injury information from ESPN's Core API for the specified team,
    providing critical information for fantasy lineup decisions.
    
    Args:
        team_id: The team abbreviation (e.g., 'KC', 'TB', 'NE') or ESPN team ID
        limit: Maximum number of injuries to return (1-100, defaults to 50)
        
    Returns:
        A dictionary containing:
        - team_id: The team identifier used
        - team_name: The team's full name  
        - injuries: List of injured players with status and details
        - count: Number of injuries returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate team_id
    if not team_id or not isinstance(team_id, str):
        return handle_validation_error(
            "Team ID is required and must be a string",
            {"team_id": team_id, "team_name": None, "injuries": []}
        )
    
    # Validate limit
    limit = validate_limit(limit or 50, 1, 100, 50)
    
    headers = get_http_headers("nfl_teams")  # Reuse existing config
    
    # ESPN Core API endpoint for team injuries
    # Use team abbreviation to construct URL - ESPN Core API uses team IDs but we'll try both formats
    url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{team_id.upper()}/injuries?limit={limit}"
    
    async with create_http_client() as client:
        try:
            # First attempt with team abbreviation as-is
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # If team abbreviation fails, we might need to map to ESPN team ID
                # For now, return empty results with a helpful message
                return create_success_response({
                    "team_id": team_id.upper(),
                    "team_name": None,
                    "injuries": [],
                    "count": 0,
                    "message": f"No injury data found for team '{team_id}'. Team may not exist or have no current injuries."
                })
            else:
                raise  # Re-raise other HTTP errors
        
        # Parse JSON response
        data = response.json()
        
        # Extract injuries from the response
        injuries_data = data.get('items', [])
        
        # Process injuries to extract relevant information
        processed_injuries = []
        team_name = None
        
        for injury_item in injuries_data:
            injury = {}
            
            # Get athlete information
            athlete_ref = injury_item.get('athlete', {})
            if athlete_ref and isinstance(athlete_ref, dict):
                injury['player_name'] = athlete_ref.get('displayName', 'Unknown')
                injury['player_id'] = athlete_ref.get('id')
                injury['position'] = athlete_ref.get('position', {}).get('abbreviation', 'N/A')
            
            # Get team information (should be consistent across all items)
            if not team_name:
                team_ref = injury_item.get('team', {})
                if team_ref and isinstance(team_ref, dict):
                    team_name = team_ref.get('displayName', 'Unknown Team')
            
            # Get injury details
            injury['status'] = injury_item.get('status', {}).get('name', 'Unknown')
            injury['description'] = injury_item.get('description', 'No description available')
            injury['date'] = injury_item.get('date', 'Unknown')
            injury['type'] = injury_item.get('type', {}).get('name', 'Unknown')
            
            # Fantasy relevance indicators
            injury['severity'] = 'Unknown'
            status_lower = injury['status'].lower()
            if 'out' in status_lower or 'ir' in status_lower:
                injury['severity'] = 'High'
            elif 'doubtful' in status_lower or 'questionable' in status_lower:
                injury['severity'] = 'Medium'
            elif 'probable' in status_lower or 'limited' in status_lower:
                injury['severity'] = 'Low'
                
            processed_injuries.append(injury)
        
        return create_success_response({
            "team_id": team_id.upper(),
            "team_name": team_name,
            "injuries": processed_injuries,
            "count": len(processed_injuries)
        })


@handle_http_errors(
    default_data={"team_id": None, "team_name": None, "player_stats": []},
    operation_name="fetching team player statistics"
)
async def get_team_player_stats(team_id: str, season: Optional[int] = 2025, season_type: Optional[int] = 2, limit: Optional[int] = 50) -> dict:
    """
    Get current season player statistics for a specific NFL team.
    
    This tool fetches individual player performance data from ESPN's Core API,
    providing key metrics for fantasy football analysis and decision making.
    
    Args:
        team_id: The team abbreviation (e.g., 'KC', 'TB', 'NE') or ESPN team ID
        season: Season year (defaults to 2025)
        season_type: 1=Pre, 2=Regular, 3=Post, 4=Off (defaults to 2)
        limit: Maximum number of player stats to return (1-100, defaults to 50)
        
    Returns:
        A dictionary containing:
        - team_id: The team identifier used
        - team_name: The team's full name
        - season: Season year requested
        - season_type: Season type requested
        - player_stats: List of players with their statistical performance
        - count: Number of player stats returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate inputs
    if not team_id or not isinstance(team_id, str):
        return handle_validation_error(
            "Team ID is required and must be a string",
            {"team_id": team_id, "team_name": None, "player_stats": []}
        )
    
    # Validate and set defaults
    season = season or 2025
    season_type = season_type or 2
    limit = validate_limit(limit or 50, 1, 100, 50)
    
    headers = get_http_headers("nfl_teams")  # Reuse existing config
    
    # ESPN Core API endpoint for team player statistics
    url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/types/{season_type}/teams/{team_id.upper()}/athletes?limit={limit}"
    
    async with create_http_client() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return create_success_response({
                    "team_id": team_id.upper(),
                    "team_name": None,
                    "season": season,
                    "season_type": season_type,
                    "player_stats": [],
                    "count": 0,
                    "message": f"No player statistics found for team '{team_id}' in season {season}."
                })
            else:
                raise
        
        # Parse JSON response
        data = response.json()
        
        # Extract athletes from the response
        athletes_data = data.get('items', [])
        
        # Process athlete statistics
        processed_stats = []
        team_name = None
        
        for athlete_item in athletes_data:
            # Extract basic athlete info
            player_stat = {
                'player_id': athlete_item.get('id'),
                'player_name': athlete_item.get('displayName', 'Unknown'),
                'jersey': athlete_item.get('jersey'),
                'position': None,
                'age': athlete_item.get('age'),
                'experience': athlete_item.get('experience', {}).get('years')
            }
            
            # Get position info
            position_ref = athlete_item.get('position', {})
            if position_ref and isinstance(position_ref, dict):
                player_stat['position'] = position_ref.get('abbreviation', 'N/A')
            
            # Get team info (should be consistent)
            if not team_name:
                team_ref = athlete_item.get('team', {})
                if team_ref and isinstance(team_ref, dict):
                    team_name = team_ref.get('displayName', 'Unknown Team')
            
            # Try to get statistics if available (this may require additional API calls)
            # For now, we'll include basic info and note that detailed stats may need separate calls
            player_stat['stats_note'] = 'Detailed statistics require additional API calls per player'
            
            # Check if player is active
            player_stat['active'] = athlete_item.get('active', True)
            
            # Fantasy relevance indicators
            position = player_stat.get('position', '').upper()
            if position in ['QB', 'RB', 'WR', 'TE', 'K', 'DST']:
                player_stat['fantasy_relevant'] = True
            else:
                player_stat['fantasy_relevant'] = False
            
            processed_stats.append(player_stat)
        
        return create_success_response({
            "team_id": team_id.upper(),
            "team_name": team_name,
            "season": season,
            "season_type": season_type,
            "player_stats": processed_stats,
            "count": len(processed_stats)
        })


@handle_http_errors(
    default_data={"standings": [], "season": None, "season_type": None},
    operation_name="fetching NFL standings"
)
async def get_nfl_standings(season: Optional[int] = 2025, season_type: Optional[int] = 2, group: Optional[int] = None) -> dict:
    """
    Get current NFL standings from ESPN's Core API.
    
    This tool fetches league standings which provide critical context for fantasy
    decisions, such as which teams might rest players or be more motivated.
    
    Args:
        season: Season year (defaults to 2025)
        season_type: 1=Pre, 2=Regular, 3=Post, 4=Off (defaults to 2)
        group: Conference group (1=AFC, 2=NFC, None=both, defaults to None for all)
        
    Returns:
        A dictionary containing:
        - standings: List of team standings with records and playoff implications
        - season: Season year requested
        - season_type: Season type requested
        - group: Conference group requested
        - count: Number of teams in standings
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate and set defaults
    season = season or 2025
    season_type = season_type or 2
    
    headers = get_http_headers("nfl_teams")  # Reuse existing config
    
    # ESPN Core API endpoint for NFL standings
    if group is not None and group in [1, 2]:
        # Get specific conference standings
        url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/types/{season_type}/groups/{group}/standings"
    else:
        # Get all standings
        url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/types/{season_type}/standings"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        
        # Extract standings from the response
        standings_items = data.get('children', []) or data.get('items', [])
        
        processed_standings = []
        
        for standing_item in standings_items:
            # Try to get team info from the standing entry
            team_info = {}
            
            # Extract team reference
            team_ref = standing_item.get('team', {})
            if team_ref and isinstance(team_ref, dict):
                team_info['team_id'] = team_ref.get('id')
                team_info['team_name'] = team_ref.get('displayName', 'Unknown')
                team_info['abbreviation'] = team_ref.get('abbreviation', 'UNK')
            
            # Extract standings statistics
            stats = standing_item.get('stats', [])
            for stat in stats:
                stat_name = stat.get('name', '').lower()
                stat_value = stat.get('value')
                
                if 'wins' in stat_name or stat_name == 'wins':
                    team_info['wins'] = stat_value
                elif 'losses' in stat_name or stat_name == 'losses':
                    team_info['losses'] = stat_value
                elif 'ties' in stat_name or stat_name == 'ties':
                    team_info['ties'] = stat_value
                elif 'winpercent' in stat_name or 'win_percent' in stat_name:
                    team_info['win_percentage'] = stat_value
                elif 'playoffrank' in stat_name or 'playoff' in stat_name:
                    team_info['playoff_rank'] = stat_value
                elif 'divisionrank' in stat_name or 'division' in stat_name:
                    team_info['division_rank'] = stat_value
            
            # Calculate fantasy implications
            wins = team_info.get('wins', 0)
            losses = team_info.get('losses', 0)
            total_games = wins + losses
            
            # Determine team motivation level for fantasy purposes
            if wins >= 12 or (total_games >= 14 and wins / total_games > 0.8):
                team_info['fantasy_context'] = 'May rest starters in late season'
                team_info['motivation_level'] = 'Low (Playoff lock)'
            elif wins <= 4 or (total_games >= 10 and wins / total_games < 0.3):
                team_info['fantasy_context'] = 'May evaluate young players'
                team_info['motivation_level'] = 'Medium (Development mode)'
            else:
                team_info['fantasy_context'] = 'Fighting for playoffs - full effort expected'
                team_info['motivation_level'] = 'High (Playoff hunt)'
            
            processed_standings.append(team_info)
        
        return create_success_response({
            "standings": processed_standings,
            "season": season,
            "season_type": season_type,
            "group": group,
            "count": len(processed_standings)
        })


@handle_http_errors(
    default_data={"team_id": None, "team_name": None, "schedule": []},
    operation_name="fetching team schedule"
)
async def get_team_schedule(team_id: str, season: Optional[int] = 2025) -> dict:
    """
    Get the schedule for a specific NFL team from ESPN's Site API.
    
    This tool fetches the team's schedule which provides critical context for fantasy
    decisions, including upcoming matchups, strength of schedule, and bye weeks.
    
    Args:
        team_id: The team abbreviation (e.g., 'KC', 'TB', 'NE') or ESPN team ID
        season: Season year (defaults to 2025)
        
    Returns:
        A dictionary containing:
        - team_id: The team identifier used
        - team_name: The team's full name
        - season: Season year requested
        - schedule: List of games with matchup details and fantasy implications
        - count: Number of games in schedule
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate team_id
    if not team_id or not isinstance(team_id, str):
        return handle_validation_error(
            "Team ID is required and must be a string",
            {"team_id": team_id, "team_name": None, "schedule": []}
        )
    
    # Validate season
    season = season or 2025
    
    headers = get_http_headers("nfl_teams")  # Reuse existing config
    
    # ESPN Site API endpoint for team schedule
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id.upper()}/schedule?season={season}"
    
    async with create_http_client() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return create_success_response({
                    "team_id": team_id.upper(),
                    "team_name": None,
                    "season": season,
                    "schedule": [],
                    "count": 0,
                    "message": f"No schedule found for team '{team_id}' in season {season}."
                })
            else:
                raise
        
        # Parse JSON response
        data = response.json()
        
        # Extract team info
        team_info = data.get('team', {})
        team_name = team_info.get('displayName', 'Unknown Team')
        
        # Extract events (games) from the response
        events = data.get('events', [])
        
        processed_schedule = []
        
        for event in events:
            game = {
                'game_id': event.get('id'),
                'date': event.get('date'),
                'week': None,
                'season_type': None,
                'opponent': None,
                'is_home': None,
                'result': None,
                'fantasy_implications': []
            }
            
            # Extract week information
            week_info = event.get('week', {})
            if week_info:
                game['week'] = week_info.get('number')
            
            # Extract season type
            season_info = event.get('season', {})
            if season_info:
                season_type_info = season_info.get('type', {})
                if season_type_info:
                    game['season_type'] = season_type_info.get('name', 'Regular Season')
            
            # Extract competition details
            competitions = event.get('competitions', [])
            if competitions:
                competition = competitions[0]  # Usually just one competition per event
                
                # Find opponent and home/away status
                competitors = competition.get('competitors', [])
                for competitor in competitors:
                    team_ref = competitor.get('team', {})
                    if team_ref and team_ref.get('abbreviation', '').upper() != team_id.upper():
                        # This is the opponent
                        game['opponent'] = {
                            'abbreviation': team_ref.get('abbreviation', 'UNK'),
                            'name': team_ref.get('displayName', 'Unknown'),
                            'logo': team_ref.get('logo')
                        }
                    elif team_ref and team_ref.get('abbreviation', '').upper() == team_id.upper():
                        # This is our team - check if home or away
                        game['is_home'] = competitor.get('homeAway') == 'home'
                
                # Get game result if available
                status = competition.get('status', {})
                if status:
                    status_type = status.get('type', {}).get('name', '')
                    if status_type == 'STATUS_FINAL':
                        # Game completed - determine win/loss
                        game['result'] = 'completed'
                        for competitor in competitors:
                            team_ref = competitor.get('team', {})
                            if team_ref and team_ref.get('abbreviation', '').upper() == team_id.upper():
                                winner = competitor.get('winner', False)
                                game['result'] = 'win' if winner else 'loss'
                    elif status_type in ['STATUS_SCHEDULED', 'STATUS_POSTPONED']:
                        game['result'] = 'scheduled'
                    else:
                        game['result'] = 'in_progress'
            
            # Add fantasy implications
            if game['opponent']:
                opp_name = game['opponent']['name']
                
                # Add basic matchup context
                if game['is_home']:
                    game['fantasy_implications'].append(f"Home game vs {opp_name} - typically favorable for offense")
                else:
                    game['fantasy_implications'].append(f"Away game at {opp_name} - consider road performance")
                
                # Add week-specific context
                if game['week']:
                    if game['week'] <= 3:
                        game['fantasy_implications'].append("Early season - sample size considerations")
                    elif game['week'] >= 15:
                        game['fantasy_implications'].append("Late season - potential rest concerns for playoff teams")
                
                # Add bye week identification
                if game['season_type'] == 'Bye Week':
                    game['fantasy_implications'].append("BYE WEEK - No fantasy points available")
            
            processed_schedule.append(game)
        
        return create_success_response({
            "team_id": team_id.upper(),
            "team_name": team_name,
            "season": season,
            "schedule": processed_schedule,
            "count": len(processed_schedule)
        })


@handle_http_errors(
    default_data={"players": [], "season": None, "category": None},
    operation_name="fetching league leaders"
)
async def get_league_leaders(category: str, season: int = 2025, season_type: int = 2, week: Optional[int] = None) -> dict:
    """Fetch current NFL statistical leaders for a single category.

    Instead of returning all categories, this focuses on one requested category.

        Supported input categories (case-insensitive) and their underlying ESPN
        stat category identifiers:
            - pass      → passingYards
            - rush      → rushingYards
            - receiving → receivingYards
            - tackles   → totalTackles
            - sacks     → sacks

    Args:
        category: One of pass, rush, receiving, tackles, sacks
        season: Season year (default 2025)
        season_type: 1=Pre, 2=Regular, 3=Post

    Returns:
        success response containing:
          - season / season_type
          - category (echoed normalized short token)
          - stat_category_name (resolved underlying ESPN category identifier)
          - players: list[{rank, value, athlete_id, athlete_name, team_id, team_abbr}]
    """
    # Normalize & validate inputs (support multiple categories separated by comma or whitespace)
    allowed = {"pass", "rush", "receiving", "tackles", "sacks"}
    if not category:
        return create_error_response(
            error_message="Category parameter required (one or multiple of: pass, rush, receiving, tackles, sacks)",
            error_type=ErrorType.VALIDATION,
            data={"players": [], "season": season, "category": None}
        )
    # Split on commas or whitespace
    requested_tokens = []
    for tok in [t.strip().lower() for part in category.split(',') for t in part.split()]:
        if tok and tok not in requested_tokens:
            requested_tokens.append(tok)
    invalid = [t for t in requested_tokens if t not in allowed]
    if invalid:
        return create_error_response(
            error_message=f"Invalid category token(s): {', '.join(invalid)}. Allowed: pass, rush, receiving, tackles, sacks",
            error_type=ErrorType.VALIDATION,
            data={"players": [], "season": season, "category": invalid}
        )
    multi = len(requested_tokens) > 1

    if season < 2000 or season > 2100:
        season = 2025
    if season_type not in (1, 2, 3):
        season_type = 2

    # Mapping from short token to prioritized list of canonical stat category name fragments
    target_fragments = {
        "pass": ["passingyards", "passing_yards", "passingyds"],
        "rush": ["rushingyards", "rushing_yards", "rushingyds"],
        "receiving": ["receivingyards", "receiving_yards", "receivingyds"],
        "tackles": ["totaltackles", "tackles"],
        "sacks": ["sacks"],
    }

    headers = get_http_headers("nfl_news")  # reuse a generic UA header config
    base = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/types/{season_type}"
    url = f"{base}/weeks/{week}/leaders" if (week is not None and isinstance(week, int) and 1 <= week <= 25) else f"{base}/leaders"

    async with create_http_client() as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        categories = data.get('categories') or data.get('items') or []

        def normalize_name(name: str) -> str:
            return ''.join(ch for ch in name.lower() if ch.isalnum())

        # Select best matching category
        chosen = None
        chosen_display = None
        # Build map token -> fragments list
        token_frag_map = {tok: target_fragments[tok] for tok in requested_tokens}
        # Iterate categories once, fill matches
        matches = {}
        for cat in categories:
            cat_name = cat.get('name') or cat.get('displayName') or cat.get('shortName') or ''
            norm = normalize_name(cat_name)
            for tok, fragments in token_frag_map.items():
                if tok in matches:
                    continue  # already matched
                if any(frag in norm for frag in fragments):
                    matches[tok] = (cat, cat.get('displayName') or cat_name)
        # Determine missing
        missing = [tok for tok in requested_tokens if tok not in matches]
        if len(requested_tokens) == 1 and missing:
            # Single-category failure retains previous error shape
            single = requested_tokens[0]
            return create_error_response(
                error_message=f"No matching stat category found for '{single}'",
                error_type=ErrorType.NOT_FOUND,
                data={"players": [], "season": season, "category": single}
            )

        # Build players per matched token
        cache_stats = {"hits": 0, "misses": 0}

        async def _fetch_json(url: str, client, headers, cache: Dict[str, Any]) -> Any:
            if not url:
                return None
            if url in cache:
                cache_stats["hits"] += 1
                return cache[url]
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                cache[url] = data
                cache_stats["misses"] += 1
                return data
            except Exception:
                return None

        async def extract_players(cat_obj, client, headers) -> List[Dict[str, Any]]:
            players_local: List[Dict[str, Any]] = []
            cache: Dict[str, Any] = {}

            # Collect leader groups (dereference group if needed)
            leader_groups = cat_obj.get('leaders', []) or []

            async def expand_group(group):
                # If no inline leaders but has ref/href, dereference
                if not group.get('leaders'):
                    for ref_key in ('$ref', 'href'):
                        if ref_key in group and isinstance(group[ref_key], str):
                            fetched = await _fetch_json(group[ref_key], client, headers, cache)
                            if fetched:
                                return fetched.get('leaders') or fetched.get('items') or []
                return group.get('leaders') or []

            expanded_lists = [expand_group(grp) for grp in leader_groups]
            expanded = await asyncio.gather(*expanded_lists) if expanded_lists else []

            # Flatten entries
            entries: List[Dict[str, Any]] = []
            for lst in expanded:
                entries.extend(lst)

            async def enrich_entry(entry):
                athlete = entry.get('athlete', {}) or {}
                team = entry.get('team', {}) or {}
                # Deref athlete/team if only reference
                for key_obj, label in ((athlete, 'athlete'), (team, 'team')):
                    if isinstance(key_obj, dict) and ('$ref' in key_obj or 'href' in key_obj):
                        ref_url = key_obj.get('$ref') or key_obj.get('href')
                        data = await _fetch_json(ref_url, client, headers, cache)
                        if data:
                            if label == 'athlete':
                                athlete.update(data)
                            else:
                                team.update(data)
                players_local.append({
                    "rank": entry.get('rank'),
                    "value": entry.get('value'),
                    "athlete_id": athlete.get('id'),
                    "athlete_name": athlete.get('displayName') or athlete.get('shortName'),
                    "team_id": team.get('id'),
                    "team_abbr": team.get('abbreviation'),
                })

            tasks = [enrich_entry(e) for e in entries]
            if tasks:
                semaphore = asyncio.Semaphore(10)
                async def sem_task(coro):
                    async with semaphore:
                        return await coro
                await asyncio.gather(*(sem_task(t) for t in tasks))
            return players_local

        if not multi:
            tok = requested_tokens[0]
            cat_obj, disp = matches.get(tok, (None, None))  # type: ignore
            players = await extract_players(cat_obj, client, headers) if cat_obj else []
            return create_success_response({
                "season": season,
                "season_type": season_type,
                "category": tok,
                "stat_category_name": disp,
                "players": players,
                "players_count": len(players),
                "cache": cache_stats
            })
        else:
            categories_payload = []
            for tok in requested_tokens:
                if tok in matches:
                    cat_obj, disp = matches[tok]
                    players = await extract_players(cat_obj, client, headers)
                    categories_payload.append({
                        "category": tok,
                        "stat_category_name": disp,
                        "players": players,
                        "players_count": len(players)
                    })
            return create_success_response({
                "season": season,
                "season_type": season_type,
                "categories_requested": requested_tokens,
                "categories_found": len(categories_payload),
                "categories_missing": missing,
                "categories": categories_payload,
                "cache": cache_stats
            })
