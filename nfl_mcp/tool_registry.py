"""Simplified tool registry for NFL MCP Server.

This module contains all MCP tool definitions in a clean, maintainable way.
Tools are defined as regular functions and registered with the FastMCP server.
"""
from __future__ import annotations
from typing import Optional, List, Callable, Any
import json
import httpx
from .metrics import timing_decorator
from . import nfl_tools, sleeper_tools, waiver_tools
from .config import (
    validate_string_input, validate_limit, LIMITS, validate_numeric_input,
    validate_url_enhanced, get_http_headers, create_http_client, sanitize_content,
    FEATURE_LEAGUE_LEADERS
)
from .database import NFLDatabase

# Shared database instance - will be initialized by server
_nfl_db: NFLDatabase | None = None

def initialize_shared(db: NFLDatabase):
    """Initialize shared resources like database connection."""
    global _nfl_db
    _nfl_db = db

def get_all_tools() -> List[Callable]:
    """Get list of all tool functions to register with FastMCP server."""
    tools = [
        # NFL News and Info
        get_nfl_news,
        get_teams,
        fetch_teams,
        get_depth_chart,
        get_team_injuries,
        get_team_player_stats,
        get_nfl_standings,
        get_team_schedule,
        
        # Web Tools
        crawl_url,
        
        # Athlete Tools
        fetch_athletes,
        lookup_athlete,
        search_athletes,
        get_athletes_by_team,
        
        # Sleeper API Tools - Basic
        get_league,
        get_rosters,
        get_league_users,
        get_matchups,
        get_playoff_bracket,
        get_transactions,
        get_traded_picks,
        get_nfl_state,
        get_trending_players,
        
        # Sleeper API Tools - Strategic Planning (New from main)
        get_strategic_matchup_preview,
        get_season_bye_week_coordination,
        get_trade_deadline_analysis,
        get_playoff_preparation_plan,
        
        # Waiver Wire Analysis Tools (New from main)
        get_waiver_log,
        check_re_entry_status,
        get_waiver_wire_dashboard,
    ]
    
    # Add feature-flagged tools
    if FEATURE_LEAGUE_LEADERS:
        tools.append(get_league_leaders)
    
    return tools


# =============================================================================
# NFL NEWS AND INFO TOOLS
# =============================================================================

@timing_decorator("get_nfl_news", tool_type="nfl")
async def get_nfl_news(limit: Optional[int] = 50) -> dict:
    """Fetch latest NFL news headlines from ESPN.

    Parameters:
        limit (int, default 50, range 1-50): Max number of articles.
    Returns: {articles: [...], total_articles, success, error?}
    Example: get_nfl_news(limit=10)
    """
    return await nfl_tools.get_nfl_news(limit)


@timing_decorator("get_teams", tool_type="nfl")
async def get_teams() -> dict:
    """Get all NFL teams from ESPN API.
    
    Returns: {teams: [...], total_teams, success, error?}
    Example: get_teams()
    """
    return await nfl_tools.get_teams()


@timing_decorator("fetch_teams", tool_type="nfl")
async def fetch_teams() -> dict:
    """Fetch all NFL teams from ESPN API and store them in database.
    
    Returns: {teams_count, last_updated, success, error?}
    Example: fetch_teams()
    """
    return await nfl_tools.fetch_teams(_nfl_db)


@timing_decorator("get_depth_chart", tool_type="nfl")
async def get_depth_chart(team_id: str) -> dict:
    """Fetch a team's depth chart from ESPN HTML page.

    Parameters:
        team_id (str, required): Team abbreviation (e.g. 'KC','NE','DAL').
    Returns: {team_id, team_name, depth_chart:[{position, players[]}], success, error?}
    Example: get_depth_chart(team_id="KC")
    """
    try:
        team_id = validate_string_input(team_id, 'team_id', max_length=4, required=True)
    except ValueError as e:
        return {"team_id": team_id, "team_name": None, "depth_chart": [], "success": False, "error": f"Invalid team_id: {e}"}
    
    try:
        timeout = httpx.Timeout(30.0, connect=10.0)
        headers = {"User-Agent": "NFL-MCP-Server/0.1.0 (NFL Depth Chart Fetcher)"}
        url = f"https://www.espn.com/nfl/team/depth/_/name/{team_id.upper()}"
        
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            team_name = soup.find('h1').get_text(strip=True) if soup.find('h1') else None
            depth_chart = []
            
            depth_sections = soup.find_all(['table', 'div'], class_=lambda x: x and 'depth' in x.lower() if x else False) or soup.find_all('table')
            for section in depth_sections:
                for row in section.find_all('tr'):
                    cells = row.find_all(['td','th'])
                    if len(cells) >= 2:
                        position = cells[0].get_text(strip=True)
                        players = [c.get_text(strip=True) for c in cells[1:] if c.get_text(strip=True) and c.get_text(strip=True) != position]
                        if position and players:
                            depth_chart.append({"position": position, "players": players})
            
            return {"team_id": team_id.upper(), "team_name": team_name, "depth_chart": depth_chart, "success": True, "error": None}
            
    except httpx.TimeoutException:
        return {"team_id": team_id, "team_name": None, "depth_chart": [], "success": False, "error": "Request timed out while fetching depth chart"}
    except httpx.HTTPStatusError as e:
        return {"team_id": team_id, "team_name": None, "depth_chart": [], "success": False, "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"}
    except Exception as e:
        return {"team_id": team_id, "team_name": None, "depth_chart": [], "success": False, "error": f"Unexpected error fetching depth chart: {str(e)}"}


