"""
NFL-related MCP tools for the NFL MCP Server.

This module contains MCP tools for fetching NFL news, teams data, and depth charts.
"""

import httpx
from typing import Optional
from bs4 import BeautifulSoup

from .config import get_http_headers, create_http_client, validate_limit, LIMITS


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
    """
    # Validate and cap the limit
    limit = validate_limit(
        limit, 
        LIMITS["nfl_news_min"], 
        LIMITS["nfl_news_max"], 
        LIMITS["nfl_news_max"]
    )
        
    try:
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
            
            return {
                "articles": processed_articles,
                "total_articles": len(processed_articles),
                "success": True,
                "error": None
            }
            
    except httpx.TimeoutException:
        return {
            "articles": [],
            "total_articles": 0,
            "success": False,
            "error": "Request timed out while fetching NFL news"
        }
    except httpx.HTTPStatusError as e:
        return {
            "articles": [],
            "total_articles": 0,
            "success": False,
            "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
        }
    except Exception as e:
        return {
            "articles": [],
            "total_articles": 0,
            "success": False,
            "error": f"Unexpected error fetching NFL news: {str(e)}"
        }


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
    """
    try:
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
            
            return {
                "teams": processed_teams,
                "total_teams": len(processed_teams),
                "success": True,
                "error": None
            }
            
    except httpx.TimeoutException:
        return {
            "teams": [],
            "total_teams": 0,
            "success": False,
            "error": "Request timed out while fetching NFL teams"
        }
    except httpx.HTTPStatusError as e:
        return {
            "teams": [],
            "total_teams": 0,
            "success": False,
            "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
        }
    except Exception as e:
        return {
            "teams": [],
            "total_teams": 0,
            "success": False,
            "error": f"Unexpected error fetching NFL teams: {str(e)}"
        }


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
    """
    try:
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
            
            return {
                "teams_count": count,
                "last_updated": last_updated,
                "success": True,
                "error": None
            }
            
    except httpx.TimeoutException:
        return {
            "teams_count": 0,
            "last_updated": None,
            "success": False,
            "error": "Request timed out while fetching teams from ESPN API"
        }
    except httpx.HTTPStatusError as e:
        return {
            "teams_count": 0,
            "last_updated": None,
            "success": False,
            "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
        }
    except Exception as e:
        return {
            "teams_count": 0,
            "last_updated": None,
            "success": False,
            "error": f"Unexpected error fetching teams: {str(e)}"
        }


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
    """
    try:
        # Validate team_id
        if not team_id or not isinstance(team_id, str):
            return {
                "team_id": team_id,
                "team_name": None,
                "depth_chart": [],
                "success": False,
                "error": "Team ID is required and must be a string"
            }
        
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
            
            return {
                "team_id": team_id.upper(),
                "team_name": team_name,
                "depth_chart": depth_chart,
                "success": True,
                "error": None
            }
            
    except httpx.TimeoutException:
        return {
            "team_id": team_id,
            "team_name": None,
            "depth_chart": [],
            "success": False,
            "error": "Request timed out while fetching depth chart"
        }
    except httpx.HTTPStatusError as e:
        return {
            "team_id": team_id,
            "team_name": None,
            "depth_chart": [],
            "success": False,
            "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
        }
    except Exception as e:
        return {
            "team_id": team_id,
            "team_name": None,
            "depth_chart": [],
            "success": False,
            "error": f"Unexpected error fetching depth chart: {str(e)}"
        }