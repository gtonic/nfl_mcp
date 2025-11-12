"""Simplified tool registry for NFL MCP Server.

This module contains all MCP tool definitions in a clean, maintainable way.
Tools are defined as regular functions and registered with the FastMCP server.
"""
from __future__ import annotations
from typing import Optional, List, Callable, Any
import json
import httpx
from .metrics import timing_decorator
from . import nfl_tools, sleeper_tools, waiver_tools, web_tools, athlete_tools, trade_analyzer_tools, cbs_fantasy_tools, opponent_analysis_tools
from .config import FEATURE_LEAGUE_LEADERS, validate_string_input, validate_limit, validate_numeric_input, LIMITS
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
        
        # CBS Fantasy Tools
        get_cbs_player_news,
        get_cbs_projections,
        get_cbs_expert_picks,
        
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
    get_fantasy_context,
        
        # Sleeper API Tools - Strategic Planning (New from main)
        get_strategic_matchup_preview,
        get_season_bye_week_coordination,
        get_trade_deadline_analysis,
        get_playoff_preparation_plan,

    # Sleeper Additional Core Endpoints
    get_user,
    get_user_leagues,
    get_league_drafts,
    get_draft,
    get_draft_picks,
    get_draft_traded_picks,
    fetch_all_players,
        
        # Waiver Wire Analysis Tools (New from main)
        get_waiver_log,
        check_re_entry_status,
        get_waiver_wire_dashboard,
        
        # Trade Analyzer Tools
        analyze_trade,
        
        # Opponent Analysis Tools
        analyze_opponent,
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
    return await nfl_tools.get_depth_chart(team_id)


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
# CBS FANTASY TOOLS
# =============================================================================

@timing_decorator("get_cbs_player_news", tool_type="cbs_fantasy")
async def get_cbs_player_news(limit: Optional[int] = 50) -> dict:
    """Fetch latest fantasy football player news from CBS Sports.

    Parameters:
        limit (int, default 50, range 1-100): Max number of news items.
    Returns: {news: [...], total_news, success, error?}
    Example: get_cbs_player_news(limit=25)
    """
    return await cbs_fantasy_tools.get_cbs_player_news(limit)


@timing_decorator("get_cbs_projections", tool_type="cbs_fantasy")
async def get_cbs_projections(
    position: str = "QB",
    week: Optional[int] = None,
    season: Optional[int] = 2025,
    scoring: str = "ppr"
) -> dict:
    """Fetch fantasy football projections from CBS Sports for a specific position and week.

    Parameters:
        position (str, default "QB"): Player position (QB, RB, WR, TE, K, DST).
        week (int, required): NFL week number (1-18).
        season (int, default 2025): Season year.
        scoring (str, default "ppr"): Scoring format (ppr, half-ppr, standard).
    Returns: {projections: [...], total_projections, week, position, success, error?}
    Example: get_cbs_projections(position="RB", week=11, season=2025, scoring="ppr")
    """
    try:
        week_i = int(week) if week is not None else None
        season_i = int(season) if season is not None else 2025
    except Exception:
        week_i = None
        season_i = 2025
    return await cbs_fantasy_tools.get_cbs_projections(
        position=position,
        week=week_i,
        season=season_i,
        scoring=scoring
    )


@timing_decorator("get_cbs_expert_picks", tool_type="cbs_fantasy")
async def get_cbs_expert_picks(week: Optional[int] = None) -> dict:
    """Fetch NFL expert picks against the spread from CBS Sports for a specific week.

    Parameters:
        week (int, required): NFL week number (1-18).
    Returns: {picks: [...], total_picks, week, success, error?}
    Example: get_cbs_expert_picks(week=10)
    """
    try:
        week_i = int(week) if week is not None else None
    except Exception:
        week_i = None
    return await cbs_fantasy_tools.get_cbs_expert_picks(week=week_i)


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
    return await web_tools.crawl_url(url, max_length)


# =============================================================================
# ATHLETE TOOLS
# =============================================================================

@timing_decorator("fetch_athletes", tool_type="athlete")
async def fetch_athletes() -> dict:
    """Fetch all NFL players from Sleeper API and store in database.
    
    Returns: {athletes_count, last_updated, success, error?}
    Example: fetch_athletes()
    """
    return await athlete_tools.fetch_athletes(_nfl_db)


@timing_decorator("lookup_athlete", tool_type="athlete")
def lookup_athlete(athlete_id: str) -> dict:
    """Look up an athlete by their ID.
    
    Parameters:
        athlete_id (str, required): The unique identifier for the athlete.
    Returns: {athlete, found, error?}
    Example: lookup_athlete(athlete_id="4034")
    """
    return athlete_tools.lookup_athlete(_nfl_db, athlete_id)


@timing_decorator("search_athletes", tool_type="athlete")
def search_athletes(name: str, limit: Optional[int] = 10) -> dict:
    """Search for athletes by name (partial match supported).
    
    Parameters:
        name (str, required): Name or partial name to search for.
        limit (int, default 10, range 1-50): Maximum number of results.
    Returns: {athletes: [...], count, search_term, error?}
    Example: search_athletes(name="Mahomes", limit=5)
    """
    return athlete_tools.search_athletes(_nfl_db, name, limit)


