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
from . import nfl_tools, sleeper_tools, waiver_tools
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

def initialize_shared(db: NFLDatabase):
    global _nfl_db
    _nfl_db = db

@_dummy.tool
@timing_decorator("get_nfl_news", tool_type="nfl")
async def get_nfl_news(limit: Optional[int] = 50) -> dict:
    """Fetch latest NFL news headlines from ESPN.

    Parameters:
        limit (int, default 50, range 1-50): Max number of articles.
    Returns: {articles: [...], total_articles, success, error?}
    Example: get_nfl_news(limit=10)
    """
    return await nfl_tools.get_nfl_news(limit)

if FEATURE_LEAGUE_LEADERS:
    @_dummy.tool
    @timing_decorator("get_league_leaders", tool_type="nfl")
    async def get_league_leaders(category: str, season: Optional[int] = 2025, season_type: Optional[int] = 2, week: Optional[int] = None) -> dict:  # pragma: no cover - conditional
        """Get NFL stat leaders for one or multiple categories (feature-flagged).

        Categories tokens (comma/space separated): pass, rush, receiving, tackles, sacks.
        Parameters:
            category (str, required): One or many tokens (e.g. "pass, rush").
            season (int, default 2025): Season year.
            season_type (int, default 2): 1=Pre,2=Regular,3=Post.
            week (int|None, optional): Reserved for future filtering (currently ignored if provided).
        Returns: Single category => players list; Multi => categories array.
        Example: get_league_leaders(category="pass, rush", season=2025)
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
    """List current NFL teams (ESPN) with metadata.

    Returns: {teams: [...], total_teams, success, error?}
    Example: get_teams()
    """
    return await nfl_tools.get_teams()

@_dummy.tool
@timing_decorator("fetch_teams", tool_type="nfl")
async def fetch_teams() -> dict:
    """Fetch & upsert NFL teams into local DB.

    Returns: {teams_count, last_updated, success, error?}
    Example: fetch_teams()
    """
    return await nfl_tools.fetch_teams(_nfl_db)  # type: ignore[arg-type]

@_dummy.tool
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
async def get_team_injuries(team_id: str, limit: Optional[int] = 50) -> dict:
    """Team injury report (ESPN Core API).

    Parameters:
        team_id (str): Team abbreviation/id.
        limit (int, default 50, range 1-100)
    Returns: {team_id, team_name, injuries:[...], count, success, error?}
    Example: get_team_injuries(team_id="KC", limit=25)
    """
    return await nfl_tools.get_team_injuries(team_id=team_id, limit=limit)

@_dummy.tool
async def get_team_player_stats(team_id: str, season: Optional[int] = 2025, season_type: Optional[int] = 2, limit: Optional[int] = 50) -> dict:
    """Team player summary stats.

    Parameters:
        team_id (str)
        season (int, default 2025)
        season_type (int, default 2)
        limit (int, default 50)
    Returns: {team_id, season, season_type, player_stats:[...], count}
    Example: get_team_player_stats(team_id="KC", season=2025)
    """
    return await nfl_tools.get_team_player_stats(team_id=team_id, season=season, season_type=season_type, limit=limit)

@_dummy.tool
async def get_nfl_standings(season: Optional[int] = 2025, season_type: Optional[int] = 2, group: Optional[int] = None) -> dict:
    """NFL standings (league or conference).

    Parameters:
        season (int, default 2025)
        season_type (int, default 2)
        group (int|None, optional): 1=AFC,2=NFC
    Returns: {standings:[...], count, season, season_type, group}
    Example: get_nfl_standings(season=2025, group=1)
    """
    return await nfl_tools.get_nfl_standings(season=season, season_type=season_type, group=group)

@_dummy.tool
async def get_team_schedule(team_id: str, season: Optional[int] = 2025) -> dict:
    """Team schedule with matchup context.

    Parameters:
        team_id (str)
        season (int, default 2025)
    Returns: {team_id, season, schedule:[...], count}
    Example: get_team_schedule(team_id="KC", season=2025)
    """
    return await nfl_tools.get_team_schedule(team_id=team_id, season=season)

@_dummy.tool
async def get_team_injuries(team_id: str, limit: Optional[int] = 50) -> dict:
    """Get current injury report for an NFL team from ESPN Core API.

    Parameters:
        team_id (str, required): Team abbreviation (e.g. 'KC','NE','DAL').
        limit (int, default 50, range 1-100): Max number of injuries to return.
    Returns: {team_id, team_name, injuries:[{player_name, position, status, severity}], count, success, error?}
    Example: get_team_injuries(team_id="KC", limit=20)
    """
    return await nfl_tools.get_team_injuries(team_id, limit)

@_dummy.tool
async def get_team_player_stats(team_id: str, season: Optional[int] = 2025, season_type: Optional[int] = 2, limit: Optional[int] = 50) -> dict:
    """Get player statistics for an NFL team from ESPN Core API.

    Parameters:
        team_id (str, required): Team abbreviation (e.g. 'KC','NE','DAL').
        season (int, default 2025): Season year.
        season_type (int, default 2): 1=Pre, 2=Regular, 3=Post, 4=Off.
        limit (int, default 50, range 1-100): Max number of players to return.
    Returns: {team_id, team_name, season, season_type, player_stats:[{player_name, position, fantasy_relevant}], count, success, error?}
    Example: get_team_player_stats(team_id="KC", season=2025, limit=25)
    """
    return await nfl_tools.get_team_player_stats(team_id, season, season_type, limit)

@_dummy.tool
async def get_nfl_standings(season: Optional[int] = 2025, season_type: Optional[int] = 2, group: Optional[int] = None) -> dict:
    """Get current NFL standings from ESPN Core API with fantasy context.

    Parameters:
        season (int, default 2025): Season year.
        season_type (int, default 2): 1=Pre, 2=Regular, 3=Post, 4=Off.
        group (int, optional): Conference group (1=AFC, 2=NFC, None=both).
    Returns: {standings:[{team_name, wins, losses, fantasy_context, motivation_level}], season, season_type, count, success, error?}
    Example: get_nfl_standings(season=2025, season_type=2)
    """
    return await nfl_tools.get_nfl_standings(season, season_type, group)

@_dummy.tool
async def get_team_schedule(team_id: str, season: Optional[int] = 2025) -> dict:
    """Get team schedule from ESPN Site API with fantasy implications.

    Parameters:
        team_id (str, required): Team abbreviation (e.g. 'KC','NE','DAL').
        season (int, default 2025): Season year.
    Returns: {team_id, team_name, season, schedule:[{date, week, opponent, is_home, fantasy_implications}], count, success, error?}
    Example: get_team_schedule(team_id="KC", season=2025)
    """
    return await nfl_tools.get_team_schedule(team_id, season)

@_dummy.tool
async def crawl_url(url: str, max_length: Optional[int] = 10000) -> dict:
    """Retrieve and sanitize page text content.

    Parameters:
        url (str, required): HTTP/HTTPS URL.
        max_length (int, default 10000, range 100-50000): Truncate content.
    Returns: {url, title, content, content_length, success, error?}
    Example: crawl_url(url="https://example.com", max_length=4000)
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
    """Bulk import Sleeper NFL player dataset into local DB.

    Returns: {athletes_count, last_updated, success, error?}
    Warning: Large payload (tens of MB) – use sparingly.
    Example: fetch_athletes()
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
    """Lookup a single athlete by ID (Sleeper player id key).

    Parameters:
        athlete_id (str, required): Player identifier.
    Returns: {athlete, found (bool), error?}
    Example: lookup_athlete(athlete_id="1234")
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
    """Search athletes by (case-insensitive) name substring.

    Parameters:
        name (str, required): Partial or full name.
        limit (int, default 10, range 1-100): Max results.
    Returns: {athletes: [...], count, search_term, error?}
    Example: search_athletes(name="Mahomes", limit=5)
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
    """List athletes (local DB) for a team abbreviation.

    Parameters:
        team_id (str, required): Abbrev (e.g. 'KC').
    Returns: {athletes: [...], count, team_id, error?}
    Example: get_athletes_by_team(team_id="KC")
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

