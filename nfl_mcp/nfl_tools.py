"""
NFL-related MCP tools for the NFL MCP Server.

This module contains MCP tools for fetching NFL news, teams data, and depth charts.
"""

import httpx
from typing import Optional
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
    default_data={"players": [], "season": None, "category": None},
    operation_name="fetching league leaders"
)
async def get_league_leaders(category: str, season: int = 2025, season_type: int = 2) -> dict:
    """Fetch current NFL statistical leaders for a single category.

    Instead of returning all categories, this focuses on one requested category.

    Supported input categories (case-insensitive):
      - pass  (passing yards leaders)
      - rush  (rushing yards leaders)
      - receiving (receiving yards leaders)
      - tackles (total tackles leaders)
      - sacks (sacks leaders)

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
    url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/types/{season_type}/leaders"

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
        def extract_players(cat_obj):
            players_local = []
            for leader_group in cat_obj.get('leaders', []):
                for entry in (leader_group.get('leaders') or []):
                    athlete = entry.get('athlete', {})
                    team = entry.get('team', {})
                    players_local.append({
                        "rank": entry.get('rank'),
                        "value": entry.get('value'),
                        "athlete_id": athlete.get('id'),
                        "athlete_name": athlete.get('displayName') or athlete.get('shortName'),
                        "team_id": team.get('id'),
                        "team_abbr": team.get('abbreviation'),
                    })
            return players_local

        if not multi:
            tok = requested_tokens[0]
            cat_obj, disp = matches.get(tok, (None, None))  # type: ignore
            players = extract_players(cat_obj) if cat_obj else []
            return create_success_response({
                "season": season,
                "season_type": season_type,
                "category": tok,
                "stat_category_name": disp,
                "players": players,
                "players_count": len(players)
            })
        else:
            categories_payload = []
            for tok in requested_tokens:
                if tok in matches:
                    cat_obj, disp = matches[tok]
                    players = extract_players(cat_obj)
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
                "categories": categories_payload
            })