@timing_decorator("get_athletes_by_team", tool_type="athlete")
def get_athletes_by_team(team_id: str) -> dict:
    """Get all athletes for a specific team.
    
    Parameters:
        team_id (str, required): The team identifier (e.g., "SF", "DAL", "NE").
    Returns: {athletes: [...], count, team_id, error?}
    Example: get_athletes_by_team(team_id="KC")
    """
    return athlete_tools.get_athletes_by_team(_nfl_db, team_id)


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
async def get_playoff_bracket(league_id: str, bracket_type: str = "winners") -> dict:
    """Get playoff bracket (winners or losers) with validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        bracket_type = validate_string_input(bracket_type, 'bracket_type', max_length=10, required=False)
        return await sleeper_tools.get_playoff_bracket(league_id, bracket_type)
    except ValueError as e:
        return {"playoff_bracket": None, "bracket_type": bracket_type, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_transactions", tool_type="sleeper")
async def get_transactions(league_id: str, week: Optional[int] = None, round: Optional[int] = None) -> dict:
    """Get league transactions for a specific week (round) with validation (week required)."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        # Accept either week or deprecated round
        effective_week = week if week is not None else round
        if effective_week is None:
            raise ValueError("week (or round) is required")
        effective_week = validate_numeric_input(effective_week, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=True)
        return await sleeper_tools.get_transactions(league_id, round=effective_week, week=effective_week)
    except ValueError as e:
        return {"transactions": [], "week": week, "count": 0, "success": False, "error": f"Invalid input: {str(e)}"}


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
    """Get trending players with validation (returns objects including counts and 'enriched')."""
    try:
        trend_type = validate_string_input(trend_type, 'trend_type', max_length=10, required=True)
        lookback_hours = validate_numeric_input(lookback_hours, min_val=LIMITS["trending_lookback_min"], max_val=LIMITS["trending_lookback_max"], default=24, required=False)
        limit = validate_numeric_input(limit, min_val=LIMITS["trending_limit_min"], max_val=LIMITS["trending_limit_max"], default=25, required=False)
        return await sleeper_tools.get_trending_players(_nfl_db, trend_type, lookback_hours, limit)
    except ValueError as e:
        return {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_fantasy_context", tool_type="sleeper")
async def get_fantasy_context(league_id: str, week: Optional[int] = None, include: Optional[str] = None) -> dict:
    """Aggregate core league context (league, rosters, users, matchups, transactions).

    Parameters:
        league_id (str, required)
        week (int, optional) - auto inferred if omitted
        include (str, optional) comma list subset
    Returns: {context:{...}, week, auto_week_inferred, success, error?}
    Example: get_fantasy_context(league_id="12345", include="league,rosters,matchups")
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if week is not None:
            week = validate_numeric_input(week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=False)
        return await sleeper_tools.get_fantasy_context(league_id, week, include)
    except ValueError as e:
        return {"context": {}, "league_id": league_id, "week": week, "success": False, "error": f"Invalid input: {str(e)}"}


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
# SLEEPER API TOOLS - ADDITIONAL CORE ENDPOINTS (Users, Drafts, Players)
# =============================================================================

@timing_decorator("get_user", tool_type="sleeper")
async def get_user(user_id_or_username: str) -> dict:
    """Fetch a Sleeper user by ID or username."""
    try:
        user_id_or_username = validate_string_input(user_id_or_username, 'user_id_or_username', max_length=40, required=True)
        return await sleeper_tools.get_user(user_id_or_username)
    except ValueError as e:
        return {"user": None, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_user_leagues", tool_type="sleeper")
async def get_user_leagues(user_id: str, season: int) -> dict:
    """Fetch all leagues for a user and season."""
    try:
        user_id = validate_string_input(user_id, 'user_id', max_length=40, required=True)
        season = validate_numeric_input(season, min_val=2017, max_val=2030, required=True)
        return await sleeper_tools.get_user_leagues(user_id, season)
    except ValueError as e:
        return {"leagues": [], "count": 0, "season": season, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_league_drafts", tool_type="sleeper")
async def get_league_drafts(league_id: str) -> dict:
    """Fetch all drafts for a league."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=40, required=True)
        return await sleeper_tools.get_league_drafts(league_id)
    except ValueError as e:
        return {"drafts": [], "count": 0, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_draft", tool_type="sleeper")
async def get_draft(draft_id: str) -> dict:
    """Fetch a specific draft."""
    try:
        draft_id = validate_string_input(draft_id, 'draft_id', max_length=40, required=True)
        return await sleeper_tools.get_draft(draft_id)
    except ValueError as e:
        return {"draft": None, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_draft_picks", tool_type="sleeper")
async def get_draft_picks(draft_id: str) -> dict:
    """Fetch all picks in a draft."""
    try:
        draft_id = validate_string_input(draft_id, 'draft_id', max_length=40, required=True)
        return await sleeper_tools.get_draft_picks(draft_id)
    except ValueError as e:
        return {"picks": [], "count": 0, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("get_draft_traded_picks", tool_type="sleeper")
async def get_draft_traded_picks(draft_id: str) -> dict:
    """Fetch traded picks in a draft."""
    try:
        draft_id = validate_string_input(draft_id, 'draft_id', max_length=40, required=True)
        return await sleeper_tools.get_draft_traded_picks(draft_id)
    except ValueError as e:
        return {"traded_picks": [], "count": 0, "success": False, "error": f"Invalid input: {str(e)}"}


@timing_decorator("fetch_all_players", tool_type="sleeper")
async def fetch_all_players(force_refresh: bool = False) -> dict:
    """Fetch full Sleeper players map (cached). Returns counts only to minimize payload."""
    try:
        return await sleeper_tools.fetch_all_players(force_refresh)
    except ValueError as e:
        return {"players": {}, "cached": False, "success": False, "error": f"Invalid input: {str(e)}"}


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
# TRADE ANALYZER TOOLS
# =============================================================================

@timing_decorator("analyze_trade", tool_type="trade")
async def analyze_trade(
    league_id: str,
    team1_roster_id: int,
    team2_roster_id: int,
    team1_gives: list[str],
    team2_gives: list[str],
    include_trending: bool = True
) -> dict:
    """Analyze a fantasy football trade for fairness and fit.
    
    This tool evaluates proposed trades between two teams by calculating player
    values, assessing positional needs, and providing fairness scores with
    actionable recommendations.
    
    Parameters:
        league_id (str, required): The unique identifier for the fantasy league.
        team1_roster_id (int, required): Roster ID for team 1.
        team2_roster_id (int, required): Roster ID for team 2.
        team1_gives (list[str], required): List of player IDs team 1 is giving up.
        team2_gives (list[str], required): List of player IDs team 2 is giving up.
        include_trending (bool, default True): Include trending player data in analysis.
    
    Returns: {
        recommendation: str (fair, needs_adjustment, unfair, etc.),
        fairness_score: float (0-100, higher = more fair),
        team1_analysis: {...},
        team2_analysis: {...},
        trade_details: {...},
        warnings: [...],
        success: bool,
        error?: str
    }
    
    Example: analyze_trade(
        league_id="12345",
        team1_roster_id=1,
        team2_roster_id=2,
        team1_gives=["4034", "4035"],
        team2_gives=["4036"]
    )
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        team1_roster_id = validate_numeric_input(team1_roster_id, min_val=1, max_val=20, required=True)
        team2_roster_id = validate_numeric_input(team2_roster_id, min_val=1, max_val=20, required=True)
        
        if not isinstance(team1_gives, list) or not isinstance(team2_gives, list):
            raise ValueError("team1_gives and team2_gives must be lists of player IDs")
        
        if not team1_gives or not team2_gives:
            raise ValueError("team1_gives and team2_gives must not be empty")
        
        return await trade_analyzer_tools.analyze_trade(
            league_id=league_id,
            team1_roster_id=team1_roster_id,
            team2_roster_id=team2_roster_id,
            team1_gives=team1_gives,
            team2_gives=team2_gives,
            nfl_db=_nfl_db,
            include_trending=include_trending
        )
    except ValueError as e:
        return {
            "recommendation": None,
            "fairness_score": 0,
            "success": False,
            "error": f"Invalid input: {str(e)}"
        }


@timing_decorator("analyze_opponent", tool_type="opponent_analysis")
async def analyze_opponent(
    league_id: str,
    opponent_roster_id: int,
    current_week: Optional[int] = None
) -> dict:
    """Analyze an opponent's roster to identify weaknesses and exploitation opportunities.
    
    This tool provides comprehensive analysis of an opponent's fantasy roster including
    position-by-position strength assessment, starter vulnerability identification,
    depth chart weakness analysis, and strategic exploitation recommendations.
    
    Parameters:
        league_id (str, required): The unique identifier for the fantasy league.
        opponent_roster_id (int, required): Roster ID of the opponent to analyze.
        current_week (int, optional): Current NFL week for matchup context.
    
    Returns: {
        vulnerability_score: float (0-100, higher = more vulnerable),
        vulnerability_level: str (high, moderate, low),
        position_assessments: {...},
        starter_weaknesses: [...],
        exploitation_strategies: [...],
        matchup_context: {...} (if current_week provided),
        opponent_name: str,
        success: bool,
        error?: str
    }
    
    Example: analyze_opponent(
        league_id="12345",
        opponent_roster_id=2,
        current_week=10
    )
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        opponent_roster_id = validate_numeric_input(opponent_roster_id, min_val=1, max_val=20, required=True)
        
        if current_week is not None:
            current_week = validate_numeric_input(current_week, min_val=1, max_val=22, required=False)
        
        return await opponent_analysis_tools.analyze_opponent(
            league_id=league_id,
            opponent_roster_id=opponent_roster_id,
            current_week=current_week
        )
    except ValueError as e:
        return {
            "vulnerability_score": 0,
            "success": False,
            "error": f"Invalid input: {str(e)}"
        }


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