"""Tool registry with module-level FastMCP tool decorators.

This isolates tool function definitions outside of the `create_app` factory so
FastMCP 2.x + standard MCP clients can reliably discover them during the
initial ListToolsRequest.
"""
from __future__ import annotations
from typing import Optional
import json
import httpx
from fastmcp import FastMCP
from .metrics import timing_decorator
from . import nfl_tools, sleeper_tools
from .config import (
    validate_string_input, validate_limit, LIMITS, validate_numeric_input,
    validate_url_enhanced, get_http_headers, create_http_client, sanitize_content,
    FEATURE_LEAGUE_LEADERS
)
from .database import NFLDatabase

# A dedicated FastMCP instance just for decorating; functions will be transferred.
_dummy = FastMCP(name="_tool_registry")

def get_tools():
    # Return underlying FunctionTool objects
    return list(_dummy._tool_manager._tools.values())  # type: ignore[attr-defined]

# Shared single DB instance placeholder for pure read operations requiring DB; will be reset by server.
_nfl_db: NFLDatabase | None = None
_waiver_store = None

def initialize_shared(db: NFLDatabase):
    global _nfl_db
    _nfl_db = db

@_dummy.tool
@timing_decorator("get_nfl_news", tool_type="nfl")
async def get_nfl_news(limit: Optional[int] = 50) -> dict:
    """Get latest NFL news articles from ESPN API.
    
    Use this tool when users ask for current NFL news, recent stories, or league updates.
    
    Parameters:
        limit (int, optional): Number of articles to return (1-50, default: 50)
    
    Returns: {articles: [...], total_articles, success, error}
    
    Call this for: "NFL news", "recent NFL stories", "what's happening in the NFL"
    """
    return await nfl_tools.get_nfl_news(limit)

if FEATURE_LEAGUE_LEADERS:
    @_dummy.tool
    @timing_decorator("get_league_leaders", tool_type="nfl")
    async def get_league_leaders(category: str, season: Optional[int] = 2025, season_type: Optional[int] = 2, week: Optional[int] = None) -> dict:  # pragma: no cover - conditional
        """Get NFL statistical leaders by category (pass, rush, receiving, tackles, sacks).
        
        Use when users want to see who leads the NFL in specific statistical categories.
        
        Parameters:
            category (str, required): Stats to get - combine with commas for multiple (e.g., "pass, rush")
                - "pass" for passing leaders (yards, TDs, completions)
                - "rush" for rushing leaders (yards, TDs, attempts)  
                - "receiving" for receiving leaders (yards, receptions, TDs)
                - "tackles" for defensive tackle leaders
                - "sacks" for sack leaders
            season (int, optional): Year (default: 2025)
            season_type (int, optional): 1=Preseason, 2=Regular season, 3=Postseason (default: 2)
            week (int, optional): Specific week filter (currently ignored)
        
        Returns: Single category => {players: [...]}; Multiple => {categories: [...]}
        
        Call this for: "who leads in passing yards", "top rushers", "sack leaders", "receiving leaders"
        """
        # Basic numeric coercion
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
            if week is not None:
                week = int(week)
        except Exception:
            week = None
        return await nfl_tools.get_league_leaders(category=category, season=season or 2025, season_type=season_type or 2, week=week)

@_dummy.tool
@timing_decorator("get_teams", tool_type="nfl")
async def get_teams() -> dict:
    """Get all current NFL teams with basic information.
    
    Use this tool when users need a list of NFL teams, team names, or team IDs.
    
    Parameters: None
    
    Returns: {teams: [...], total_teams, success, error}
    
    Call this for: "NFL teams", "list all teams", "team names", "what teams are in the NFL"
    """
    return await nfl_tools.get_teams()

@_dummy.tool
@timing_decorator("fetch_teams", tool_type="nfl")
async def fetch_teams() -> dict:
    """Update local database with latest NFL team information from ESPN.
    
    Use this tool to refresh team data in the local cache before accessing team information.
    
    Parameters: None
    
    Returns: {teams_count, last_updated, success, error}
    
    Call this for: "update team data", "refresh teams", "fetch latest team info"
    """
    return await nfl_tools.fetch_teams(_nfl_db)  # type: ignore[arg-type]

