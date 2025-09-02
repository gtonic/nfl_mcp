#!/usr/bin/env python3
"""
NFL MCP Server

A FastMCP server that provides:
- Health endpoint (non-MCP REST endpoint)
- Multiply tool (MCP tool for arithmetic operations)
- URL crawling tool (MCP tool for web content extraction)
- NFL news tool (MCP tool for fetching latest NFL news from ESPN)
- NFL teams tool (MCP tool for fetching all NFL teams from ESPN)
- Athlete tools (MCP tools for fetching and looking up NFL athletes from Sleeper API)
"""

import re
import httpx
from typing import Optional
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from starlette.responses import JSONResponse

from .database import AthleteDatabase


def create_app() -> FastMCP:
    """Create and configure the FastMCP server application."""
    
    # Create FastMCP server instance
    mcp = FastMCP(
        name="NFL MCP Server"
    )
    
    # Initialize athlete database
    athlete_db = AthleteDatabase()
    
    # Health endpoint (non-MCP, directly exposed REST endpoint)
    @mcp.custom_route(path="/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint for monitoring server status."""
        return JSONResponse({
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.1.0"
        })
    
    # MCP Tool: Multiply two integers
    @mcp.tool
    def multiply(x: int, y: int) -> int:
        """
        Multiply two integer numbers and return the result.
        
        Args:
            x: First integer to multiply
            y: Second integer to multiply
            
        Returns:
            The product of x and y
        """
        return x * y
    
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
        if limit is None:
            limit = 50
        elif limit < 1:
            limit = 1
        elif limit > 50:
            limit = 50
            
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (NFL News Fetcher)"
            }
            
            # Build the ESPN API URL
            url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?limit={limit}"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                # Fetch the news from ESPN API
                response = await client.get(url, follow_redirects=True)
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
            
            # Build the ESPN API URL for teams
            url = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/2025/teams"
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                # Fetch the teams from ESPN API
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse JSON response
                data = response.json()
                
                # Extract teams from the response
                teams_data = data.get('items', [])
                
                # Process teams to extract key information
                processed_teams = []
                for team in teams_data:
                    processed_team = {
                        "id": team.get('id', ''),
                        "name": team.get('name', '') or team.get('displayName', '')
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
        # Validate URL format
        if not url.startswith(('http://', 'https://')):
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": "URL must start with http:// or https://"
            }
        
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Web Content Extractor)"
            }
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                # Fetch the URL
                response = await client.get(url, follow_redirects=True)
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
                count = athlete_db.upsert_athletes(athletes_data)
                last_updated = athlete_db.get_last_updated()
                
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
            athlete = athlete_db.get_athlete_by_id(athlete_id)
            
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
            # Validate limit
            if limit is None or limit < 1:
                limit = 10
            elif limit > 100:
                limit = 100
            
            athletes = athlete_db.search_athletes_by_name(name, limit)
            
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
            athletes = athlete_db.get_athletes_by_team(team_id)
            
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
    
    return mcp


def main():
    """Main entry point for the server."""
    app = create_app()
    
    # Run the server with HTTP transport on port 9000
    app.run(transport="http", port=9000, host="0.0.0.0")


if __name__ == "__main__":
    main()