@timing_decorator("get_team_injuries", tool_type="nfl")
async def get_team_injuries(team_id: str, limit: Optional[int] = 50) -> dict:
    """Fetch current injury report for a team (ESPN Core API).

    Parameters:
        team_id (str, required): Team abbreviation or ESPN team id (e.g. 'KC').
        limit (int, default 50, range 1-100): Max injuries to return.
    Returns: {team_id, team_name, injuries:[...], count, success, error?}
    Example: get_team_injuries(team_id="KC", limit=20)
    """
    try:
        if team_id is None or not isinstance(team_id, str):
            raise ValueError("team_id required")
        limit_val = int(limit) if limit is not None else 50
    except Exception:
        limit_val = 50
    return await nfl_tools.get_team_injuries(team_id=team_id, limit=limit_val)


@timing_decorator("get_team_player_stats", tool_type="nfl")
async def get_team_player_stats(team_id: str, season: Optional[int] = 2025, season_type: Optional[int] = 2, limit: Optional[int] = 50) -> dict:
    """Fetch current season player summary stats for a team.

    Parameters:
        team_id (str, required): Team abbreviation or ESPN id.
        season (int, default 2025): Season year.
        season_type (int, default 2): 1=Pre,2=Regular,3=Post.
        limit (int, default 50, range 1-100): Max players.
    Returns: {team_id, season, season_type, player_stats:[...], count, success, error?}
    Example: get_team_player_stats(team_id="KC", season=2024, limit=25)
    """
    try:
        season_i = int(season) if season is not None else 2025
    except Exception:
        season_i = 2025
    try:
        season_type_i = int(season_type) if season_type is not None else 2
    except Exception:
        season_type_i = 2
    try:
        limit_i = int(limit) if limit is not None else 50
    except Exception:
        limit_i = 50
    return await nfl_tools.get_team_player_stats(team_id=team_id, season=season_i, season_type=season_type_i, limit=limit_i)


@timing_decorator("get_nfl_standings", tool_type="nfl")
async def get_nfl_standings(season: Optional[int] = 2025, season_type: Optional[int] = 2, group: Optional[int] = None) -> dict:
    """Fetch NFL standings (league or conference) from ESPN Core API.

    Parameters:
        season (int, default 2025): Season year.
        season_type (int, default 2): 1=Pre,2=Regular,3=Post.
        group (int, optional): 1=AFC,2=NFC, None=all.
    Returns: {standings:[...], season, season_type, group, count, success, error?}
    Example: get_nfl_standings(season=2024, group=1)
    """
    try:
        season_i = int(season) if season is not None else 2025
    except Exception:
        season_i = 2025
    try:
        season_type_i = int(season_type) if season_type is not None else 2
    except Exception:
        season_type_i = 2
    try:
        group_i = int(group) if group is not None else None
    except Exception:
        group_i = None
    return await nfl_tools.get_nfl_standings(season=season_i, season_type=season_type_i, group=group_i)


@timing_decorator("get_team_schedule", tool_type="nfl")
async def get_team_schedule(team_id: str, season: Optional[int] = 2025) -> dict:
    """Fetch a team's schedule (Site API) including matchup context.

    Parameters:
        team_id (str, required): Team abbreviation or ESPN id.
        season (int, default 2025): Season year.
    Returns: {team_id, team_name, season, schedule:[...], count, success, error?}
    Example: get_team_schedule(team_id="KC", season=2024)
    """
    try:
        season_i = int(season) if season is not None else 2025
    except Exception:
        season_i = 2025
    return await nfl_tools.get_team_schedule(team_id=team_id, season=season_i)


