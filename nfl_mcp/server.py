#!/usr/bin/env python3
"""
NFL MCP Server

A FastMCP server that provides:
- Health endpoint (non-MCP REST endpoint)
- URL crawling tool (MCP tool for web content extraction)
- NFL news tool (MCP tool for fetching latest NFL news from ESPN)
- NFL teams tool (MCP tool for fetching all NFL teams from ESPN)
- Athlete tools (MCP tools for fetching and looking up NFL athletes from Sleeper API)
- Sleeper API tools (MCP tools for comprehensive fantasy league management):
  - League information, rosters, users, matchups, playoffs
  - Transactions, traded picks, NFL state, trending players
"""

import re
import httpx
from typing import Optional
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from starlette.responses import JSONResponse

from .database import NFLDatabase
from .config import (
    DEFAULT_TIMEOUT, get_http_headers, create_http_client, 
    is_valid_url, validate_limit, LIMITS
)


def create_app() -> FastMCP:
    """Create and configure the FastMCP server application."""
    
    # Create FastMCP server instance
    mcp = FastMCP(
        name="NFL MCP Server"
    )
    
    # Initialize NFL database
    nfl_db = NFLDatabase()
    
    # Health endpoint (non-MCP, directly exposed REST endpoint)
    @mcp.custom_route(path="/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint for monitoring server status."""
        return JSONResponse({
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.1.0"
        })
    

    
    # MCP Tool: Get NFL news from ESPN
    @mcp.tool
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

    # MCP Tool: Get NFL teams
    @mcp.tool
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
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (NFL Teams Fetcher)"
            }
            
            # Build the ESPN API URL for teams (fixed to use correct endpoint)
            url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                # Fetch the teams from ESPN API
                response = await client.get(url, follow_redirects=True)
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

    # MCP Tool: Fetch teams from ESPN API and store in database
    @mcp.tool
    async def fetch_teams() -> dict:
        """
        Fetch all NFL teams from ESPN API and store them in the local database.
        
        This tool fetches the complete teams data from ESPN's API and 
        upserts the data into the SQLite database for fast local lookups.
        
        Returns:
            A dictionary containing:
            - teams_count: Number of teams processed
            - last_updated: Timestamp of the update
            - success: Whether the fetch was successful
            - error: Error message (if any)
        """
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (NFL Teams Fetcher)"
            }
            
            # ESPN API endpoint for teams
            url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                # Fetch the teams from ESPN API
                response = await client.get(url, follow_redirects=True)
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

    # MCP Tool: Get team depth chart
    @mcp.tool
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
            
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (NFL Depth Chart Fetcher)"
            }
            
            # Build the ESPN depth chart URL
            url = f"https://www.espn.com/nfl/team/depth/_/name/{team_id.upper()}"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                # Fetch the depth chart page
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse HTML content
                from bs4 import BeautifulSoup
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

    # MCP Tool: Crawl URL and extract content
    @mcp.tool
    async def crawl_url(url: str, max_length: Optional[int] = 10000) -> dict:
        """
        Crawl a URL and extract its text content in a format understandable by LLMs.
        
        This tool fetches a web page, extracts the main text content, and returns
        it in a clean, structured format suitable for LLM processing.
        
        Args:
            url: The URL to crawl (must include http:// or https://)
            max_length: Maximum length of extracted text (default: 10000 characters)
            
        Returns:
            A dictionary containing:
            - url: The crawled URL
            - title: Page title (if available)
            - content: Cleaned text content
            - content_length: Length of extracted content
            - success: Whether the crawl was successful
            - error: Error message (if any)
        """
        # Validate URL format for security
        if not is_valid_url(url):
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": "URL must start with http:// or https://"
            }
        
        try:
            headers = get_http_headers("web_crawler")
            
            async with create_http_client() as client:
                # Fetch the URL
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                # Parse HTML content
                soup = BeautifulSoup(response.text, 'lxml')
                
                # Extract title
                title_tag = soup.find('title')
                title = title_tag.get_text().strip() if title_tag else None
                
                # Remove script and style elements
                for script in soup(["script", "style", "nav", "footer", "aside", "form"]):
                    script.extract()
                
                # Get text content
                text = soup.get_text()
                
                # Clean up the text
                lines = (line.strip() for line in text.splitlines())
                chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                text = ' '.join(chunk for chunk in chunks if chunk)
                
                # Remove excessive whitespace and normalize
                text = re.sub(r'\s+', ' ', text).strip()
                
                # Apply length limit if specified
                if max_length and len(text) > max_length:
                    text = text[:max_length] + "..."
                
                return {
                    "url": url,
                    "title": title,
                    "content": text,
                    "content_length": len(text),
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": "Request timed out"
            }
        except httpx.HTTPStatusError as e:
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }
    
    # MCP Tool: Fetch athletes from Sleeper API and store in database
    @mcp.tool
    async def fetch_athletes() -> dict:
        """
        Fetch all NFL players from Sleeper API and store them in the local database.
        
        This tool fetches the complete athlete roster from Sleeper's API and 
        upserts the data into the SQLite database for fast local lookups.
        
        Returns:
            A dictionary containing:
            - athletes_count: Number of athletes processed
            - last_updated: Timestamp of the update
            - success: Whether the fetch was successful
            - error: Error message (if any)
        """
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(60.0, connect=15.0)  # Longer timeout for large dataset
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (NFL Athletes Fetcher)"
            }
            
            # Sleeper API endpoint for all players
            url = "https://api.sleeper.app/v1/players/nfl"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                # Fetch the athletes from Sleeper API
                response = await client.get(url, follow_redirects=True)
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
    
    # MCP Tool: Lookup athlete by ID
    @mcp.tool
    def lookup_athlete(athlete_id: str) -> dict:
        """
        Look up an athlete by their ID.
        
        This tool queries the local database for an athlete with the given ID
        and returns their information including name, team, position, etc.
        
        Args:
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
    
    # MCP Tool: Search athletes by name
    @mcp.tool
    def search_athletes(name: str, limit: Optional[int] = 10) -> dict:
        """
        Search for athletes by name (partial match supported).
        
        This tool searches the local database for athletes whose names match
        the given search term, supporting partial matches.
        
        Args:
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
    
    # MCP Tool: Get athletes by team
    @mcp.tool  
    def get_athletes_by_team(team_id: str) -> dict:
        """
        Get all athletes for a specific team.
        
        This tool retrieves all athletes associated with a given team ID
        from the local database.
        
        Args:
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

    # SLEEPER API TOOLS
    
    # MCP Tool: Get league information by ID
    @mcp.tool
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
        """
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Sleeper League Fetcher)"
            }
            
            # Sleeper API endpoint for specific league
            url = f"https://api.sleeper.app/v1/league/{league_id}"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                league_data = response.json()
                
                return {
                    "league": league_data,
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "league": None,
                "success": False,
                "error": "Request timed out while fetching league information"
            }
        except httpx.HTTPStatusError as e:
            return {
                "league": None,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "league": None,
                "success": False,
                "error": f"Unexpected error fetching league: {str(e)}"
            }

    # MCP Tool: Get rosters in a league
    @mcp.tool
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
        """
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Sleeper Rosters Fetcher)"
            }
            
            # Sleeper API endpoint for league rosters
            url = f"https://api.sleeper.app/v1/league/{league_id}/rosters"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                rosters_data = response.json()
                
                return {
                    "rosters": rosters_data,
                    "count": len(rosters_data) if rosters_data else 0,
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "rosters": [],
                "count": 0,
                "success": False,
                "error": "Request timed out while fetching league rosters"
            }
        except httpx.HTTPStatusError as e:
            return {
                "rosters": [],
                "count": 0,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "rosters": [],
                "count": 0,
                "success": False,
                "error": f"Unexpected error fetching rosters: {str(e)}"
            }

    # MCP Tool: Get users in a league
    @mcp.tool
    async def get_league_users(league_id: str) -> dict:
        """
        Get all users in a fantasy league from Sleeper API.
        
        This tool fetches information about all users/managers in a specific
        fantasy league including usernames, display names, and avatars.
        
        Args:
            league_id: The unique identifier for the league
            
        Returns:
            A dictionary containing:
            - users: List of all users in the league
            - count: Number of users found
            - success: Whether the request was successful
            - error: Error message (if any)
        """
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Sleeper Users Fetcher)"
            }
            
            # Sleeper API endpoint for league users
            url = f"https://api.sleeper.app/v1/league/{league_id}/users"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                users_data = response.json()
                
                return {
                    "users": users_data,
                    "count": len(users_data) if users_data else 0,
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "users": [],
                "count": 0,
                "success": False,
                "error": "Request timed out while fetching league users"
            }
        except httpx.HTTPStatusError as e:
            return {
                "users": [],
                "count": 0,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "users": [],
                "count": 0,
                "success": False,
                "error": f"Unexpected error fetching users: {str(e)}"
            }

    # MCP Tool: Get matchups in a league for a specific week
    @mcp.tool
    async def get_matchups(league_id: str, week: int) -> dict:
        """
        Get matchups for a specific week in a fantasy league from Sleeper API.
        
        This tool fetches matchup information including points scored, roster IDs,
        and other matchup details for the specified week.
        
        Args:
            league_id: The unique identifier for the league
            week: The week number (1-18 for regular season, 19+ for playoffs)
            
        Returns:
            A dictionary containing:
            - matchups: List of all matchups for the week
            - week: The week number requested
            - count: Number of matchups found
            - success: Whether the request was successful
            - error: Error message (if any)
        """
        try:
            # Validate week parameter
            if week < 1 or week > 22:
                return {
                    "matchups": [],
                    "week": week,
                    "count": 0,
                    "success": False,
                    "error": "Week must be between 1 and 22"
                }
            
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Sleeper Matchups Fetcher)"
            }
            
            # Sleeper API endpoint for league matchups
            url = f"https://api.sleeper.app/v1/league/{league_id}/matchups/{week}"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                matchups_data = response.json()
                
                return {
                    "matchups": matchups_data,
                    "week": week,
                    "count": len(matchups_data) if matchups_data else 0,
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "matchups": [],
                "week": week,
                "count": 0,
                "success": False,
                "error": "Request timed out while fetching matchups"
            }
        except httpx.HTTPStatusError as e:
            return {
                "matchups": [],
                "week": week,
                "count": 0,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "matchups": [],
                "week": week,
                "count": 0,
                "success": False,
                "error": f"Unexpected error fetching matchups: {str(e)}"
            }

    # MCP Tool: Get playoff bracket for a league
    @mcp.tool
    async def get_playoff_bracket(league_id: str) -> dict:
        """
        Get playoff bracket information for a fantasy league from Sleeper API.
        
        This tool fetches the playoff bracket structure including matchups,
        rounds, and playoff results for the specified league.
        
        Args:
            league_id: The unique identifier for the league
            
        Returns:
            A dictionary containing:
            - bracket: Playoff bracket information
            - success: Whether the request was successful
            - error: Error message (if any)
        """
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Sleeper Playoffs Fetcher)"
            }
            
            # Sleeper API endpoint for playoff bracket
            url = f"https://api.sleeper.app/v1/league/{league_id}/playoffs_bracket"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                bracket_data = response.json()
                
                return {
                    "bracket": bracket_data,
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "bracket": None,
                "success": False,
                "error": "Request timed out while fetching playoff bracket"
            }
        except httpx.HTTPStatusError as e:
            return {
                "bracket": None,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "bracket": None,
                "success": False,
                "error": f"Unexpected error fetching playoff bracket: {str(e)}"
            }

    # MCP Tool: Get transactions for a league
    @mcp.tool
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
        """
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Sleeper Transactions Fetcher)"
            }
            
            # Build URL - with or without round
            if round is not None:
                if round < 1 or round > 18:
                    return {
                        "transactions": [],
                        "round": round,
                        "count": 0,
                        "success": False,
                        "error": "Round must be between 1 and 18"
                    }
                url = f"https://api.sleeper.app/v1/league/{league_id}/transactions/{round}"
            else:
                url = f"https://api.sleeper.app/v1/league/{league_id}/transactions"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                transactions_data = response.json()
                
                return {
                    "transactions": transactions_data,
                    "round": round,
                    "count": len(transactions_data) if transactions_data else 0,
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "transactions": [],
                "round": round,
                "count": 0,
                "success": False,
                "error": "Request timed out while fetching transactions"
            }
        except httpx.HTTPStatusError as e:
            return {
                "transactions": [],
                "round": round,
                "count": 0,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "transactions": [],
                "round": round,
                "count": 0,
                "success": False,
                "error": f"Unexpected error fetching transactions: {str(e)}"
            }

    # MCP Tool: Get traded picks for a league
    @mcp.tool
    async def get_traded_picks(league_id: str) -> dict:
        """
        Get traded draft picks for a fantasy league from Sleeper API.
        
        This tool fetches information about draft picks that have been traded
        between teams in the specified league.
        
        Args:
            league_id: The unique identifier for the league
            
        Returns:
            A dictionary containing:
            - traded_picks: List of traded draft picks
            - count: Number of traded picks found
            - success: Whether the request was successful
            - error: Error message (if any)
        """
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Sleeper Traded Picks Fetcher)"
            }
            
            # Sleeper API endpoint for traded picks
            url = f"https://api.sleeper.app/v1/league/{league_id}/traded_picks"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                traded_picks_data = response.json()
                
                return {
                    "traded_picks": traded_picks_data,
                    "count": len(traded_picks_data) if traded_picks_data else 0,
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "traded_picks": [],
                "count": 0,
                "success": False,
                "error": "Request timed out while fetching traded picks"
            }
        except httpx.HTTPStatusError as e:
            return {
                "traded_picks": [],
                "count": 0,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "traded_picks": [],
                "count": 0,
                "success": False,
                "error": f"Unexpected error fetching traded picks: {str(e)}"
            }

    # MCP Tool: Get NFL state
    @mcp.tool
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
        """
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Sleeper NFL State Fetcher)"
            }
            
            # Sleeper API endpoint for NFL state
            url = "https://api.sleeper.app/v1/state/nfl"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                nfl_state_data = response.json()
                
                return {
                    "nfl_state": nfl_state_data,
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "nfl_state": None,
                "success": False,
                "error": "Request timed out while fetching NFL state"
            }
        except httpx.HTTPStatusError as e:
            return {
                "nfl_state": None,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "nfl_state": None,
                "success": False,
                "error": f"Unexpected error fetching NFL state: {str(e)}"
            }

    # MCP Tool: Get trending players
    @mcp.tool
    async def get_trending_players(trend_type: str = "add", lookback_hours: Optional[int] = 24, limit: Optional[int] = 25) -> dict:
        """
        Get trending players from Sleeper API.
        
        This tool fetches players that are trending in fantasy leagues,
        either being added or dropped frequently.
        
        Args:
            trend_type: Type of trend - "add" or "drop" (default: "add")
            lookback_hours: Hours to look back for trends (default: 24, max: 168)
            limit: Maximum number of players to return (default: 25, max: 100)
            
        Returns:
            A dictionary containing:
            - trending_players: List of trending players with trend data
            - trend_type: The type of trend requested
            - lookback_hours: Hours looked back
            - count: Number of players returned
            - success: Whether the request was successful
            - error: Error message (if any)
        """
        try:
            # Validate parameters
            if trend_type not in ["add", "drop"]:
                return {
                    "trending_players": [],
                    "trend_type": trend_type,
                    "lookback_hours": lookback_hours,
                    "count": 0,
                    "success": False,
                    "error": "trend_type must be 'add' or 'drop'"
                }
            
            if lookback_hours is not None and (lookback_hours < 1 or lookback_hours > 168):
                return {
                    "trending_players": [],
                    "trend_type": trend_type,
                    "lookback_hours": lookback_hours,
                    "count": 0,
                    "success": False,
                    "error": "lookback_hours must be between 1 and 168"
                }
            
            if limit is not None and (limit < 1 or limit > 100):
                return {
                    "trending_players": [],
                    "trend_type": trend_type,
                    "lookback_hours": lookback_hours,
                    "count": 0,
                    "success": False,
                    "error": "limit must be between 1 and 100"
                }
            
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Sleeper Trending Players Fetcher)"
            }
            
            # Build URL with query parameters
            url = f"https://api.sleeper.app/v1/players/nfl/trending/{trend_type}"
            params = {}
            if lookback_hours:
                params["lookback_hours"] = lookback_hours
            if limit:
                params["limit"] = limit
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.get(url, params=params, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                trending_data = response.json()

                # Enrich with local DB lookups for full names and metadata
                enriched_players = []
                for item in trending_data or []:
                    pid = str(item.get("player_id", ""))
                    cnt = item.get("count", 0)
                    athlete = nfl_db.get_athlete_by_id(pid)
                    if athlete:
                        full_name = athlete.get("full_name") or (
                            ((athlete.get("first_name") or "") + " " + (athlete.get("last_name") or "")).strip()
                        )
                        team = athlete.get("team_id")
                        position = athlete.get("position")
                        found = True
                    else:
                        full_name = None
                        team = None
                        position = None
                        found = False
                    enriched_players.append({
                        "player_id": pid,
                        "full_name": full_name,
                        "team_id": team,
                        "position": position,
                        "count": cnt,
                        "found": found
                    })

                return {
                    "trending_players": enriched_players,
                    "trend_type": trend_type,
                    "lookback_hours": lookback_hours,
                    "count": len(enriched_players),
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "trending_players": [],
                "trend_type": trend_type,
                "lookback_hours": lookback_hours,
                "count": 0,
                "success": False,
                "error": "Request timed out while fetching trending players"
            }
        except httpx.HTTPStatusError as e:
            return {
                "trending_players": [],
                "trend_type": trend_type,
                "lookback_hours": lookback_hours,
                "count": 0,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "trending_players": [],
                "trend_type": trend_type,
                "lookback_hours": lookback_hours,
                "count": 0,
                "success": False,
                "error": f"Unexpected error fetching trending players: {str(e)}"
            }
    
    return mcp


def main():
    """Main entry point for the server."""
    app = create_app()
    
    # Run the server with HTTP transport on port 9000
    app.run(transport="http", port=9000, host="0.0.0.0")


if __name__ == "__main__":
    main()