@_dummy.tool
async def get_depth_chart(team_id: str) -> dict:
    """Get a team's depth chart showing player positions and depth order.
    
    Use this tool when users want to see starting lineups, backups, or positional depth for a team.
    
    Parameters:
        team_id (str, required): Team abbreviation (e.g., 'KC', 'NE', 'SF', 'DAL')
    
    Returns: {team_id, team_name, depth_chart: [{position, players}], success, error}
    
    Call this for: "Chiefs depth chart", "who starts at QB for KC", "team starters", "depth chart"
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
            response = await client.get(url, follow_redirects=True); response.raise_for_status()
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
        return {"team_id": team_id, "team_name": None, "depth_chart": [], "success": False, "error": f"Unexpected error fetching depth chart: {e}"}

@_dummy.tool
async def crawl_url(url: str, max_length: Optional[int] = 10000) -> dict:
    """Extract clean text content from any web page for analysis.
    
    Use this tool when users provide URLs and want the text content analyzed or summarized.
    
    Parameters:
        url (str, required): Full URL starting with http:// or https://
        max_length (int, optional): Maximum text length to return (100-50000, default: 10000)
    
    Returns: {url, title, content, content_length, success, error}
    
    Call this for: "analyze this URL", "summarize this page", "what does this website say"
    """
    try:
        url = validate_string_input(url, 'general', max_length=2000, required=True)
    except ValueError as e:
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": f"Invalid URL: {e}"}
    if not validate_url_enhanced(url):
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": "URL validation failed - potentially unsafe URL"}
    try:
        max_length = validate_numeric_input(max_length, min_val=100, max_val=50000, default=10000, required=False)
    except ValueError:
        max_length = 10000
    try:
        headers = get_http_headers("web_crawler")
        async with create_http_client() as client:
            response = await client.get(url, headers=headers); response.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'lxml')
            title_tag = soup.find('title'); title = sanitize_content(title_tag.get_text().strip()) if title_tag else None
            for tag in soup(["script","style","nav","footer","aside","form"]): tag.extract()
            text = sanitize_content(soup.get_text(), max_length=max_length)
            return {"url": url, "title": title, "content": text, "content_length": len(text), "success": True, "error": None}
    except httpx.TimeoutException:
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": "Request timed out"}
    except httpx.HTTPStatusError as e:
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"}
    except Exception as e:
        return {"url": url, "title": None, "content": "", "content_length": 0, "success": False, "error": f"Unexpected error: {e}"}

@_dummy.tool
@timing_decorator("fetch_athletes", tool_type="athlete")
async def fetch_athletes() -> dict:
    """Download and cache all NFL player data from Sleeper API into local database.
    
    Use this tool to update the local player database before searching or looking up athletes.
    WARNING: This downloads a large dataset (tens of MB) - use sparingly.
    
    Parameters: None
    
    Returns: {athletes_count, last_updated, success, error}
    
    Call this for: "update player database", "refresh athlete data", "download latest players"
    """
    try:
        timeout = httpx.Timeout(60.0, connect=15.0)
        headers = {"User-Agent": "NFL-MCP-Server/0.1.0 (NFL Athletes Fetcher)"}
        url = "https://api.sleeper.app/v1/players/nfl"
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.get(url, follow_redirects=True); response.raise_for_status(); data = response.json()
        count = _nfl_db.upsert_athletes(data)  # type: ignore[union-attr]
        last_updated = _nfl_db.get_last_updated()  # type: ignore[union-attr]
        return {"athletes_count": count, "last_updated": last_updated, "success": True, "error": None}
    except Exception as e:
        return {"athletes_count": 0, "last_updated": None, "success": False, "error": str(e)}

@_dummy.tool
def lookup_athlete(athlete_id: str) -> dict:
    """Find a specific NFL player by their unique Sleeper ID.
    
    Use this tool when you have a specific player ID and need detailed player information.
    
    Parameters:
        athlete_id (str, required): Player's Sleeper API identifier (numeric string)
    
    Returns: {athlete, found (bool), error}
    
    Call this for: "player ID 1234", "lookup athlete 5678", when you have a specific player ID
    """
    try:
        athlete_id = validate_string_input(athlete_id, 'alphanumeric_id', max_length=50, required=True)
    except ValueError as e:
        return {"athlete": None, "found": False, "error": f"Invalid athlete_id: {e}"}
    try:
        athlete = _nfl_db.get_athlete_by_id(athlete_id)  # type: ignore[union-attr]
        return {"athlete": athlete, "found": bool(athlete), "error": None if athlete else f"Athlete with ID '{athlete_id}' not found"}
    except Exception as e:
        return {"athlete": None, "found": False, "error": f"Error looking up athlete: {e}"}

@_dummy.tool
def search_athletes(name: str, limit: Optional[int] = 10) -> dict:
    """Search for NFL players by name (supports partial matching).
    
    Use this tool when users mention player names or want to find players by name.
    
    Parameters:
        name (str, required): Player name or partial name to search for
        limit (int, optional): Maximum results to return (1-100, default: 10)
    
    Returns: {athletes: [...], count, search_term, error}
    
    Call this for: "find Mahomes", "search for Josh Allen", "players named Williams"
    """
    try:
        name = validate_string_input(name, 'athlete_name', max_length=100, required=True)
    except ValueError as e:
        return {"athletes": [], "count": 0, "search_term": name, "error": f"Invalid name: {e}"}
    limit = validate_limit(limit, LIMITS["athletes_search_min"], LIMITS["athletes_search_max"], LIMITS["athletes_search_default"])
    try:
        athletes = _nfl_db.search_athletes_by_name(name, limit)  # type: ignore[union-attr]
        return {"athletes": athletes, "count": len(athletes), "search_term": name, "error": None}
    except Exception as e:
        return {"athletes": [], "count": 0, "search_term": name, "error": f"Error searching athletes: {e}"}

@_dummy.tool
def get_athletes_by_team(team_id: str) -> dict:
    """Get all players currently on a specific NFL team's roster.
    
    Use this tool when users ask about a team's players, roster, or who plays for a team.
    
    Parameters:
        team_id (str, required): Team abbreviation (e.g., 'KC', 'SF', 'NE', 'DAL')
    
    Returns: {athletes: [...], count, team_id, error}
    
    Call this for: "Chiefs roster", "who plays for KC", "49ers players", "team roster"
    """
    try:
        team_id = validate_string_input(team_id, 'team_id', max_length=4, required=True)
    except ValueError as e:
        return {"athletes": [], "count": 0, "team_id": team_id, "error": f"Invalid team_id: {e}"}
    try:
        athletes = _nfl_db.get_athletes_by_team(team_id)  # type: ignore[union-attr]
        return {"athletes": athletes, "count": len(athletes), "team_id": team_id, "error": None}
    except Exception as e:
        return {"athletes": [], "count": 0, "team_id": team_id, "error": f"Error getting athletes for team: {e}"}

# Sleeper Fantasy League Tools
@_dummy.tool
async def get_league(league_id: str) -> dict:
    """Get detailed information about a Sleeper fantasy football league.
    
    Use this tool when users provide a league ID and want league settings, scoring, roster positions.
    
    Parameters:
        league_id (str, required): Sleeper league ID (18-digit numeric string)
    
    Returns: {league, success, error}
    
    Call this for: "my league info", "league settings", when users provide a league ID
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_league(league_id)
    except ValueError as e:
        return {"league": None, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_rosters(league_id: str) -> dict:
    """Get all team rosters in a Sleeper fantasy league.
    
    Use this tool to see team compositions, player ownership, and roster construction.
    
    Parameters:
        league_id (str, required): Sleeper league ID (18-digit numeric string)
    
    Returns: {rosters: [...], count, success, error}
    
    Call this for: "league rosters", "who has which players", "team rosters"
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_rosters(league_id)
    except ValueError as e:
        return {"rosters": [], "count": 0, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_league_users(league_id: str) -> dict:
    """Get all managers/users in a Sleeper fantasy league.
    
    Use this tool to see league membership, manager names, and user information.
    
    Parameters:
        league_id (str, required): Sleeper league ID (18-digit numeric string)
    
    Returns: {users: [...], count, success, error}
    
    Call this for: "league members", "who's in my league", "league managers"
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_league_users(league_id)
    except ValueError as e:
        return {"users": [], "count": 0, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_matchups(league_id: str, week: int) -> dict:
    """Get fantasy matchups for a specific week in a Sleeper league.
    
    Use this tool to see weekly head-to-head matchups, scores, and results.
    
    Parameters:
        league_id (str, required): Sleeper league ID (18-digit numeric string)
        week (int, required): NFL week number (1-22)
    
    Returns: {matchups: [...], week, count, success, error}
    
    Call this for: "week 5 matchups", "this week's games", "my matchup week 3"
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        week = validate_numeric_input(week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        return await sleeper_tools.get_matchups(league_id, week)
    except ValueError as e:
        return {"matchups": [], "week": week, "count": 0, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def get_playoff_bracket(league_id: str) -> dict:
    """Get playoff bracket structure and results for a Sleeper fantasy league.
    
    Use this tool during fantasy playoffs to see bracket, matchups, and advancement.
    
    Parameters:
        league_id (str, required): Sleeper league ID (18-digit numeric string)
    
    Returns: {playoff_bracket, success, error}
    
    Call this for: "playoff bracket", "fantasy playoffs", "championship bracket"
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_playoff_bracket(league_id)
    except ValueError as e:
        return {"playoff_bracket": None, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_transactions(league_id: str, round: Optional[int] = None) -> dict:
    """Get all transactions (trades, adds, drops) in a Sleeper fantasy league.
    
    Use this tool to see league activity, recent moves, and transaction history.
    
    Parameters:
        league_id (str, required): Sleeper league ID (18-digit numeric string)
        round (int, optional): Filter by specific draft round (1-18)
    
    Returns: {transactions: [...], round, count, success, error}
    
    Call this for: "recent transactions", "league moves", "who got dropped/added"
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
        return await sleeper_tools.get_transactions(league_id, round)
    except ValueError as e:
        return {"transactions": [], "round": round, "count": 0, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def get_traded_picks(league_id: str) -> dict:
    """Get all traded draft picks in a Sleeper dynasty fantasy league.
    
    Use this tool for dynasty leagues to see which draft picks have been traded.
    
    Parameters:
        league_id (str, required): Sleeper league ID (18-digit numeric string)
    
    Returns: {traded_picks: [...], count, success, error}
    
    Call this for: "traded picks", "draft pick trades", "who owns which picks"
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_traded_picks(league_id)
    except ValueError as e:
        return {"traded_picks": [], "count": 0, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_nfl_state() -> dict:
    """Get current NFL season information (week, season type, year).
    
    Use this tool to check what week/season it is according to Sleeper's data.
    
    Parameters: None
    
    Returns: {nfl_state: {week, season, season_type, ...}, success, error}
    
    Call this for: "what week is it", "current NFL week", "season info"
    """
    return await sleeper_tools.get_nfl_state()

@_dummy.tool
async def get_trending_players(trend_type: str = "add", lookback_hours: Optional[int] = 24, limit: Optional[int] = 25) -> dict:
    """Get trending players being added or dropped in Sleeper fantasy leagues.
    
    Use this tool to see which players are popular on the waiver wire or being dropped.
    
    Parameters:
        trend_type (str, optional): "add" for popular adds, "drop" for popular drops (default: "add")
        lookback_hours (int, optional): Hours to look back for trends (1-168, default: 24)
        limit (int, optional): Maximum players to return (1-100, default: 25)
    
    Returns: {trending_players: [...], trend_type, lookback_hours, count, success, error}
    
    Call this for: "trending players", "waiver wire targets", "popular adds", "who's being dropped"
    """
    try:
        trend_type = validate_string_input(trend_type, 'trend_type', max_length=10, required=True)
        lookback_hours = validate_numeric_input(lookback_hours, min_val=LIMITS["trending_lookback_min"], max_val=LIMITS["trending_lookback_max"], default=24, required=False)
        limit = validate_numeric_input(limit, min_val=LIMITS["trending_limit_min"], max_val=LIMITS["trending_limit_max"], default=25, required=False)
        return await sleeper_tools.get_trending_players(trend_type, lookback_hours, limit)
    except ValueError as e:
        return {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0, "success": False, "error": f"Invalid input: {e}"}