# =============================================================================
# WEB TOOLS
# =============================================================================

@timing_decorator("crawl_url", tool_type="web")
async def crawl_url(url: str, max_length: Optional[int] = 10000) -> dict:
    """Crawl URL and extract text content for LLM processing.

    Parameters:
        url (str, required): The URL to crawl (must include http:// or https://).
        max_length (int, default 10000, range 100-50000): Maximum length of extracted text.
    Returns: {url, title, content, content_length, success, error?}
    Example: crawl_url(url="https://example.com", max_length=5000)
    """
    try:
        url = validate_string_input(url, 'general', max_length=2000, required=True)
    except ValueError as e:
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": f"Invalid URL: {str(e)}"}
    
    if not validate_url_enhanced(url):
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": "URL validation failed - potentially unsafe URL"}
    
    try:
        max_length = validate_numeric_input(max_length, min_val=100, max_val=50000, default=10000, required=False)
    except ValueError:
        max_length = 10000
    
    try:
        headers = get_http_headers("web_crawler")
        
        async with create_http_client() as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'lxml')
            
            title_tag = soup.find('title')
            title = sanitize_content(title_tag.get_text().strip()) if title_tag else None
            
            for script in soup(["script", "style", "nav", "footer", "aside", "form"]):
                script.extract()
            
            text = soup.get_text()
            text = sanitize_content(text, max_length=max_length)
            
            return {"url": url, "title": title, "content": text, "content_length": len(text), "success": True, "error": None}
            
    except httpx.TimeoutException:
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": "Request timed out"}
    except httpx.HTTPStatusError as e:
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"}
    except Exception as e:
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": f"Unexpected error: {str(e)}"}


# =============================================================================
# ATHLETE TOOLS
# =============================================================================

@timing_decorator("fetch_athletes", tool_type="athlete")
async def fetch_athletes() -> dict:
    """Fetch all NFL players from Sleeper API and store in database.
    
    Returns: {athletes_count, last_updated, success, error?}
    Example: fetch_athletes()
    """
    try:
        timeout = httpx.Timeout(60.0, connect=15.0)
        headers = {"User-Agent": "NFL-MCP-Server/0.1.0 (NFL Athletes Fetcher)"}
        url = "https://api.sleeper.app/v1/players/nfl"
        
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
            
            athletes_data = response.json()
            count = _nfl_db.upsert_athletes(athletes_data)
            last_updated = _nfl_db.get_last_updated()
            
            return {"athletes_count": count, "last_updated": last_updated, "success": True, "error": None}
            
    except httpx.TimeoutException:
        return {"athletes_count": 0, "last_updated": None, "success": False, "error": "Request timed out while fetching athletes from Sleeper API"}
    except httpx.HTTPStatusError as e:
        return {"athletes_count": 0, "last_updated": None, "success": False, "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"}
    except Exception as e:
        return {"athletes_count": 0, "last_updated": None, "success": False, "error": f"Unexpected error fetching athletes: {str(e)}"}


@timing_decorator("lookup_athlete", tool_type="athlete")
def lookup_athlete(athlete_id: str) -> dict:
    """Look up an athlete by their ID.
    
    Parameters:
        athlete_id (str, required): The unique identifier for the athlete.
    Returns: {athlete, found, error?}
    Example: lookup_athlete(athlete_id="4034")
    """
    try:
        athlete_id = validate_string_input(athlete_id, 'alphanumeric_id', max_length=50, required=True)
    except ValueError as e:
        return {"athlete": None, "found": False, "error": f"Invalid athlete_id: {str(e)}"}
    
    try:
        athlete = _nfl_db.get_athlete_by_id(athlete_id)
        
        if athlete:
            return {"athlete": athlete, "found": True, "error": None}
        else:
            return {"athlete": None, "found": False, "error": f"Athlete with ID '{athlete_id}' not found"}
            
    except Exception as e:
        return {"athlete": None, "found": False, "error": f"Error looking up athlete: {str(e)}"}


@timing_decorator("search_athletes", tool_type="athlete")
def search_athletes(name: str, limit: Optional[int] = 10) -> dict:
    """Search for athletes by name (partial match supported).
    
    Parameters:
        name (str, required): Name or partial name to search for.
        limit (int, default 10, range 1-50): Maximum number of results.
    Returns: {athletes: [...], count, search_term, error?}
    Example: search_athletes(name="Mahomes", limit=5)
    """
    try:
        name = validate_string_input(name, 'athlete_name', max_length=100, required=True)
    except ValueError as e:
        return {"athletes": [], "count": 0, "search_term": name, "error": f"Invalid name: {str(e)}"}
    
    limit = validate_limit(limit, LIMITS["athletes_search_min"], LIMITS["athletes_search_max"], LIMITS["athletes_search_default"])
    
    try:
        athletes = _nfl_db.search_athletes_by_name(name, limit)
        return {"athletes": athletes, "count": len(athletes), "search_term": name, "error": None}
        
    except Exception as e:
        return {"athletes": [], "count": 0, "search_term": name, "error": f"Error searching athletes: {str(e)}"}