# Sleeper tools
@_dummy.tool
async def get_league(league_id: str) -> dict:
    """Fetch Sleeper league metadata.

    Parameters:
        league_id (str, required)
    Returns: {league, success, error?}
    Example: get_league(league_id="123456789012345678")
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_league(league_id)
    except ValueError as e:
        return {"league": None, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_rosters(league_id: str) -> dict:
    """Fetch rosters for a Sleeper league.

    Parameters:
        league_id (str, required)
    Returns: {rosters:[...], count, success, error?}
    Example: get_rosters(league_id="123456789012345678")
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_rosters(league_id)
    except ValueError as e:
        return {"rosters": [], "count": 0, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_league_users(league_id: str) -> dict:
    """List users in a Sleeper league.

    Parameters:
        league_id (str, required)
    Returns: {users:[...], count, success, error?}
    Example: get_league_users(league_id="123456789012345678")
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_league_users(league_id)
    except ValueError as e:
        return {"users": [], "count": 0, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_matchups(league_id: str, week: int) -> dict:
    """Fetch matchups for a given week.

    Parameters:
        league_id (str, required)
        week (int, required, range config week_min-week_max)
    Returns: {matchups:[...], week, count, success, error?}
    Example: get_matchups(league_id="123456789012345678", week=3)
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        week = validate_numeric_input(week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        return await sleeper_tools.get_matchups(league_id, week)
    except ValueError as e:
        return {"matchups": [], "week": week, "count": 0, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def get_playoff_bracket(league_id: str) -> dict:
    """Fetch playoff bracket for a league (if available)."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_playoff_bracket(league_id)
    except ValueError as e:
        return {"playoff_bracket": None, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_transactions(league_id: str, round: Optional[int] = None) -> dict:
    """List transactions for a league (optionally filter by round).

    Parameters:
        league_id (str, required)
        round (int|None, optional): Draft round filter.
    Returns: {transactions:[...], round, count, success, error?}
    Example: get_transactions(league_id="123", round=2)
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
    """List traded picks for a league."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_traded_picks(league_id)
    except ValueError as e:
        return {"traded_picks": [], "count": 0, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_nfl_state() -> dict:
    """Return current Sleeper NFL state (week, season type, etc)."""
    return await sleeper_tools.get_nfl_state()

@_dummy.tool
async def get_waiver_log(league_id: str, round: Optional[int] = None, dedupe: bool = True) -> dict:
    """Get waiver wire log with de-duplication analysis.

    Parameters:
        league_id (str, required): League identifier
        round (int|None, optional): Round filter
        dedupe (bool, optional): Enable de-duplication (default: True)
    Returns: {waiver_log:[...], duplicates_found:[...], total_transactions, unique_transactions, success, error?}
    Example: get_waiver_log(league_id="123456", dedupe=True)
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
        return await waiver_tools.get_waiver_log(league_id, round, dedupe)
    except ValueError as e:
        return {"waiver_log": [], "duplicates_found": [], "total_transactions": 0, "unique_transactions": 0, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def check_re_entry_status(league_id: str, round: Optional[int] = None) -> dict:
    """Check re-entry status for players (dropped then re-added).

    Parameters:
        league_id (str, required): League identifier
        round (int|None, optional): Round filter
    Returns: {re_entry_players:{...}, volatile_players:[...], total_players_analyzed, players_with_re_entries, success, error?}
    Example: check_re_entry_status(league_id="123456")
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
        return await waiver_tools.check_re_entry_status(league_id, round)
    except ValueError as e:
        return {"re_entry_players": {}, "volatile_players": [], "total_players_analyzed": 0, "players_with_re_entries": 0, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def get_waiver_wire_dashboard(league_id: str, round: Optional[int] = None) -> dict:
    """Get comprehensive waiver wire dashboard with analysis.

    Parameters:
        league_id (str, required): League identifier  
        round (int|None, optional): Round filter
    Returns: {waiver_log:[...], re_entry_analysis:{...}, dashboard_summary:{...}, volatile_players:[...], success, error?}
    Example: get_waiver_wire_dashboard(league_id="123456")
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
        return await waiver_tools.get_waiver_wire_dashboard(league_id, round)
    except ValueError as e:
        return {"waiver_log": [], "re_entry_analysis": {}, "dashboard_summary": {}, "volatile_players": [], "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def get_trending_players(trend_type: str = "add", lookback_hours: Optional[int] = 24, limit: Optional[int] = 25) -> dict:
    """Fetch trending add/drop players.

    Parameters:
        trend_type (str, default 'add'): 'add' or 'drop'.
        lookback_hours (int, default 24): Window size.
        limit (int, default 25): Max players.
    Returns: {trending_players:[...], count, success, error?}
    Example: get_trending_players(trend_type='drop', lookback_hours=48, limit=15)
    """
    try:
        trend_type = validate_string_input(trend_type, 'trend_type', max_length=10, required=True)
        lookback_hours = validate_numeric_input(lookback_hours, min_val=LIMITS["trending_lookback_min"], max_val=LIMITS["trending_lookback_max"], default=24, required=False)
        limit = validate_numeric_input(limit, min_val=LIMITS["trending_limit_min"], max_val=LIMITS["trending_limit_max"], default=25, required=False)
        return await sleeper_tools.get_trending_players(trend_type, lookback_hours, limit)
    except ValueError as e:
        return {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0, "success": False, "error": f"Invalid input: {e}"}

# Strategic Planning Functions

@_dummy.tool
async def get_strategic_matchup_preview(league_id: str, current_week: int, weeks_ahead: Optional[int] = 4) -> dict:
    """Strategic preview of upcoming matchups for multi-week planning.
    
    Combines Sleeper league data with NFL schedules for early warning about bye weeks, 
    tough matchups, and strategic opportunities. Essential for forward-looking fantasy management.
    
    Parameters:
        league_id (str, required): Sleeper league identifier.
        current_week (int, required): Current NFL week (1-22).
        weeks_ahead (int, default 4, range 1-8): Weeks to analyze ahead.
    Returns: {strategic_preview:{weeks, summary}, weeks_analyzed, league_id, success, error?}
    Example: get_strategic_matchup_preview(league_id="123456789012345678", current_week=8, weeks_ahead=6)
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        current_week = validate_numeric_input(current_week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        weeks_ahead = validate_numeric_input(weeks_ahead, min_val=1, max_val=8, default=4, required=False)
        return await sleeper_tools.get_strategic_matchup_preview(league_id, current_week, weeks_ahead)
    except ValueError as e:
        return {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool  
async def get_season_bye_week_coordination(league_id: str, season: Optional[int] = 2025) -> dict:
    """Season-long bye week coordination with fantasy league schedule.
    
    Analyzes entire NFL bye week calendar against your fantasy playoffs to identify
    optimal trading periods, waiver timing, and roster construction strategies.
    
    Parameters:
        league_id (str, required): Sleeper league identifier.
        season (int, default 2025): NFL season year.
    Returns: {coordination_plan:{bye_week_calendar, strategic_periods, recommendations}, season, league_id, success, error?}
    Example: get_season_bye_week_coordination(league_id="123456789012345678", season=2025)
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        season = validate_numeric_input(season, min_val=2020, max_val=2030, default=2025, required=False)
        return await sleeper_tools.get_season_bye_week_coordination(league_id, season)
    except ValueError as e:
        return {"coordination_plan": {}, "season": season, "league_id": league_id, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def get_trade_deadline_analysis(league_id: str, current_week: int) -> dict:
    """Strategic trade deadline timing analysis.
    
    Evaluates optimal trade timing by analyzing upcoming bye weeks, playoff schedules,
    and league patterns to maximize competitive advantage before deadline.
    
    Parameters:
        league_id (str, required): Sleeper league identifier.
        current_week (int, required): Current NFL week for timeline analysis.
    Returns: {trade_analysis:{timing_analysis, strategic_windows, recommendations}, league_id, current_week, success, error?}
    Example: get_trade_deadline_analysis(league_id="123456789012345678", current_week=11)
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        current_week = validate_numeric_input(current_week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        return await sleeper_tools.get_trade_deadline_analysis(league_id, current_week)
    except ValueError as e:
        return {"trade_analysis": {}, "league_id": league_id, "current_week": current_week, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def get_playoff_preparation_plan(league_id: str, current_week: int) -> dict:
    """Comprehensive playoff preparation plan combining league and NFL data.
    
    Analyzes playoff structure, NFL schedules, and provides detailed preparation plan
    including roster optimization, matchup analysis, and strategic timing recommendations.
    
    Parameters:
        league_id (str, required): Sleeper league identifier.
        current_week (int, required): Current NFL week for timeline analysis.
    Returns: {playoff_plan:{timeline, strategic_priorities, recommendations}, readiness_score, league_id, success, error?}
    Example: get_playoff_preparation_plan(league_id="123456789012345678", current_week=12)
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        current_week = validate_numeric_input(current_week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        return await sleeper_tools.get_playoff_preparation_plan(league_id, current_week)
    except ValueError as e:
        return {"playoff_plan": {}, "league_id": league_id, "readiness_score": 0, "success": False, "error": f"Invalid input: {e}"}

    return result
