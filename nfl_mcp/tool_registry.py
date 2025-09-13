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
from . import nfl_tools, sleeper_tools, waiver_history
from .config import (
    validate_string_input, validate_limit, LIMITS, validate_numeric_input, 
    validate_url_enhanced, get_http_headers, create_http_client, sanitize_content
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
    global _nfl_db, _waiver_store
    _nfl_db = db
    _waiver_store = waiver_history.get_waiver_store(db)

@_dummy.tool
@timing_decorator("get_nfl_news", tool_type="nfl")
async def get_nfl_news(limit: Optional[int] = 50) -> dict:
    return await nfl_tools.get_nfl_news(limit)

@_dummy.tool
@timing_decorator("get_league_leaders", tool_type="nfl")
async def get_league_leaders(category: str, season: Optional[int] = 2025, season_type: Optional[int] = 2) -> dict:
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
    return await nfl_tools.get_league_leaders(category=category, season=season or 2025, season_type=season_type or 2)

@_dummy.tool
@timing_decorator("get_teams", tool_type="nfl")
async def get_teams() -> dict:
    return await nfl_tools.get_teams()

@_dummy.tool
@timing_decorator("fetch_teams", tool_type="nfl")
async def fetch_teams() -> dict:
    return await nfl_tools.fetch_teams(_nfl_db)  # type: ignore[arg-type]

@_dummy.tool
async def get_depth_chart(team_id: str) -> dict:
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
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_league(league_id)
    except ValueError as e:
        return {"league": None, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_rosters(league_id: str) -> dict:
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_rosters(league_id)
    except ValueError as e:
        return {"rosters": [], "count": 0, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_league_users(league_id: str) -> dict:
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_league_users(league_id)
    except ValueError as e:
        return {"users": [], "count": 0, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_matchups(league_id: str, week: int) -> dict:
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        week = validate_numeric_input(week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        return await sleeper_tools.get_matchups(league_id, week)
    except ValueError as e:
        return {"matchups": [], "week": week, "count": 0, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def get_playoff_bracket(league_id: str) -> dict:
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_playoff_bracket(league_id)
    except ValueError as e:
        return {"playoff_bracket": None, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_transactions(league_id: str, round: Optional[int] = None) -> dict:
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
        return await sleeper_tools.get_transactions(league_id, round)
    except ValueError as e:
        return {"transactions": [], "round": round, "count": 0, "success": False, "error": f"Invalid input: {e}"}

@_dummy.tool
async def get_traded_picks(league_id: str) -> dict:
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_traded_picks(league_id)
    except ValueError as e:
        return {"traded_picks": [], "count": 0, "success": False, "error": f"Invalid league_id: {e}"}

@_dummy.tool
async def get_nfl_state() -> dict:
    return await sleeper_tools.get_nfl_state()

@_dummy.tool
async def get_trending_players(trend_type: str = "add", lookback_hours: Optional[int] = 24, limit: Optional[int] = 25) -> dict:
    try:
        trend_type = validate_string_input(trend_type, 'trend_type', max_length=10, required=True)
        lookback_hours = validate_numeric_input(lookback_hours, min_val=LIMITS["trending_lookback_min"], max_val=LIMITS["trending_lookback_max"], default=24, required=False)
        limit = validate_numeric_input(limit, min_val=LIMITS["trending_limit_min"], max_val=LIMITS["trending_limit_max"], default=25, required=False)
        return await sleeper_tools.get_trending_players(trend_type, lookback_hours, limit)
    except ValueError as e:
        return {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0, "success": False, "error": f"Invalid input: {e}"}

# Waiver history tools
@_dummy.tool(description="Record a waiver recommendation with duplicate suppression")
def record_waiver_recommendation(player_id: str, league_id: str, recommendation_type: str, rationale: str, context_json: Optional[str] = None, ttl_days: Optional[int] = 7, proj_points_delta_pct: Optional[float] = 20.0, snap_pct_delta_abs: Optional[float] = 10.0) -> dict:
    try:
        player_id_valid = validate_string_input(player_id, 'alphanumeric_id', max_length=50, required=True)
        league_id_valid = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        recommendation_type_valid = validate_string_input(recommendation_type, 'general', max_length=30, required=True)
        rationale_valid = validate_string_input(rationale, 'general', max_length=5000, required=True)
    except ValueError as e:
        return {"success": False, "error": f"Validation error: {e}"}
    ctx = {}
    if context_json:
        try: ctx = json.loads(context_json)
        except Exception: ctx = {}
    rec = waiver_history.WaiverRecommendation(player_id=player_id_valid, league_id=league_id_valid, recommendation_type=recommendation_type_valid, rationale_full=rationale_valid, context=ctx)
    thresholds = {'proj_points_delta_pct': float(proj_points_delta_pct) if proj_points_delta_pct is not None else 20.0, 'snap_pct_delta_abs': float(snap_pct_delta_abs) if snap_pct_delta_abs is not None else 10.0}
    result = _waiver_store.record(rec, ttl_days=ttl_days or 7, thresholds=thresholds)  # type: ignore[union-attr]
    result.update({'player_id': player_id_valid, 'league_id': league_id_valid, 'recommendation_type': recommendation_type_valid})
    return result

@_dummy.tool(description="Check recent waiver recommendation status for a player")
def check_waiver_recommendation(player_id: str, league_id: str, recommendation_type: Optional[str] = None, ttl_days: Optional[int] = 7) -> dict:
    try:
        player_id_valid = validate_string_input(player_id, 'alphanumeric_id', max_length=50, required=True)
        league_id_valid = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        recommendation_type_valid = validate_string_input(recommendation_type, 'general', max_length=30, required=True) if recommendation_type else None
    except ValueError as e:
        return {"success": False, "error": f"Validation error: {e}"}
    status = _waiver_store.check_player(player_id_valid, league_id_valid, recommendation_type_valid, ttl_days or 7)  # type: ignore[union-attr]
    status.update({'player_id': player_id_valid, 'league_id': league_id_valid})
    return status

@_dummy.tool(description="Get recent waiver recommendations for a league")
def get_recent_waiver_recommendations(league_id: str, limit: Optional[int] = 25, ttl_days: Optional[int] = 7) -> dict:
    try:
        league_id_valid = validate_string_input(league_id, 'league_id', max_length=20, required=True)
    except ValueError as e:
        return {"success": False, "error": f"Validation error: {e}"}
    limit = min(max(int(limit or 25), 1), 100)
    rows = _waiver_store.get_recent(league_id_valid, limit=limit, ttl_days=ttl_days or 7)  # type: ignore[union-attr]
    return {"league_id": league_id_valid, "count": len(rows), "recommendations": rows}

@_dummy.tool(description="Purge old waiver recommendations (admin)")
def purge_waiver_history(ttl_days: Optional[int] = 30, max_rows: Optional[int] = 10000) -> dict:
    try:
        ttl = int(ttl_days or 30); mr = int(max_rows or 10000)
    except ValueError:
        ttl, mr = 30, 10000
    result = _waiver_store.purge_old(ttl_days=ttl, max_rows=mr)  # type: ignore[union-attr]
    result.update({'ttl_days': ttl, 'max_rows': mr})
    return result