@timing_decorator("get_athletes_by_team", tool_type="athlete")
def get_athletes_by_team(team_id: str) -> dict:
    """Get all athletes for a specific team.
    
    Parameters:
        team_id (str, required): The team identifier (e.g., "SF", "DAL", "NE").
    Returns: {athletes: [...], count, team_id, error?}
    Example: get_athletes_by_team(team_id="KC")
    """
    try:
        team_id = validate_string_input(team_id, 'team_id', max_length=4, required=True)
    except ValueError as e:
        return {"athletes": [], "count": 0, "team_id": team_id, "error": f"Invalid team_id: {str(e)}"}
    
    try:
        athletes = _nfl_db.get_athletes_by_team(team_id)
        return {"athletes": athletes, "count": len(athletes), "team_id": team_id, "error": None}
        
    except Exception as e:
        return {"athletes": [], "count": 0, "team_id": team_id, "error": f"Error getting athletes for team: {str(e)}"}


# =============================================================================
# SLEEPER API TOOLS - BASIC
# =============================================================================

@timing_decorator("get_league", tool_type="sleeper")
async def get_league(league_id: str) -> dict:
    """Get league information with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_league(league_id)
    except ValueError as e:
        return {"league": None, "success": False, "error": f"Invalid league_id: {str(e)}"}


@timing_decorator("get_rosters", tool_type="sleeper")
async def get_rosters(league_id: str) -> dict:
    """Get league rosters with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_rosters(league_id)
    except ValueError as e:
        return {"rosters": [], "count": 0, "success": False, "error": f"Invalid league_id: {str(e)}"}


