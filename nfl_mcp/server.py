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
    is_valid_url, validate_limit, LIMITS, validate_string_input, 
    validate_numeric_input, validate_url_enhanced, sanitize_content
)
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors, handle_database_errors, handle_validation_error
)
from . import sleeper_tools
from . import nfl_tools 
from . import athlete_tools
from . import web_tools


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
            - error_type: Type of error (if any)
        """
        return await nfl_tools.get_nfl_news(limit)

    # MCP Tool: Get NFL league leaders
    @mcp.tool
    async def get_league_leaders(season: Optional[int] = 2025, season_type: Optional[int] = 2, limit: Optional[int] = 25) -> dict:
        """Get current NFL statistical leaders.

        Args:
            season: Season year (e.g. 2025)
            season_type: 1=Preseason, 2=Regular, 3=Postseason
            limit: Max categories to return (cap 100)
        """
        # Lightweight numeric validation with fallbacks
        try:
            if season is not None:
                season = int(season)
        except Exception:
            season = 2025
        try:
            if season_type is not None:
                season_type = int(season_type)
        except Exception:
            season_type = 2
        try:
            if limit is not None:
                limit = int(limit)
        except Exception:
            limit = 25
        return await nfl_tools.get_league_leaders(season=season or 2025, season_type=season_type or 2, limit=limit or 25)

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
            - error_type: Type of error (if any)
        """
        return await nfl_tools.get_teams()

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
            - error_type: Type of error (if any)
        """
        return await nfl_tools.fetch_teams(nfl_db)

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
        # Validate team_id input
        try:
            team_id = validate_string_input(team_id, 'team_id', max_length=4, required=True)
        except ValueError as e:
            return {
                "team_id": team_id,
                "team_name": None,
                "depth_chart": [],
                "success": False,
                "error": f"Invalid team_id: {str(e)}"
            }
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
        # Enhanced URL validation for security
        try:
            url = validate_string_input(url, 'general', max_length=2000, required=True)
        except ValueError as e:
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": f"Invalid URL: {str(e)}"
            }
        
        if not validate_url_enhanced(url):
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": "URL validation failed - potentially unsafe URL"
            }
        
        # Validate max_length parameter
        try:
            max_length = validate_numeric_input(max_length, min_val=100, max_val=50000, default=10000, required=False)
        except ValueError:
            max_length = 10000
        
        try:
            headers = get_http_headers("web_crawler")
            
            async with create_http_client() as client:
                # Fetch the URL
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                # Parse HTML content
                soup = BeautifulSoup(response.text, 'lxml')
                
                # Extract title and sanitize
                title_tag = soup.find('title')
                title = sanitize_content(title_tag.get_text().strip()) if title_tag else None
                
                # Remove script and style elements
                for script in soup(["script", "style", "nav", "footer", "aside", "form"]):
                    script.extract()
                
                # Get text content
                text = soup.get_text()
                
                # Use enhanced content sanitization
                text = sanitize_content(text, max_length=max_length)
                
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
        # Validate athlete_id input
        try:
            athlete_id = validate_string_input(athlete_id, 'alphanumeric_id', max_length=50, required=True)
        except ValueError as e:
            return {
                "athlete": None,
                "found": False,
                "error": f"Invalid athlete_id: {str(e)}"
            }
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
        # Validate name input
        try:
            name = validate_string_input(name, 'athlete_name', max_length=100, required=True)
        except ValueError as e:
            return {
                "athletes": [],
                "count": 0,
                "search_term": name,
                "error": f"Invalid name: {str(e)}"
            }
        
        # Validate limit parameter
        limit = validate_limit(
            limit,
            LIMITS["athletes_search_min"],
            LIMITS["athletes_search_max"],
            LIMITS["athletes_search_default"]
        )
        
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
        # Validate team_id input
        try:
            team_id = validate_string_input(team_id, 'team_id', max_length=4, required=True)
        except ValueError as e:
            return {
                "athletes": [],
                "count": 0,
                "team_id": team_id,
                "error": f"Invalid team_id: {str(e)}"
            }
        
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
    
    # Register all Sleeper API tools from the sleeper_tools module
    @mcp.tool
    async def get_league(league_id: str) -> dict:
        """Get league information with input validation."""
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
            return await sleeper_tools.get_league(league_id)
        except ValueError as e:
            return {
                "league": None,
                "success": False,
                "error": f"Invalid league_id: {str(e)}"
            }

    @mcp.tool
    async def get_rosters(league_id: str) -> dict:
        """Get league rosters with input validation."""
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
            return await sleeper_tools.get_rosters(league_id)
        except ValueError as e:
            return {
                "rosters": [],
                "count": 0,
                "success": False,
                "error": f"Invalid league_id: {str(e)}"
            }

    @mcp.tool
    async def get_league_users(league_id: str) -> dict:
        """Get league users with input validation."""
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
            return await sleeper_tools.get_league_users(league_id)
        except ValueError as e:
            return {
                "users": [],
                "count": 0,
                "success": False,
                "error": f"Invalid league_id: {str(e)}"
            }
    
    @mcp.tool
    async def get_matchups(league_id: str, week: int) -> dict:
        """Get league matchups with input validation."""
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
            week = validate_numeric_input(week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
            return await sleeper_tools.get_matchups(league_id, week)
        except ValueError as e:
            return {
                "matchups": [],
                "week": week,
                "count": 0,
                "success": False,
                "error": f"Invalid input: {str(e)}"
            }
    
    @mcp.tool
    async def get_playoff_bracket(league_id: str) -> dict:
        """Get playoff bracket with input validation."""
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
            return await sleeper_tools.get_playoff_bracket(league_id)
        except ValueError as e:
            return {
                "playoff_bracket": None,
                "success": False,
                "error": f"Invalid league_id: {str(e)}"
            }
    
    @mcp.tool
    async def get_transactions(league_id: str, round: Optional[int] = None) -> dict:
        """Get league transactions with input validation."""
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
            if round is not None:
                round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
            return await sleeper_tools.get_transactions(league_id, round)
        except ValueError as e:
            return {
                "transactions": [],
                "round": round,
                "count": 0,
                "success": False,
                "error": f"Invalid input: {str(e)}"
            }
    
    @mcp.tool
    async def get_traded_picks(league_id: str) -> dict:
        """Get traded picks with input validation."""
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
            return await sleeper_tools.get_traded_picks(league_id)
        except ValueError as e:
            return {
                "traded_picks": [],
                "count": 0,
                "success": False,
                "error": f"Invalid league_id: {str(e)}"
            }
    
    @mcp.tool
    async def get_nfl_state() -> dict:
        """Get NFL state - no validation needed as it has no parameters."""
        return await sleeper_tools.get_nfl_state()
    
    @mcp.tool
    async def get_trending_players(trend_type: str = "add", lookback_hours: Optional[int] = 24, limit: Optional[int] = 25) -> dict:
        """Get trending players with input validation."""
        try:
            trend_type = validate_string_input(trend_type, 'trend_type', max_length=10, required=True)
            lookback_hours = validate_numeric_input(
                lookback_hours, 
                min_val=LIMITS["trending_lookback_min"], 
                max_val=LIMITS["trending_lookback_max"], 
                default=24, 
                required=False
            )
            limit = validate_numeric_input(
                limit,
                min_val=LIMITS["trending_limit_min"],
                max_val=LIMITS["trending_limit_max"],
                default=25,
                required=False
            )
            return await sleeper_tools.get_trending_players(trend_type, lookback_hours, limit)
        except ValueError as e:
            return {
                "trending_players": [],
                "trend_type": trend_type,
                "lookback_hours": lookback_hours,
                "count": 0,
                "success": False,
                "error": f"Invalid input: {str(e)}"
            }

    return mcp


def main():
    """Main entry point for the server."""
    app = create_app()
    
    # Run the server with HTTP transport on port 9000
    app.run(transport="http", port=9000, host="0.0.0.0")


if __name__ == "__main__":
    main()
