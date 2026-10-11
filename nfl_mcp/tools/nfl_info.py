"""NFL news, teams, depth charts, standings, schedules and league leaders.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import nfl_tools
from ..config import (
    validate_limit,
    validate_string_input,
)
from ..metrics import timing_decorator
from . import _context

# =============================================================================
# NFL NEWS AND INFO TOOLS
# =============================================================================

@timing_decorator("get_nfl_news", tool_type="nfl")
async def get_nfl_news(limit: int | None = 50) -> dict:
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
    return await nfl_tools.fetch_teams(_context.get_db())


@timing_decorator("get_depth_chart", tool_type="nfl")
async def get_depth_chart(team_id: str) -> dict:
    """Fetch a team's depth chart from ESPN HTML page.

    Parameters:
        team_id (str, required): Team abbreviation (e.g. 'KC','NE','DAL').
    Returns: {team_id, team_name, depth_chart:[{position, players[]}], success, error?}
    Example: get_depth_chart(team_id="KC")
    """
    return await nfl_tools.get_depth_chart(team_id)


@timing_decorator("get_team_player_stats", tool_type="nfl")
async def get_team_player_stats(team_id: str, season: int | None = None, season_type: int | None = 2, limit: int | None = 50) -> dict:
    """Season-to-date per-player stats for a team (Sleeper season totals).

    Parameters:
        team_id (str, required): Team abbreviation, any spelling (KC, WAS, LA).
        season (int, default: current season): Season year.
        season_type (int, default 2): 1=Pre,2=Regular,3=Post.
        limit (int, default 50, range 1-100): Max players.
    Returns: {team_id, team_name, season, season_type, player_stats:[{player_id,
        player_name, position, games_played, fantasy_points{std,half_ppr,ppr},
        passing?, rushing?, receiving?, kicking?, defense?, snaps?}], count,
        source, success, error?}
    Example: get_team_player_stats(team_id="KC", season=2024, limit=25)
    """
    try:
        season_i = int(season) if season is not None else None
    except Exception:
        season_i = None
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
async def get_nfl_standings(season: int | None = None, season_type: int | None = 2, group: int | None = None) -> dict:
    """Fetch NFL standings (league or conference) from ESPN Core API.

    Parameters:
        season (int, default: current season): Season year.
        season_type (int, default 2): 1=Pre,2=Regular,3=Post.
        group (int, optional): 1=AFC,2=NFC, None=all.
    Returns: {standings:[...], season, season_type, group, count, success, error?}
    Example: get_nfl_standings(season=2024, group=1)
    """
    try:
        season_i = int(season) if season is not None else None
    except Exception:
        season_i = None
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
async def get_team_schedule(team_id: str, season: int | None = None) -> dict:
    """Fetch a team's schedule (Site API) including matchup context.

    Parameters:
        team_id (str, required): Team abbreviation, any spelling (KC, WAS, LA).
        season (int, default: current season): Season year.
    Returns: {team_id, team_name, season, schedule:[...], count, success, error?}
    Example: get_team_schedule(team_id="KC", season=2024)
    """
    try:
        season_i = int(season) if season is not None else None
    except Exception:
        season_i = None
    return await nfl_tools.get_team_schedule(team_id=team_id, season=season_i)


# =============================================================================
# FEATURE-FLAGGED TOOLS
# =============================================================================
# Registered only when MCP_FEATURE_LEAGUE_LEADERS is on (see tool_registry).
@timing_decorator("get_league_leaders", tool_type="nfl")
async def get_league_leaders(stat_type: str = "passing", limit: int | None = 25) -> dict:
    """Get NFL league leaders by stat type (feature-flagged).

    Parameters:
        stat_type (str, default "passing"): passing/rushing/receiving/tackles/sacks.
        limit (int, default 25, range 1-100): Max leaders to return.
    Returns: {leaders: [...], stat_type, count, season, success, error?}
    Example: get_league_leaders(stat_type="rushing", limit=10)
    """
    # Map friendly labels to the underlying short category tokens.
    alias = {
        "passing": "pass", "pass": "pass", "passingyards": "pass",
        "rushing": "rush", "rush": "rush", "rushingyards": "rush",
        "receiving": "receiving", "rec": "receiving", "receivingyards": "receiving",
        "tackles": "tackles", "tackle": "tackles",
        "sacks": "sacks", "sack": "sacks",
    }
    try:
        stat_type = validate_string_input(stat_type, 'stat_type', max_length=20, required=True)
        limit = validate_limit(limit, 1, 100, 25)
    except ValueError as e:
        return {"leaders": [], "stat_type": stat_type, "count": 0, "success": False, "error": f"Invalid input: {e!s}"}

    category = alias.get(stat_type.strip().lower().replace("_", ""), stat_type.strip().lower())
    # Call by keyword (the underlying signature is get_league_leaders(category,
    # season, season_type, week) — passing limit positionally landed in season).
    res = await nfl_tools.get_league_leaders(category=category)
    if not res.get("success"):
        return {"leaders": [], "stat_type": stat_type, "count": 0,
                "success": False, "error": res.get("error"), "error_type": res.get("error_type")}
    players = (res.get("players") or [])[:limit]
    return {
        "leaders": players,
        "stat_type": res.get("category", category),
        "count": len(players),
        "season": res.get("season"),
        "success": True,
        "error": None,
    }