@timing_decorator("get_league_users", tool_type="sleeper")
async def get_league_users(league_id: str) -> dict:
    """Get league users with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_league_users(league_id)
    except ValueError as e:
        return {"users": [], "count": 0, "success": False, "error": f"Invalid league_id: {str(e)}"}


@timing_decorator("get_matchups", tool_type="sleeper")
async def get_matchups(league_id: str, week: int) -> dict:
    """Get league matchups with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        week = validate_numeric_input(week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        return await sleeper_tools.get_matchups(league_id, week)
    except ValueError as e:
        return {"matchups": [], "week": week, "count": 0, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_playoff_bracket", tool_type="sleeper")
async def get_playoff_bracket(league_id: str) -> dict:
    """Get playoff bracket with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_playoff_bracket(league_id)
    except ValueError as e:
        return {"playoff_bracket": None, "success": False, "error": f"Invalid league_id: {str(e)}"}


@timing_decorator("get_transactions", tool_type="sleeper")
async def get_transactions(league_id: str, round: Optional[int] = None) -> dict:
    """Get league transactions with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
        return await sleeper_tools.get_transactions(league_id, round)
    except ValueError as e:
        return {"transactions": [], "round": round, "count": 0, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_traded_picks", tool_type="sleeper")
async def get_traded_picks(league_id: str) -> dict:
    """Get traded picks with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_traded_picks(league_id)
    except ValueError as e:
        return {"traded_picks": [], "count": 0, "success": False, "error": f"Invalid league_id: {str(e)}"}


@timing_decorator("get_nfl_state", tool_type="sleeper")
async def get_nfl_state() -> dict:
    """Get NFL state - no validation needed as it has no parameters."""
    return await sleeper_tools.get_nfl_state()


@timing_decorator("get_trending_players", tool_type="sleeper")
async def get_trending_players(trend_type: str = "add", lookback_hours: Optional[int] = 24, limit: Optional[int] = 25) -> dict:
    """Get trending players with input validation."""
    try:
        trend_type = validate_string_input(trend_type, 'trend_type', max_length=10, required=True)
        lookback_hours = validate_numeric_input(lookback_hours, min_val=LIMITS["trending_lookback_min"], max_val=LIMITS["trending_lookback_max"], default=24, required=False)
        limit = validate_numeric_input(limit, min_val=LIMITS["trending_limit_min"], max_val=LIMITS["trending_limit_max"], default=25, required=False)
        return await sleeper_tools.get_trending_players(trend_type, lookback_hours, limit)
    except ValueError as e:
        return {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0, "success": False, "error": f"Invalid input: {str(e)}"}


# =============================================================================
# SLEEPER API TOOLS - STRATEGIC PLANNING (NEW FROM MAIN)
# =============================================================================

@timing_decorator("get_strategic_matchup_preview", tool_type="sleeper")
async def get_strategic_matchup_preview(league_id: str, current_week: int, weeks_ahead: Optional[int] = 4) -> dict:
    """Strategic preview of upcoming matchups for multi-week planning."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        current_week = validate_numeric_input(current_week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        weeks_ahead = validate_numeric_input(weeks_ahead, min_val=1, max_val=8, default=4, required=False)
        return await sleeper_tools.get_strategic_matchup_preview(league_id, current_week, weeks_ahead)
    except ValueError as e:
        return {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_season_bye_week_coordination", tool_type="sleeper")
async def get_season_bye_week_coordination(league_id: str, season: Optional[int] = 2025) -> dict:
    """Season-long bye week coordination with fantasy league schedule."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        season = validate_numeric_input(season, min_val=2020, max_val=2030, default=2025, required=False)
        return await sleeper_tools.get_season_bye_week_coordination(league_id, season)
    except ValueError as e:
        return {"coordination_plan": {}, "season": season, "league_id": league_id, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_trade_deadline_analysis", tool_type="sleeper")
async def get_trade_deadline_analysis(league_id: str, current_week: int) -> dict:
    """Strategic trade deadline timing analysis."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        current_week = validate_numeric_input(current_week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        return await sleeper_tools.get_trade_deadline_analysis(league_id, current_week)
    except ValueError as e:
        return {"trade_analysis": {}, "league_id": league_id, "current_week": current_week, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_playoff_preparation_plan", tool_type="sleeper")
async def get_playoff_preparation_plan(league_id: str, current_week: int) -> dict:
    """Comprehensive playoff preparation plan combining league and NFL data."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        current_week = validate_numeric_input(current_week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        return await sleeper_tools.get_playoff_preparation_plan(league_id, current_week)
    except ValueError as e:
        return {"playoff_plan": {}, "league_id": league_id, "readiness_score": 0, "success": False, "error": f"Invalid input: {str(e)}"}


# =============================================================================
# WAIVER WIRE ANALYSIS TOOLS (NEW FROM MAIN)
# =============================================================================

@timing_decorator("get_waiver_log", tool_type="waiver")
async def get_waiver_log(league_id: str, round: Optional[int] = None, dedupe: bool = True) -> dict:
    """Get waiver wire activity log with de-duplication."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
        return await waiver_tools.get_waiver_log(league_id, round, dedupe)
    except ValueError as e:
        return {"waiver_log": [], "league_id": league_id, "round": round, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("check_re_entry_status", tool_type="waiver")
async def check_re_entry_status(league_id: str, round: Optional[int] = None) -> dict:
    """Check player re-entry status on waiver wire."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
        return await waiver_tools.check_re_entry_status(league_id, round)
    except ValueError as e:
        return {"re_entry_status": {}, "league_id": league_id, "round": round, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_waiver_wire_dashboard", tool_type="waiver")
async def get_waiver_wire_dashboard(league_id: str, round: Optional[int] = None) -> dict:
    """Get comprehensive waiver wire analysis dashboard."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
        return await waiver_tools.get_waiver_wire_dashboard(league_id, round)
    except ValueError as e:
        return {"dashboard": {}, "league_id": league_id, "round": round, "success": False, "error": f"Invalid input: {str(e)}"}


# =============================================================================
# FEATURE-FLAGGED TOOLS
# =============================================================================

if FEATURE_LEAGUE_LEADERS:
    @timing_decorator("get_league_leaders", tool_type="nfl")
    async def get_league_leaders(stat_type: str = "passing", limit: Optional[int] = 25) -> dict:
        """Get NFL league leaders by stat type (feature-flagged).
        
        Parameters:
            stat_type (str, default "passing"): Type of stat to get leaders for.
            limit (int, default 25, range 1-100): Max leaders to return.
        Returns: {leaders: [...], stat_type, count, success, error?}
        Example: get_league_leaders(stat_type="rushing", limit=10)
        """
        try:
            stat_type = validate_string_input(stat_type, 'stat_type', max_length=20, required=True)
            limit = validate_limit(limit, 1, 100, 25)
            return await nfl_tools.get_league_leaders(stat_type, limit)
        except ValueError as e:
            return {"leaders": [], "stat_type": stat_type, "count": 0, "success": False, "error": f"Invalid input: {str(e)}"}