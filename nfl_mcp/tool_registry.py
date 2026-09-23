"""Simplified tool registry for NFL MCP Server.

This module contains all MCP tool definitions in a clean, maintainable way.
Tools are defined as regular functions and registered with the FastMCP server.

Database access uses a ContextVar for async-safe dependency injection instead
of a mutable global, eliminating race conditions and making the code testable.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar

from . import (
    athlete_tools,
    briefing_tools,
    cbs_fantasy_tools,
    coaching_tools,
    draft_tools,
    faab_tools,
    handcuff_tools,
    ir_audit,
    league_changes_tools,
    lineup_optimizer_tools,
    matchup_tools,
    nfl_tools,
    opponent_analysis_tools,
    opportunity_tools,
    player_values,
    playoff_tools,
    projections,
    retro_tools,
    ros,
    sleeper_tools,
    sos_tools,
    streaming_tools,
    trade_analyzer_tools,
    trade_finder_tools,
    usage_trends,
    vegas_tools,
    waiver_target_tools,
    waiver_tools,
    weather_tools,
    web_tools,
    win_probability,
)
from .config import (
    FEATURE_LEAGUE_LEADERS,
    LIMITS,
    validate_limit,
    validate_numeric_input,
    validate_string_input,
)
from .database import NFLDatabase
from .metrics import timing_decorator
from .teams import normalize_team

logger = logging.getLogger(__name__)

# Async-safe database instance via ContextVar (replaces mutable global get_db())
_db_token: ContextVar[NFLDatabase | None] = ContextVar("nfl_db", default=None)


def initialize_shared(db: NFLDatabase) -> None:
    """Register the shared database instance for tool access.

    Called once at application startup to inject the DB.
    """
    _db_token.set(db)


def get_db() -> NFLDatabase | None:
    """Return the current request's database instance (or None)."""
    return _db_token.get()

def get_all_tools() -> list[Callable]:
    """Get list of all tool functions to register with FastMCP server."""
    tools = [
        # NFL News and Info
        get_nfl_news,
        get_teams,
        fetch_teams,
        get_depth_chart,
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
        get_weekly_briefing,
        get_weekly_retro,
        get_league_changes,

        # Season planning
        get_bye_week_plan,
        get_playoff_odds,

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
        get_waiver_targets,
        audit_ir_slots,
        recommend_faab_bid,
        get_handcuff_map,

        # Trade Analyzer Tools
        analyze_trade,
        find_trade_targets,

        # Player Value Tools (real market-consensus values)
        get_player_values,

        # Draft Assistant Tools (VBD board + live pick recommendations)
        get_draft_board,
        recommend_draft_pick,
        simulate_draft,

        # Projection Tools (transparent weekly projections)
        project_players,
        get_opportunity_projections,
        get_ros_projections,
        get_usage_trends,

        # Opponent Analysis Tools
        analyze_opponent,

        # Matchup Analysis Tools (Lineup Optimization)
        get_defense_rankings,
        get_matchup_difficulty,
        analyze_roster_matchups,

        # Strength-of-Schedule Tools (ROS / playoff-week planning)
        get_strength_of_schedule,

        # Streaming Planner (weekly DST/K/QB/TE matchup lookahead)
        get_streaming_options,

        # Weather / wind (game-environment analysis)
        get_weather_forecast,

        # Lineup Optimizer Tools (Start/Sit Recommendations)
        get_start_sit_recommendation,
        get_roster_recommendations,
        compare_players_for_slot,
        analyze_full_lineup,
        get_win_probability_lineup,

        # Vegas Lines Tools (Game Environment Analysis)
        get_vegas_lines,
        get_stack_opportunities,

        # Injury Report Tools (Multi-source with confidence scoring)
        get_injury_report,
        get_injury_trends,
        get_gameday_inactives,

        # Coaching Intelligence Tools
        get_coaching_staff,
        get_all_coaching_staffs,
        get_coaching_tree,
        get_scheme_classification,
    ]

    # Add feature-flagged tools
    if FEATURE_LEAGUE_LEADERS:
        tools.append(get_league_leaders)

    return tools


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
    return await nfl_tools.fetch_teams(get_db())


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
async def get_team_player_stats(team_id: str, season: int | None = 2026, season_type: int | None = 2, limit: int | None = 50) -> dict:
    """Season-to-date per-player stats for a team (Sleeper season totals).

    Parameters:
        team_id (str, required): Team abbreviation, any spelling (KC, WAS, LA).
        season (int, default 2026): Season year.
        season_type (int, default 2): 1=Pre,2=Regular,3=Post.
        limit (int, default 50, range 1-100): Max players.
    Returns: {team_id, team_name, season, season_type, player_stats:[{player_id,
        player_name, position, games_played, fantasy_points{std,half_ppr,ppr},
        passing?, rushing?, receiving?, kicking?, defense?, snaps?}], count,
        source, success, error?}
    Example: get_team_player_stats(team_id="KC", season=2024, limit=25)
    """
    try:
        season_i = int(season) if season is not None else 2026
    except Exception:
        season_i = 2026
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
async def get_nfl_standings(season: int | None = 2026, season_type: int | None = 2, group: int | None = None) -> dict:
    """Fetch NFL standings (league or conference) from ESPN Core API.

    Parameters:
        season (int, default 2026): Season year.
        season_type (int, default 2): 1=Pre,2=Regular,3=Post.
        group (int, optional): 1=AFC,2=NFC, None=all.
    Returns: {standings:[...], season, season_type, group, count, success, error?}
    Example: get_nfl_standings(season=2024, group=1)
    """
    try:
        season_i = int(season) if season is not None else 2026
    except Exception:
        season_i = 2026
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
async def get_team_schedule(team_id: str, season: int | None = 2026) -> dict:
    """Fetch a team's schedule (Site API) including matchup context.

    Parameters:
        team_id (str, required): Team abbreviation, any spelling (KC, WAS, LA).
        season (int, default 2026): Season year.
    Returns: {team_id, team_name, season, schedule:[...], count, success, error?}
    Example: get_team_schedule(team_id="KC", season=2024)
    """
    try:
        season_i = int(season) if season is not None else 2026
    except Exception:
        season_i = 2026
    return await nfl_tools.get_team_schedule(team_id=team_id, season=season_i)


# =============================================================================
# CBS FANTASY TOOLS
# =============================================================================

@timing_decorator("get_cbs_player_news", tool_type="cbs_fantasy")
async def get_cbs_player_news(limit: int | None = 50) -> dict:
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
    week: int | None = None,
    season: int | None = 2026,
    scoring: str = "ppr"
) -> dict:
    """Fetch SEASON-LONG fantasy football projections from CBS Sports for a position.

    CBS only publishes season-long projections: the week is validated and echoed
    back, but the source returns identical full-season numbers for every week.
    Results carry period="season" and week_honoured=False. Do NOT use these as
    week-level projections; use project_player/project_players for that.

    Parameters:
        position (str, default "QB"): Player position (QB, RB, WR, TE, K, DST).
        week (int, required): NFL week number (1-18). Validated, not honoured.
        season (int, default 2026): Season year.
        scoring (str, default "ppr"): Scoring format (ppr, half-ppr, standard).
    Returns: {projections: [...], total_projections, week, period, week_honoured,
        position, success, error?}
    Example: get_cbs_projections(position="RB", week=11, season=2026, scoring="ppr")
    """
    try:
        week_i = int(week) if week is not None else None
        season_i = int(season) if season is not None else 2026
    except Exception:
        week_i = None
        season_i = 2026
    return await cbs_fantasy_tools.get_cbs_projections(
        position=position,
        week=week_i,
        season=season_i,
        scoring=scoring
    )


@timing_decorator("get_cbs_expert_picks", tool_type="cbs_fantasy")
async def get_cbs_expert_picks(week: int | None = None) -> dict:
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
async def crawl_url(url: str, max_length: int | None = 10000) -> dict:
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
    return await athlete_tools.fetch_athletes(get_db())


@timing_decorator("lookup_athlete", tool_type="athlete")
def lookup_athlete(athlete_id: str) -> dict:
    """Look up an athlete by their ID.

    Parameters:
        athlete_id (str, required): The unique identifier for the athlete.
    Returns: {athlete, found, error?}
    Example: lookup_athlete(athlete_id="4034")
    """
    return athlete_tools.lookup_athlete(get_db(), athlete_id)


@timing_decorator("search_athletes", tool_type="athlete")
def search_athletes(name: str, limit: int | None = 10) -> dict:
    """Search for athletes by name (partial match supported).

    Parameters:
        name (str, required): Name or partial name to search for.
        limit (int, default 10, range 1-50): Maximum number of results.
    Returns: {athletes: [...], count, search_term, error?}
    Example: search_athletes(name="Mahomes", limit=5)
    """
    return athlete_tools.search_athletes(get_db(), name, limit)


@timing_decorator("get_athletes_by_team", tool_type="athlete")
def get_athletes_by_team(team_id: str) -> dict:
    """Get all athletes for a specific team.

    Parameters:
        team_id (str, required): The team identifier (e.g., "SF", "DAL", "NE").
    Returns: {athletes: [...], count, team_id, error?}
    Example: get_athletes_by_team(team_id="KC")
    """
    return athlete_tools.get_athletes_by_team(get_db(), team_id)


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
        return {"league": None, "success": False, "error": f"Invalid league_id: {e!s}"}


@timing_decorator("get_rosters", tool_type="sleeper")
async def get_rosters(league_id: str) -> dict:
    """Get league rosters with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_rosters(league_id)
    except ValueError as e:
        return {"rosters": [], "count": 0, "success": False, "error": f"Invalid league_id: {e!s}"}


@timing_decorator("get_league_users", tool_type="sleeper")
async def get_league_users(league_id: str) -> dict:
    """Get league users with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_league_users(league_id)
    except ValueError as e:
        return {"users": [], "count": 0, "success": False, "error": f"Invalid league_id: {e!s}"}


@timing_decorator("get_matchups", tool_type="sleeper")
async def get_matchups(league_id: str, week: int) -> dict:
    """Get league matchups with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        week = validate_numeric_input(week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=True)
        return await sleeper_tools.get_matchups(league_id, week)
    except ValueError as e:
        return {"matchups": [], "week": week, "count": 0, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("get_playoff_bracket", tool_type="sleeper")
async def get_playoff_bracket(league_id: str, bracket_type: str = "winners") -> dict:
    """Get playoff bracket (winners or losers) with validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        bracket_type = validate_string_input(bracket_type, 'bracket_type', max_length=10, required=False)
        return await sleeper_tools.get_playoff_bracket(league_id, bracket_type)
    except ValueError as e:
        return {"playoff_bracket": None, "bracket_type": bracket_type, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("get_transactions", tool_type="sleeper")
async def get_transactions(league_id: str, week: int | None = None, round: int | None = None) -> dict:
    """Get COMPLETED league transactions for a week (adds, drops, trades, waivers).

    Does NOT include pending waiver claims. Sleeper exposes a claim only after it
    has been processed, so a claim sitting in someone's queue appears nowhere in
    this response. An empty list means "nothing processed yet", not "nobody has
    claims in" — do not report the absence of claims from this tool. The league
    app is the only place pending claims are visible.

    Parameters:
        league_id (str, required): Sleeper league id.
        week (int, required): NFL week (alias: `round`).
    Returns: {transactions: [...], week, count, success, error?}
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        # Accept either week or deprecated round
        effective_week = week if week is not None else round
        if effective_week is None:
            raise ValueError("week (or round) is required")
        effective_week = validate_numeric_input(effective_week, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=True)
        return await sleeper_tools.get_transactions(league_id, round=effective_week, week=effective_week)
    except ValueError as e:
        return {"transactions": [], "week": week, "count": 0, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("get_traded_picks", tool_type="sleeper")
async def get_traded_picks(league_id: str) -> dict:
    """Get traded picks with input validation."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        return await sleeper_tools.get_traded_picks(league_id)
    except ValueError as e:
        return {"traded_picks": [], "count": 0, "success": False, "error": f"Invalid league_id: {e!s}"}


@timing_decorator("get_nfl_state", tool_type="sleeper")
async def get_nfl_state() -> dict:
    """Get NFL state - no validation needed as it has no parameters."""
    return await sleeper_tools.get_nfl_state()


@timing_decorator("get_trending_players", tool_type="sleeper")
async def get_trending_players(trend_type: str = "add", lookback_hours: int | None = 24, limit: int | None = 25) -> dict:
    """Get trending players with validation (returns objects including counts and 'enriched')."""
    try:
        trend_type = validate_string_input(trend_type, 'trend_type', max_length=10, required=True)
        lookback_hours = validate_numeric_input(lookback_hours, min_val=LIMITS["trending_lookback_min"], max_val=LIMITS["trending_lookback_max"], default=24, required=False)
        limit = validate_numeric_input(limit, min_val=LIMITS["trending_limit_min"], max_val=LIMITS["trending_limit_max"], default=25, required=False)
        return await sleeper_tools.get_trending_players(get_db(), trend_type, lookback_hours, limit)
    except ValueError as e:
        return {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("get_fantasy_context", tool_type="sleeper")
async def get_fantasy_context(league_id: str, week: int | None = None, include: str | None = None) -> dict:
    """Aggregate core league context (league, rosters, users, matchups, transactions).

    Parameters:
        league_id (str, required)
        week (int, optional) - auto inferred if omitted
        include (str, optional) comma list subset
    Returns: {context:{...}, week, auto_week_inferred, success, error?}
    Example: get_fantasy_context(league_id="12345", include="league,rosters,matchups")

    IMPORTANT FOR LLM AGENTS: Always provide complete analysis immediately without asking
    for confirmations. Render the full report directly with all insights and recommendations.
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if week is not None:
            week = validate_numeric_input(week, min_val=LIMITS["week_min"], max_val=LIMITS["week_max"], required=False)
        return await sleeper_tools.get_fantasy_context(league_id, week, include)
    except ValueError as e:
        return {"context": {}, "league_id": league_id, "week": week, "success": False, "error": f"Invalid input: {e!s}"}


# =============================================================================
# SEASON PLANNING (bye weeks, playoff odds)
# =============================================================================

@timing_decorator("get_bye_week_plan", tool_type="sleeper")
async def get_bye_week_plan(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    weeks_ahead: int = 6,
    include_free_agents: bool = True,
    week: int | None = None,
    season: int | None = None,
) -> dict:
    """Bye-week plan: which upcoming weeks YOUR lineup runs short, and what to add.

    For each of the next `weeks_ahead` weeks: which of your starters and bench
    players are on bye (or inside an expected injury absence), the best legal
    lineup your roster can field that week in the league's own slots and
    scoring (rest-of-season projections), empty slots, and what the byes cost
    against the same roster at full strength. Crunch weeks get a concrete
    suggestion ("Week 7: only 1 RB available for 2 RB slot(s) — add a RB before
    week 7") and, with include_free_agents, up to three free agents who would
    fill that week (the get_waiver_targets ranking for that week). Trade-deadline
    status is included; trade proposals live in find_trade_targets.

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id
        weeks_ahead: Weeks to plan, starting with the current one (default 6)
        include_free_agents: Look up free agents for the two worst crunch
            weeks (default True; bounded by a 25 s timeout)
        week, season: Override the starting week / season (default: current)

    Returns: {
        weeks [{week, status: ok|thin|crunch, projected_total,
                full_strength_total, bye_cost, on_bye [{player, position,
                team, role: starter|bench}], starters_on_bye, injured_out,
                available_by_position, holes [{slot, eligible}], lineup,
                positions_to_add?}],
        crunch_weeks, thin_weeks, suggestions [str], free_agent_options
        {week: [{name, position, team, projected_points, upgrade_points,
        recommendation}]}, core_starters, trade_deadline, method, success
    }

    Example: get_bye_week_plan(league_id="123", roster_id=7)
    Example: get_bye_week_plan(league_id="123", user_id="456", weeks_ahead=10)
    """
    from . import bye_week_tools
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
    except ValueError as e:
        return {"weeks": [], "success": False, "error": f"Invalid input: {e!s}"}
    return await bye_week_tools.get_bye_week_plan(
        league_id=league_id, roster_id=roster_id, user_id=user_id,
        weeks_ahead=weeks_ahead, week=week, season=season,
        include_free_agents=include_free_agents, db=get_db(),
    )


@timing_decorator("get_playoff_odds", tool_type="sleeper")
async def get_playoff_odds(
    league_id: str,
    current_week: int | None = None,
    num_sims: int | None = 10000,
    score_sd: float | None = None,
    my_roster_id: int | None = None,
    seed: int | None = None,
) -> dict:
    """Compute playoff probabilities via Monte-Carlo of the rest of the season.

    Simulates every remaining regular-season matchup (each team scores ~ Normal
    around its strength), ranks by record then points, and counts how often
    each team makes a playoff seed. Strength blends points-per-game so far with
    the roster's projected best lineup for each remaining week (byes and
    injuries included); the projection leads early, actual results take over
    as the season goes on. Each team's weekly spread is measured from its own
    played weeks and shrunk toward the league's.

    Parameters:
        league_id (str, required): Sleeper league id.
        current_week (int, optional): First unplayed week (defaults to NFL state).
        num_sims (int): Iterations (default 10000, capped 100..50000).
        score_sd (float, optional): Override the measured spread with one value
            for every team. Leave unset to measure it.
        my_roster_id (int, optional): Also returns your win/lose-this-week swing.
        seed (int, optional): RNG seed for reproducibility.
    Returns: {odds:[{roster_id, name, record, mean_ppg, actual_ppg,
              projected_ppg, actual_weight, score_sd, games_scored,
              playoff_pct, avg_seed}], strength_source ('blended'|'actual'|
              'projected'|'league_average'), score_sd_source ('measured'|
              'default'|'caller'), league_score_sd, this_week_swing?,
              playoff_teams, current_week, success}

    IMPORTANT FOR LLM AGENTS: Render the odds immediately without asking for confirmation.
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if current_week is not None:
            current_week = validate_numeric_input(current_week, min_val=1, max_val=22, required=False)
        if my_roster_id is not None:
            my_roster_id = validate_numeric_input(my_roster_id, min_val=1, max_val=32, required=False)
    except ValueError as e:
        return {"odds": [], "success": False, "error": f"Invalid input: {e!s}"}
    return await playoff_tools.get_playoff_odds(
        league_id=league_id, current_week=current_week, num_sims=num_sims or 10000,
        score_sd=score_sd, my_roster_id=my_roster_id, seed=seed, db=get_db(),
    )


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
        return {"user": None, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("get_user_leagues", tool_type="sleeper")
async def get_user_leagues(user_id: str, season: int) -> dict:
    """Fetch all leagues for a user and season."""
    try:
        user_id = validate_string_input(user_id, 'user_id', max_length=40, required=True)
        season = validate_numeric_input(season, min_val=2017, max_val=2030, required=True)
        return await sleeper_tools.get_user_leagues(user_id, season)
    except ValueError as e:
        return {"leagues": [], "count": 0, "season": season, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("get_league_drafts", tool_type="sleeper")
async def get_league_drafts(league_id: str) -> dict:
    """Fetch all drafts for a league."""
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=40, required=True)
        return await sleeper_tools.get_league_drafts(league_id)
    except ValueError as e:
        return {"drafts": [], "count": 0, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("get_draft", tool_type="sleeper")
async def get_draft(draft_id: str) -> dict:
    """Fetch a specific draft."""
    try:
        draft_id = validate_string_input(draft_id, 'draft_id', max_length=40, required=True)
        return await sleeper_tools.get_draft(draft_id)
    except ValueError as e:
        return {"draft": None, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("get_draft_picks", tool_type="sleeper")
async def get_draft_picks(draft_id: str) -> dict:
    """Fetch all picks in a draft."""
    try:
        draft_id = validate_string_input(draft_id, 'draft_id', max_length=40, required=True)
        return await sleeper_tools.get_draft_picks(draft_id)
    except ValueError as e:
        return {"picks": [], "count": 0, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("get_draft_traded_picks", tool_type="sleeper")
async def get_draft_traded_picks(draft_id: str) -> dict:
    """Fetch traded picks in a draft."""
    try:
        draft_id = validate_string_input(draft_id, 'draft_id', max_length=40, required=True)
        return await sleeper_tools.get_draft_traded_picks(draft_id)
    except ValueError as e:
        return {"traded_picks": [], "count": 0, "success": False, "error": f"Invalid input: {e!s}"}


@timing_decorator("fetch_all_players", tool_type="sleeper")
async def fetch_all_players(force_refresh: bool = False) -> dict:
    """Fetch full Sleeper players map (cached). Returns counts only to minimize payload."""
    try:
        return await sleeper_tools.fetch_all_players(force_refresh)
    except ValueError as e:
        return {"players": {}, "cached": False, "success": False, "error": f"Invalid input: {e!s}"}


# =============================================================================
# WAIVER WIRE TOOLS
# =============================================================================

_WAIVER_SECTIONS = ("log", "summary", "re_entries")


def _player_ids_for(player: str) -> set[str]:
    """Sleeper ids matching a waiver-log player filter (an id or a name)."""
    player = str(player).strip()
    if player.isdigit() or (player.isalpha() and player.isupper() and len(player) <= 4):
        return {player}
    db = get_db()
    hits = (db.search_athletes_by_name(player, limit=10) or []) if db is not None else []
    return {str(h.get("id")) for h in hits if h.get("id")}


@timing_decorator("get_waiver_log", tool_type="waiver")
async def get_waiver_log(
    league_id: str,
    round: int | None = None,
    sections: list[str] | None = None,
    player: str | None = None,
    dedupe: bool = True,
) -> dict:
    """What already HAPPENED on this league's waiver wire: processed claims,
    failed claims, summary counts, and players dropped and re-added.

    One tool for the transaction-log view of waivers (who to pick up next is
    get_waiver_targets; how much to bid is recommend_faab_bid). Pending claims
    are never visible — Sleeper only exposes a claim once processed.

    Parameters:
        league_id (str, required): Sleeper league id.
        round (int, optional): NFL week to read (default: current week).
        sections (list, optional): any of "log" (de-duplicated waiver/free-agent
            transactions + failed claims), "summary" (dashboard counts),
            "re_entries" (players dropped and re-added; volatile = more than one
            re-entry). Default: all three.
        player (str, optional): Sleeper player id or name — keep only
            transactions/re-entries involving him.
        dedupe (bool, default True): Remove duplicate transactions from the log.

    Returns: {
        waiver_log, duplicates_found, total_transactions, unique_transactions,
        failed_claims, failed_claims_count            (section "log"),
        dashboard_summary {total_waiver_transactions, duplicates_removed,
            players_with_re_entries, volatile_players_count, failed_claims,
            deduplication_rate}                        (section "summary"),
        re_entry_players {player_id: {re_entries, is_volatile, ...}},
        volatile_players, total_players_analyzed        (section "re_entries"),
        sections, player_filter?, league_id, round, success
    }

    Example: get_waiver_log(league_id="123")
    Example: get_waiver_log(league_id="123", round=3, sections=["re_entries"], player="Tyler Allgeier")
    """
    import asyncio

    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
    except ValueError as e:
        return {"waiver_log": [], "league_id": league_id, "round": round, "success": False, "error": f"Invalid input: {e!s}"}

    alias = {"dashboard": "summary", "re_entry": "re_entries", "reentries": "re_entries"}
    wanted = [s.strip().lower() for s in (sections or _WAIVER_SECTIONS) if isinstance(s, str)]
    wanted = list(dict.fromkeys(alias.get(s, s) for s in wanted))
    unknown = sorted(set(wanted) - set(_WAIVER_SECTIONS))
    if unknown or not wanted:
        return {"success": False, "league_id": league_id, "round": round,
                "error": f"Unknown section(s) {unknown}; use any of {list(_WAIVER_SECTIONS)}."}

    need_log = "log" in wanted or "summary" in wanted
    need_re = "re_entries" in wanted or "summary" in wanted
    log_res, re_res = await asyncio.gather(
        waiver_tools.get_waiver_log(league_id, round, dedupe) if need_log else asyncio.sleep(0, {}),
        waiver_tools.check_re_entry_status(league_id, round) if need_re else asyncio.sleep(0, {}),
    )
    for res in (log_res, re_res):
        if res and not res.get("success", True):
            return {**res, "sections": wanted}

    ids = _player_ids_for(player) if player else None

    def _involves(tx: dict) -> bool:
        keys = set((tx.get("adds") or {}).keys()) | set((tx.get("drops") or {}).keys())
        keys |= set(tx.get("wanted") or []) | set(tx.get("would_have_dropped") or [])
        return bool(keys & ids)

    out: dict = {"league_id": league_id, "round": round, "sections": wanted, "success": True, "error": None}
    log = log_res.get("waiver_log") or []
    failed = log_res.get("failed_claims") or []
    re_players = re_res.get("re_entry_players") or {}
    volatile = re_res.get("volatile_players") or []
    if ids is not None:
        log = [t for t in log if _involves(t)]
        failed = [t for t in failed if _involves(t)]
        re_players = {k: v for k, v in re_players.items() if k in ids}
        volatile = [p for p in volatile if p in ids]
        out["player_filter"] = {"query": player, "player_ids": sorted(ids)}
    if "log" in wanted:
        out.update({
            "waiver_log": log,
            "duplicates_found": log_res.get("duplicates_found") or [],
            "total_transactions": log_res.get("total_transactions", 0),
            "unique_transactions": log_res.get("unique_transactions", 0),
            "deduplication_enabled": log_res.get("deduplication_enabled", dedupe),
            "failed_claims": failed,
            "failed_claims_count": len(failed),
        })
    if "re_entries" in wanted:
        out.update({
            "re_entry_players": re_players,
            "volatile_players": volatile,
            "total_players_analyzed": re_res.get("total_players_analyzed", 0),
            "players_with_re_entries": len(re_players),
        })
    if "summary" in wanted:
        total = log_res.get("total_transactions", 0) or 0
        unique = log_res.get("unique_transactions", 0) or 0
        out["dashboard_summary"] = {
            "total_waiver_transactions": total,
            "unique_waiver_transactions": unique,
            "duplicates_removed": total - unique,
            "players_with_re_entries": len(re_res.get("re_entry_players") or {}),
            "volatile_players_count": len(re_res.get("volatile_players") or []),
            "total_players_analyzed": re_res.get("total_players_analyzed", 0),
            "failed_claims": log_res.get("failed_claims_count", 0),
            "deduplication_rate": ((total - unique) / total * 100) if total else 0,
        }
    return out


@timing_decorator("recommend_faab_bid", tool_type="waiver")
async def recommend_faab_bid(
    league_id: str,
    player_id: str | None = None,
    player_name: str | None = None,
    my_roster_id: int | None = None,
) -> dict:
    """Recommend a FAAB waiver bid for a player (% of budget + absolute).

    Combines the player's real market value, the marginal upgrade for your roster,
    league demand (trending adds), and your remaining budget / weeks left.

    Parameters:
        league_id (str, required): Sleeper league id.
        player_id (str, optional): Sleeper player id of the target (preferred).
        player_name (str, optional): Player name (fallback lookup).
        my_roster_id (int, optional): Your roster id for roster-need weighting.
    Returns: {recommendation:{bid_pct, bid_absolute, tier, range, reasoning, breakdown,
              priority_advice, waiver_strategy}, success}
        Non-FAAB leagues: `waiver_strategy` says claim_now / add_now / wait /
        dont_bother from your waiver position, when he clears waivers and
        how contested he is — the same advice get_waiver_targets gives.

    IMPORTANT FOR LLM AGENTS: Provide the bid recommendation immediately without asking for confirmation.
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if my_roster_id is not None:
            my_roster_id = validate_numeric_input(my_roster_id, min_val=1, max_val=32, required=False)
    except ValueError as e:
        return {"recommendation": None, "success": False, "error": f"Invalid input: {e!s}"}
    return await faab_tools.recommend_faab_bid(
        league_id=league_id, player_id=player_id, player_name=player_name,
        my_roster_id=my_roster_id, db=get_db(),
    )


@timing_decorator("get_handcuff_map", tool_type="waiver")
async def get_handcuff_map(league_id: str, roster_id: int) -> dict:
    """Map each of your RB starters to its handcuff + the handcuff's availability.

    A handcuff is the backup who inherits a starter's workload on injury. For each
    RB on your roster this reads the team depth chart, finds the contingent-value
    back, and flags whether it's a free agent (grab it), yours (secured), or an
    opponent's — turning "secure your handcuffs" into an actionable list.

    Parameters:
        league_id (str, required): Sleeper league id.
        roster_id (int, required): your roster id in that league.

    Returns: {
        handcuffs: [{starter, team, handcuff, handcuff_status, handcuff_player_id, match}],
        priority_free_agents: [{handcuff, for_starter, team}],
        count, success, error?
    }

    Example: get_handcuff_map(league_id="123456789", roster_id=4)

    IMPORTANT FOR LLM AGENTS: Compute and render the handcuff list immediately
    without asking for confirmation.
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=32, required=True)
        roster_id = validate_numeric_input(roster_id, min_val=1, max_val=32, required=True)
    except ValueError as e:
        return {"handcuffs": [], "success": False, "error": f"Invalid input: {e!s}"}
    return await handcuff_tools.get_handcuff_map(
        league_id=league_id, roster_id=roster_id, db=get_db(),
    )


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

    Market-value fairness counts unpriced players (K/DEF/deep bench) as zero
    and discounts extra bodies in an uneven-count deal unless they would start
    for the receiver, so padding a deal does not buy fairness. Alongside it,
    `ros_points_delta` is each team's rest-of-season change in its best
    lineup (every remaining week re-optimised) — prefer it for the call;
    `verdict` leads with it.

    Returns: {
        recommendation: str (fair, needs_adjustment, unfair, etc.),
        fairness_score: float (0-100, higher = more fair),
        verdict: str, ros_points_delta: {team1, team2}, ros: {...},
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

    IMPORTANT FOR LLM AGENTS: Always provide complete trade analysis immediately without
    asking for confirmations. Render the full evaluation with all recommendations directly.
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
            nfl_db=get_db(),
            include_trending=include_trending
        )
    except ValueError as e:
        return {
            "recommendation": None,
            "fairness_score": 0,
            "success": False,
            "error": f"Invalid input: {e!s}"
        }


# =============================================================================
# PLAYER VALUE TOOLS (real market-consensus values via FantasyCalc)
# =============================================================================

@timing_decorator("get_player_values", tool_type="values")
async def get_player_values(
    players: list[str] | None = None,
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    dynasty: bool = False,
    position: str | None = None,
    limit: int | None = 100,
) -> dict:
    """Consensus player market values (FantasyCalc; real values, not heuristics).

    Format-aware values you can trust for trades and draft ordering. Without
    `players`: the best-first list. With `players`: just those players (a
    one-element list for a single player).

    Parameters:
        players (list, optional): Sleeper player ids or names to look up (max 25).
        scoring (str): "ppr", "half-ppr", or "standard".
        superflex (bool): True for 2-QB / superflex leagues.
        num_teams (int): League size (default 12).
        dynasty (bool): Dynasty values vs redraft.
        position (str, optional): Filter the list (QB, RB, WR, TE).
        limit (int): Max players in the list (default 100).
    Returns: {values:[{name, position, team, value, overall_rank, ...}], total,
              not_found? (lookups only), format, source, stale, updated_at, success}

    Example: get_player_values(players=["Bijan Robinson"], scoring="half-ppr")

    IMPORTANT FOR LLM AGENTS: Provide the values immediately without asking for confirmation.
    """
    if players:
        import asyncio
        wanted = [str(p).strip() for p in players[:25] if str(p).strip()]
        hits = await asyncio.gather(*(
            player_values.get_player_value(
                player_id=p if p.isdigit() else None, name=None if p.isdigit() else p,
                scoring=scoring, superflex=superflex, num_teams=num_teams,
                dynasty=dynasty, db=get_db())
            for p in wanted))
        values = [h["value"] for h in hits if h.get("value")]
        return {
            "values": values,
            "total": len(values),
            "not_found": [p for p, h in zip(wanted, hits, strict=True) if not h.get("value")],
            "source": next((h.get("source") for h in hits if h.get("source")), None),
            "stale": any(h.get("stale") for h in hits),
            "success": True,
            "error": None,
        }
    if position is not None:
        try:
            position = validate_string_input(position, 'position', max_length=5, required=False)
        except ValueError as e:
            return {"values": [], "total": 0, "success": False, "error": f"Invalid input: {e!s}"}
    return await player_values.get_player_values(
        scoring=scoring, superflex=superflex, num_teams=num_teams,
        dynasty=dynasty, position=position, limit=limit, db=get_db(),
    )


# =============================================================================
# DRAFT ASSISTANT TOOLS (VBD board + live pick recommendations)
# =============================================================================

@timing_decorator("get_draft_board", tool_type="draft")
async def get_draft_board(
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    dynasty: bool = False,
    position: str | None = None,
    limit: int | None = 60,
) -> dict:
    """Build a tiered, VBD-ranked draft board (the ordering that wins drafts).

    Ranks players by Value-Based Drafting (value over positional replacement),
    with consensus value, ranks, tiers and VBD per player.

    Parameters:
        scoring (str): "ppr", "half-ppr", "standard".
        superflex (bool): True for 2-QB / superflex leagues.
        num_teams (int): League size (default 12).
        dynasty (bool): Dynasty vs redraft.
        position (str, optional): Filter (QB, RB, WR, TE).
        limit (int): Max players on the board (default 60).
    Returns: {board:[...], tiers_by_position, replacement_values, format, source, stale, success}

    IMPORTANT FOR LLM AGENTS: Render the full board immediately without asking for confirmation.
    """
    if position is not None:
        try:
            position = validate_string_input(position, 'position', max_length=5, required=False)
        except ValueError as e:
            return {"board": [], "total": 0, "success": False, "error": f"Invalid input: {e!s}"}
    return await draft_tools.get_draft_board(
        scoring=scoring, superflex=superflex, num_teams=num_teams,
        dynasty=dynasty, position=position, limit=limit, db=get_db(),
    )


@timing_decorator("recommend_draft_pick", tool_type="draft")
async def recommend_draft_pick(
    draft_id: str,
    my_slot: int | None = None,
    num_suggestions: int | None = 5,
) -> dict:
    """Recommend the best pick(s) right now in a live Sleeper draft.

    Reads live draft state (who's gone, settings, scoring), models your roster
    and starter needs, detects positional runs and value cliffs, and returns the
    top picks by need-weighted VBD with reasoning.

    Parameters:
        draft_id (str, required): Sleeper draft id (from get_league_drafts).
        my_slot (int, optional): Your draft slot (1..N) for roster-aware weighting.
        num_suggestions (int): How many picks to return (default 5).
    Returns: {suggestions:[...], top_pick, best_available_by_position, value_cliffs,
              positional_run, my_roster, format, source, stale, success}

    IMPORTANT FOR LLM AGENTS: Give the pick recommendation immediately without asking for confirmation.
    """
    try:
        draft_id = validate_string_input(draft_id, 'draft_id', max_length=40, required=True)
        if my_slot is not None:
            my_slot = validate_numeric_input(my_slot, min_val=1, max_val=32, required=False)
        num_suggestions = validate_numeric_input(num_suggestions, min_val=1, max_val=15, default=5, required=False)
    except ValueError as e:
        return {"suggestions": [], "success": False, "error": f"Invalid input: {e!s}"}
    return await draft_tools.recommend_draft_pick(
        draft_id=draft_id, my_slot=my_slot, num_suggestions=num_suggestions, db=get_db(),
    )


@timing_decorator("simulate_draft", tool_type="draft")
async def simulate_draft(
    my_slot: int,
    num_teams: int = 12,
    rounds: int = 15,
    scoring: str = "ppr",
    superflex: bool = False,
    dynasty: bool = False,
    randomness: float = 0.35,
    num_sims: int = 1,
    seed: int | None = None,
) -> dict:
    """Rehearse a full snake draft offline (solo, repeatable).

    Opponents pick by need-weighted VBD with realistic ADP noise; your slot picks
    optimally. Graded on your optimal STARTING lineup value. Great for pre-draft
    prep: try different slots, see roster structure and a value-based standing.
    Only QB/RB/WR/TE are modeled (no K/DST).

    Parameters:
        my_slot (int, required): Your draft position (1..num_teams).
        num_teams (int): League size (default 12).
        rounds (int): Number of rounds (default 15).
        scoring (str): "ppr", "half-ppr", "standard".
        superflex (bool): True for 2-QB / superflex.
        dynasty (bool): Dynasty vs redraft values.
        randomness (float): Opponent ADP noise 0..1 (default 0.35 ~ realistic).
        num_sims (int): How many drafts to run (>1 returns aggregate structure).
        seed (int, optional): RNG seed for reproducibility.
    Returns: {sample:{my_team, standings, grade,...}, aggregate?, format, source, success}

    IMPORTANT FOR LLM AGENTS: Run the simulation and present the result immediately.
    """
    try:
        my_slot = validate_numeric_input(my_slot, min_val=1, max_val=32, required=True)
        num_teams = validate_numeric_input(num_teams, min_val=2, max_val=32, default=12, required=False)
        rounds = validate_numeric_input(rounds, min_val=1, max_val=30, default=15, required=False)
        num_sims = validate_numeric_input(num_sims, min_val=1, max_val=200, default=1, required=False)
        if seed is not None:
            seed = validate_numeric_input(seed, min_val=0, max_val=2**31 - 1, required=False)
    except ValueError as e:
        return {"sample": None, "success": False, "error": f"Invalid input: {e!s}"}
    return await draft_tools.simulate_draft(
        my_slot=my_slot, num_teams=num_teams, rounds=rounds, scoring=scoring,
        superflex=superflex, dynasty=dynasty, randomness=randomness,
        num_sims=num_sims, seed=seed, db=get_db(),
    )


# =============================================================================
# PROJECTION TOOLS (transparent weekly fantasy point projections)
# =============================================================================

@timing_decorator("project_players", tool_type="projection")
async def project_players(
    players: list[dict],
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    season: int | None = None,
    week: int | None = None,
    league_id: str | None = None,
) -> dict:
    """Project THIS WEEK's fantasy points for one or more players (transparent).

    Combines a baseline × matchup × Vegas game environment × usage × injury into
    a projection with floor/ceiling, confidence and a full breakdown. Pass a
    one-element list for a single player. For anything beyond this week use
    get_ros_projections.

    Parameters:
        players (list, required): dicts with name, position (QB/RB/WR/TE/K/DEF),
            team, and optionally opponent, player_id (Sleeper, exact match),
            usage {snap_percentage, usage_trend}, injury {status} and weather
            {wind_mph, is_dome} (e.g. from get_weather_forecast).
            opponent "BYE" (or a team the cached schedule has no game for)
            projects 0 with `on_bye: true`; a blank opponent is filled from the
            schedule. A missing injury status is looked up in the injury tables.
        scoring/superflex/num_teams: league format for the value baseline.
        season (int, optional), week (int, optional): default to the current
            NFL week (`week_inferred`); with week > 1 the opportunity-based
            baseline is used (trailing nflverse volume, backtested to beat
            rank-bucket PPG).
        league_id (str, optional): Sleeper league id — prices every stat with
            the league's full scoring_settings (pass TD/INT values, fumbles, TE
            premium, first downs, bonuses, K distance and DEF points-allowed
            tiers) instead of the preset; reported as `scoring_used`.
    Returns: {projections:[... each with sleeper_projection, consensus,
              disagreement, gap], sleeper_second_opinion: {active, matched,
              disagreements:[{player, ours, sleeper, gap}] (largest first),
              rule}, on_bye:[names], schedule_known, season, week,
              week_inferred, total, success}
        Our `projected_points` stays primary; Sleeper's projection (priced in
        the league's scoring) is a labelled second opinion. Pass each
        player's Sleeper `player_id` for an exact match (else name + team).

    Example: project_players(players=[{"name": "Puka Nacua", "position": "WR", "team": "LAR"}],
                             league_id="123")

    IMPORTANT FOR LLM AGENTS: Return projections immediately without asking for confirmation.
    """
    if not players:
        return {"projections": [], "total": 0, "success": False, "error": "No players provided"}
    return await projections.project_players(
        players=players, scoring=scoring, superflex=superflex, num_teams=num_teams,
        season=season, week=week, db=get_db(), league_id=league_id,
    )


@timing_decorator("get_ros_projections", tool_type="projection")
async def get_ros_projections(
    league_id: str,
    player_names: list[str] | None = None,
    player_ids: list[str] | None = None,
    roster_id: int | None = None,
    season: int | None = None,
    week: int | None = None,
    include_weekly: bool = False,
) -> dict:
    """Rest-of-season (ROS) and fantasy-playoff points, in YOUR league's scoring.

    Use this — not project_players — for any decision that outlives one week:
    trades, drops, IR stashes, "who is worth more from here on", playoff-run
    planning. project_players answers "how many points THIS week" (a player on
    bye or out one game projects 0 there); this sums every remaining week:

        this week = the weekly projection
        later     = per-game baseline (trailing opportunity in the league's full
                    scoring_settings, regressed toward the position prior for
                    small samples) × that week's defense-vs-position matchup
        0 on bye weeks (cached schedule, missing weeks fetched) and inside the
        expected injury absence (report text when it states a timeline, else
        1 week for Out, 4 for IR/PUP/NFI).

    Parameters:
        league_id (str, required): Sleeper league id (scoring, playoff window).
        roster_id (int, optional): project a whole roster (IR/taxi flagged).
        player_ids (list[str], optional): Sleeper player ids.
        player_names (list[str], optional): names, resolved in the athlete cache.
        season, week (optional): default to the current NFL week.
        include_weekly (bool, default False): add the per-week breakdown.

    Returns: {players: [{player, position, team, ros_points (rest of regular
              season), playoff_points (league's playoff weeks), total_points,
              weeks_counted, bye_weeks, injury_weeks, injury_window, per_game,
              baseline_source, weekly?}], regular_season_weeks, playoff_weeks,
              unresolved, elapsed_seconds, success}

    Example: get_ros_projections(league_id="123", roster_id=7)
    Example: get_ros_projections(league_id="123", player_names=["Puka Nacua"], include_weekly=True)
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
    except ValueError as e:
        return {"players": [], "success": False, "error": f"Invalid input: {e!s}"}
    return await ros.get_ros_projections(
        league_id=league_id, player_names=player_names, player_ids=player_ids,
        roster_id=roster_id, season=season, week=week, include_weekly=include_weekly,
        db=get_db(),
    )


@timing_decorator("get_opportunity_projections", tool_type="projection")
async def get_opportunity_projections(
    season: int,
    week: int,
    players: list[str] | None = None,
    lookback: int = 6,
    min_games: int = 2,
    top_n: int = 50,
    scoring: str = "ppr",
    league_id: str | None = None,
) -> dict:
    """Opportunity-based projections from trailing volume (beats trailing-PPG).

    Projects each player's next-week points from recency-weighted trailing
    targets/carries (QB: pass attempts) × their points-per-opportunity shrunk
    toward a position prior. Volume is stickier than points, so this orders
    players better for start/sit (validated on the backtest: MAE and Spearman
    both improve vs a trailing-PPG baseline). Key-free (nflverse).

    Parameters:
        season (int, required): NFL season year.
        week (int, required): Week to project (>= 2; uses weeks < week as history).
        players (list, optional): Names or player_ids to project; omit for the
            top_n projected players (waiver/streamer discovery).
        lookback (int, optional): Trailing games to weight (default 6).
        min_games (int, optional): Min prior games to project a player (default 2).
        top_n (int, optional): Cap when players is omitted (default 50).
        scoring (str, optional): 'ppr' (default), 'half_ppr', 'standard', or a
            raw per-reception value like '0.5'. Changes both the points and the
            receiver-vs-runner order, so pass your league's real setting.
        league_id (str, optional): Sleeper league id — prices every stat with
            the league's full scoring_settings (pass TD/INT values, fumbles, TE
            premium, first downs, bonuses, K distance and DEF points-allowed
            tiers) instead of the preset; reported as `scoring_used`.

    Returns: {
        season, week, lookback, count, scoring, ppr, scoring_used,
        projections: [{player_id, name, position, team, projected_points
                       (in `scoring`; `projected_ppr` is the same number under
                       its historical key), exp_targets, exp_carries,
                       games_used}] (highest-first),
        success: bool, error?: str
    }

    Example: get_opportunity_projections(season=2025, week=10, players=["Puka Nacua"])

    IMPORTANT FOR LLM AGENTS: Compute and render the projections immediately
    without asking for confirmation.
    """
    return await opportunity_tools.get_opportunity_projections(
        season=season,
        week=week,
        players=players,
        lookback=lookback,
        min_games=min_games,
        top_n=top_n,
        scoring=scoring,
        league_id=league_id,
    )


@timing_decorator("get_usage_trends", tool_type="projection")
async def get_usage_trends(
    league_id: str | None = None,
    roster_id: int | None = None,
    player_names: list[str] | None = None,
    weeks: int = 4,
    season: int | None = None,
    through_week: int | None = None,
) -> dict:
    """Week-by-week usage and role trends for a roster or a list of players.

    Per player per week: target share, air-yards share, WOPR, RACR, share of
    the team's carries (nflverse weekly stats), offensive snap share and
    red-zone opportunities (Sleeper weekly stats: `rec_rz_tgt` + `rush_rz_att`
    — real counts, not estimated from TDs). Each metric gets a direction —
    rising / falling / stable — from its least-squares change across the
    played weeks, and each player a short flag list ("target share up 3 weeks
    in a row", "part-time role: 42% of snaps in week 3", "did not play week 2").
    Bye and missed weeks are shown and left out of the trend. Key-free.

    Parameters:
        league_id (str, optional) + roster_id (int, optional): every QB/RB/WR/TE
            on that Sleeper roster (K/DEF have no usage shares).
        player_names (list, optional): names to look up instead (max 30).
        weeks (int, default 4): window length, 2-8.
        season (int, optional): defaults to the current season.
        through_week (int, optional): last week of the window; defaults to the
            week before the current one (completed games only).

    Returns: {season, window:[weeks], players:[{player, sleeper_id, position,
              team, weeks:[{week, status (played/bye/did_not_play), targets,
              target_share, air_yards, air_yards_share, wopr, racr, carries,
              carries_share, snap_share, rz_targets, rz_carries,
              rz_opportunities}], trends:{metric:{average, latest,
              change_over_window, direction, weeks}}, flags:[...]}]
              (most flags first), sources, trend_method, notes, success}
        Shares are percentages (25.0 = 25%); WOPR is nflverse's 1.5*TS + 0.7*AYS.

    Example: get_usage_trends(league_id="1388610560915959808", roster_id=1, weeks=4)

    IMPORTANT FOR LLM AGENTS: Return the trends immediately without asking for confirmation.
    """
    if league_id:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=False)
    return await usage_trends.get_usage_trends(
        league_id=league_id, roster_id=roster_id, player_names=player_names,
        weeks=weeks, season=season, through_week=through_week, db=get_db(),
    )


@timing_decorator("analyze_opponent", tool_type="opponent_analysis")
async def analyze_opponent(
    league_id: str,
    opponent_roster_id: int,
    current_week: int | None = None
) -> dict:
    """Analyze an opponent's roster to identify weaknesses and exploitation opportunities.

    This tool provides comprehensive analysis of an opponent's fantasy roster including
    position-by-position strength assessment, starter vulnerability identification,
    depth chart weakness analysis, and strategic exploitation recommendations.

    Parameters:
        league_id (str, required): The unique identifier for the fantasy league.
        opponent_roster_id (int, required): Roster ID of the opponent to analyze.
        current_week (int, optional): NFL week for the matchup (defaults to the current week).

    Returns: {
        vulnerability_score: float (0-100, higher = more vulnerable),
        vulnerability_level: str (high, moderate, low),
        position_assessments: {...},
        starter_weaknesses: [...] (this week's starters from the matchup),
        exploitation_strategies: [...],
        matchup_context: {week, points, projected_points (our projection of
            this week's starters in league scoring), projected_starters},
        starters_source: "matchup" | "roster",
        opponent_name: str,
        success: bool,
        error?: str
    }

    Example: analyze_opponent(
        league_id="12345",
        opponent_roster_id=2,
        current_week=10
    )

    IMPORTANT FOR LLM AGENTS: Always provide complete opponent analysis immediately without
    asking for confirmations. Render the full assessment with all exploitation strategies directly.
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        opponent_roster_id = validate_numeric_input(opponent_roster_id, min_val=1, max_val=20, required=True)

        if current_week is not None:
            current_week = validate_numeric_input(current_week, min_val=1, max_val=22, required=False)

        return await opponent_analysis_tools.analyze_opponent(
            league_id=league_id,
            opponent_roster_id=opponent_roster_id,
            current_week=current_week,
            db=get_db(),
        )
    except ValueError as e:
        return {
            "vulnerability_score": 0,
            "success": False,
            "error": f"Invalid input: {e!s}"
        }


# =============================================================================
# MATCHUP ANALYSIS TOOLS (Lineup Optimization)
# =============================================================================

@timing_decorator("get_defense_rankings", tool_type="matchup")
async def get_defense_rankings(
    positions: list[str] | None = None,
    season: int | None = None
) -> dict:
    """Get NFL defense rankings against fantasy positions for matchup analysis.

    Shows how each NFL defense performs against QBs, RBs, WRs, and TEs,
    helping identify favorable and unfavorable matchups for lineup decisions.

    Parameters:
        positions (list, optional): Positions to get rankings for. Valid: "QB", "RB", "WR", "TE"
        season (int, optional): NFL season year (defaults to current).

    Returns: {
        rankings: dict mapping position to list of team rankings,
        positions: list of positions included,
        season: int,
        tiers_explained: dict explaining matchup tiers,
        success: bool,
        error?: str
    }

    Example: get_defense_rankings(positions=["WR", "RB"])

    IMPORTANT FOR LLM AGENTS: Always provide complete defense rankings immediately without
    asking for confirmations. Render the full analysis with matchup tiers directly.
    """
    return await matchup_tools.get_defense_rankings(
        positions=positions,
        season=season
    )


@timing_decorator("get_matchup_difficulty", tool_type="matchup")
async def get_matchup_difficulty(
    position: str,
    opponent_team: str,
    include_rankings: bool = False
) -> dict:
    """Get matchup difficulty for a specific position vs opponent defense.

    Analyzes how the opponent defense performs against the given position
    and provides a recommendation for lineup decisions.

    Parameters:
        position (str, required): Fantasy position - "QB", "RB", "WR", or "TE"
        opponent_team (str, required): Opponent team abbreviation (e.g., "KC", "SF", "DAL")
        include_rankings (bool, default False): Whether to include full position rankings

    Returns: {
        matchup: {rank, rank_display, matchup_tier, tier_indicator, recommendation},
        position_rankings?: list (if include_rankings=True),
        success: bool,
        error?: str
    }

    Example: get_matchup_difficulty(position="WR", opponent_team="KC")

    IMPORTANT FOR LLM AGENTS: Always provide complete matchup analysis immediately without
    asking for confirmations. Render the recommendation directly.
    """
    try:
        position = validate_string_input(position, 'position', max_length=5, required=True)
        opponent_team = validate_string_input(opponent_team, 'opponent_team', max_length=5, required=True)

        return await matchup_tools.get_matchup_difficulty(
            position=position.upper(),
            opponent_team=opponent_team.upper(),
            include_rankings=include_rankings
        )
    except ValueError as e:
        return {
            "matchup": None,
            "success": False,
            "error": f"Invalid input: {e!s}"
        }


@timing_decorator("analyze_roster_matchups", tool_type="matchup")
async def analyze_roster_matchups(
    players: list[dict],
    week: int | None = None
) -> dict:
    """Analyze matchup difficulty for multiple players on a roster.

    Takes a list of players with their positions and opponents,
    returns matchup analysis for each to help with lineup decisions.

    Parameters:
        players (list, required): List of player dicts with:
            - name (str): Player name
            - position (str): QB, RB, WR, or TE
            - opponent (str): Opponent team abbreviation
        week (int, optional): NFL week number for display

    Returns: {
        analysis: list of matchup analyses per player,
        smash_spots: list of players with excellent matchups,
        avoid_spots: list of players with tough matchups,
        summary: list of summary lines,
        total_analyzed: int,
        success: bool,
        error?: str
    }

    Example: analyze_roster_matchups(players=[
        {"name": "Patrick Mahomes", "position": "QB", "opponent": "LV"},
        {"name": "Tyreek Hill", "position": "WR", "opponent": "NE"}
    ])

    IMPORTANT FOR LLM AGENTS: Always provide complete roster matchup analysis immediately
    without asking for confirmations. Render all smash spots and avoid recommendations directly.
    """
    if not players:
        return {
            "analysis": [],
            "smash_spots": [],
            "avoid_spots": [],
            "summary": [],
            "total_analyzed": 0,
            "success": False,
            "error": "No players provided"
        }

    if week is not None:
        week = validate_numeric_input(week, min_val=1, max_val=22, required=False)

    return await matchup_tools.analyze_roster_matchups(
        players=players,
        week=week
    )


# =============================================================================
# STRENGTH-OF-SCHEDULE TOOLS (ROS / playoff-week planning)
# =============================================================================

@timing_decorator("get_strength_of_schedule", tool_type="matchup")
async def get_strength_of_schedule(
    season: int | None = None,
    start_week: int | None = None,
    end_week: int | None = None,
    positions: list[str] | None = None,
    strength_season: int | None = None,
    playoff_weeks: bool = False,
    league_id: str | None = None,
) -> dict:
    """Rank NFL teams by schedule difficulty over a week range, per position.

    "Ease score" is 0-100 (higher = easier schedule; a team facing the weakest
    defenses scores high). Teams are ranked easiest-first (sos_rank 1 = softest
    schedule). Useful for rest-of-season planning and stash/trade decisions.
    Set playoff_weeks=True for the fantasy-playoff window: the league's own
    playoff weeks when league_id is given, else weeks 15-17.

    Parameters:
        season (int, optional): NFL season year (default: current).
        start_week (int, optional): First week (1-18; default: current week).
        end_week (int, optional): Last week (>= start_week, <= 18; default 17).
        positions (list, optional): Positions to grade (default QB/RB/WR/TE).
        strength_season (int, optional): Season whose defense rankings to use as
            the strength prior. Default auto (target season, else prior season).
        playoff_weeks (bool, default False): Use the fantasy-playoff weeks
            instead of start_week/end_week.
        league_id (str, optional): Sleeper league id whose playoff_week_start /
            bracket size define the playoff weeks.

    Returns: {
        season, weeks, positions, window ("custom" | "playoffs"),
        strength_source_season, strength_is_fallback,
        by_position: {pos: [teams easiest-first with sos_rank/ease_score/weeks]},
        overall: [teams easiest-first],
        success: bool, error?: str
    }

    Example: get_strength_of_schedule(start_week=4, end_week=14)
    Example: get_strength_of_schedule(playoff_weeks=True, league_id="123")

    IMPORTANT FOR LLM AGENTS: Always compute and render the full ranking
    immediately without asking for confirmation.
    """
    from .week_context import current_season_week

    if season is None or (start_week is None and not playoff_weeks):
        current = await current_season_week(get_db())
        season = season or current["season"]
        if start_week is None:
            start_week = current["week"]
    window = "custom"
    if playoff_weeks:
        window = "playoffs"
        start_week, end_week = sos_tools.PLAYOFF_WEEKS[0], sos_tools.PLAYOFF_WEEKS[-1]
        if league_id:
            league = ((await sleeper_tools.get_league(str(league_id))) or {}).get("league") or {}
            first, last = ros.playoff_window(league.get("settings") or {})
            if first:
                start_week, end_week = first, last
    if end_week is None:
        end_week = max(int(start_week), 17)
    result = await sos_tools.get_strength_of_schedule(
        season=season,
        start_week=start_week,
        end_week=end_week,
        positions=positions,
        strength_season=strength_season,
    )
    if isinstance(result, dict):
        result["window"] = window
    return result


# =============================================================================
# STREAMING PLANNER (weekly DST / K / QB / TE matchup lookahead)
# =============================================================================

@timing_decorator("get_streaming_options", tool_type="matchup")
async def get_streaming_options(
    season: int,
    start_week: int,
    weeks_ahead: int = 3,
    positions: list[str] | None = None,
    strength_season: int | None = None,
    top_n: int = 8,
    league_id: str | None = None,
    only_available: bool = False,
    scoring: str | None = None,
) -> dict:
    """Rank weekly streaming options per position over the next 1-4 weeks.

    The reliable weekly waiver edge: which DST/K/QB/TE to stream based on
    matchup over a short lookahead. `stream_score` is 0-100 (higher = better);
    options are ranked best-first (`stream_rank` 1 = top stream). Signals are
    schedule-based and key-free: QB/RB/WR/TE = softer opponent defense; DST =
    weaker opponent offense; K = stronger own offense.

    Pass `league_id` to annotate each option with free-agent availability (clean
    for DST; K/QB/TE/RB/WR list the team's players at that position), and
    `only_available=True` to keep only options with a free-agent streamer.

    Parameters:
        season (int, required): NFL season year.
        start_week (int, required): First week of the window (1-18).
        weeks_ahead (int, optional): Weeks to look ahead incl. start (1-4, default 3).
        positions (list, optional): Positions to plan (default QB/TE/DST/K).
        strength_season (int, optional): Rankings-prior season (default auto).
        top_n (int, optional): Max options per position (default 8; 0 = all).
        league_id (str, optional): Sleeper league id for free-agent availability.
        only_available (bool, optional): keep only free-agent-available options.
        scoring (str, optional): preset for DST/K `projected_points` when no
            league_id is given; with league_id the league's own K distance and
            DEF points-allowed values are used (`scoring_used`).

    Returns: {
        season, weeks, positions,
        defense_source_season, defense_is_fallback,
        offense_source_season, offense_is_fallback, availability_active,
        streaming_options: {pos: [teams best-first with stream_rank/stream_score/weeks/availability]},
        notes: [str], success: bool, error?: str
    }

    Example: get_streaming_options(season=2026, start_week=10, positions=["DST", "K"],
                                   league_id="123456789", only_available=True)

    IMPORTANT FOR LLM AGENTS: Compute and render the full ranking immediately
    without asking for confirmation.
    """
    return await streaming_tools.get_streaming_options(
        season=season,
        start_week=start_week,
        weeks_ahead=weeks_ahead,
        positions=positions,
        strength_season=strength_season,
        top_n=top_n,
        league_id=league_id,
        only_available=only_available,
        scoring=scoring,
    )


# =============================================================================
# WEATHER / WIND (game-environment analysis)
# =============================================================================

@timing_decorator("get_weather_forecast", tool_type="matchup")
async def get_weather_forecast(
    season: int,
    week: int,
    teams: list[str] | None = None,
) -> dict:
    """Per-game weather forecast + fantasy impact for an NFL week (Open-Meteo, no key).

    Wind above ~15 mph fades passing and (especially) kicking; dome games are
    neutral. Games are returned worst-weather-first so you can fade passing/K in
    the ugly spots. Schedule-based, no API key.

    Parameters:
        season (int, required): NFL season year.
        week (int, required): Regular-season week (1-18).
        teams (list, optional): Team abbreviations to filter to (either side).

    Returns: {
        season, week, count,
        games: [{home, away, kickoff, stadium, dome, wind_mph, precip_in,
                 temp_f, impact:{severity, passing, kicking, running, note}}]
                 (worst-weather-first),
        success: bool, error?: str
    }

    Example: get_weather_forecast(season=2026, week=16)

    IMPORTANT FOR LLM AGENTS: Compute and render the full forecast immediately
    without asking for confirmation.
    """
    return await weather_tools.get_weather_forecast(
        season=season,
        week=week,
        teams=teams,
    )


# =============================================================================
# LINEUP OPTIMIZER TOOLS (Start/Sit Recommendations)
# =============================================================================

@timing_decorator("get_start_sit_recommendation", tool_type="lineup")
async def get_start_sit_recommendation(
    player_name: str,
    position: str,
    team: str,
    opponent: str = "",
    player_id: str | None = None,
    target_share: float | None = None,
    snap_percentage: float | None = None,
    injury_status: str | None = None,
    practice_status: str | None = None,
    projected_points: float | None = None,
    scoring: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
    week: int | None = None,
) -> dict:
    """Get a start/sit recommendation for a single player.

    Analyzes matchup difficulty, usage trends, health status, and projections
    to provide a confidence-weighted recommendation.

    Parameters:
        player_name (str, required): Player's full name
        position (str, required): Fantasy position (QB, RB, WR, TE, K, DEF).
            K and DEF are priced off Vegas totals (own for K, opponent's for
            DEF) or, without live lines, the season's scoring; their matchup
            is an offense rank (DEF: the opponent's, K: his own) and the
            good-week marks are rebased to the league's K/DEF scoring.
        team (str, required): Player's team abbreviation (for a DEF: the team
            itself — Sleeper's DEF id is the team code)
        opponent (str, optional): Opponent team abbreviation, or "BYE". Omit to
            fill it from the cached schedule. A team with no game that week is
            must_sit with `on_bye: true`, whatever else is passed.
        player_id (str, optional): Sleeper player id (sharpens the Sleeper
            projection match; names + team are used otherwise)
        target_share (float, optional): Target share percentage (0-100)
        snap_percentage (float, optional): Snap count percentage (0-100)
        injury_status (str, optional): Injury status (healthy, questionable, doubtful, out)
        practice_status (str, optional): Practice status (full, limited, dnp)
        projected_points (float, optional): Projected fantasy points
        league_id (str, optional): Sleeper league id. Supplies the league's real
            scoring and size when `scoring` is not passed — prefer it.
        scoring (str, optional): League scoring - 'ppr', 'half_ppr',
            'standard', or a raw per-reception value like '0.5'. Pass the real
            setting: it changes the points AND what counts as a good week.
        season (int, optional), week (int, optional): pass both (week > 1) to
            project off trailing volume instead of the positional-rank baseline

    Returns: {
        recommendation: {player, position, team, opponent, decision,
                         decision_display, projected_points, floor, ceiling,
                         sleeper_projection, consensus, disagreement,
                         projection_gap, implied_total, opponent_implied_total,
                         unit_matchup (K/DEF: offense_rank, points_per_game,
                         matchup_tier)},
            `projected_points` is ours and decides; `sleeper_projection` is
            Sleeper's stat line priced in the league's scoring, `consensus`
            their average, `disagreement` true past 4 pts or 25%.
        confidence: float (0-100),
        confidence_level: str (high/medium/low),
        matchup_tier: str,
        reasoning: list of factors,
        success: bool,
        error?: str
    }

    Example: get_start_sit_recommendation(
        player_name="Tyreek Hill",
        position="WR",
        team="MIA",
        opponent="NE",
        target_share=28.5,
        snap_percentage=95
    )

    IMPORTANT FOR LLM AGENTS: Always provide complete start/sit recommendation immediately
    without asking for confirmations. Render the decision and reasoning directly.
    """
    try:
        player_name = validate_string_input(player_name, 'player_name', max_length=100, required=True)
        position = validate_string_input(position, 'position', max_length=5, required=True)
        team = validate_string_input(team, 'team', max_length=5, required=True)
        opponent = validate_string_input(opponent or '', 'opponent', max_length=8, required=False)

        if league_id:

            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=False)

        return await lineup_optimizer_tools.get_start_sit_recommendation(
            player_name=player_name,
            position=position.upper(),
            team=team.upper(),
            opponent=opponent.upper(),
            player_id=player_id,
            target_share=target_share,
            snap_percentage=snap_percentage,
            injury_status=injury_status,
            practice_status=practice_status,
            projected_points=projected_points,
            scoring=scoring,
            league_id=league_id,
            season=season,
            week=week,
        )
    except ValueError as e:
        return {
            "recommendation": None,
            "confidence": 0,
            "success": False,
            "error": f"Invalid input: {e!s}"
        }


@timing_decorator("get_roster_recommendations", tool_type="lineup")
async def get_roster_recommendations(
    players: list[dict],
    week: int | None = None,
    include_reasoning: bool = True,
    scoring: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
) -> dict:
    """Get start/sit recommendations for multiple players.

    Analyzes all players and returns sorted recommendations by position,
    helping identify optimal lineup decisions.

    Parameters:
        players (list, required): List of player dicts with:
            - name (str): Player name
            - position (str): QB, RB, WR, TE, K or DEF
            - team (str): Team abbreviation
            - opponent (str): Opponent team abbreviation
            - usage (dict, optional): {target_share, snap_percentage}
            - injury (dict, optional): {status, practice_status}
            - projection (dict, optional): {projected_points}
        week (int, optional): NFL week - with `season` and week > 1 this selects
            the opportunity baseline for the projections, not just a response label
        include_reasoning (bool, default True): Whether to include detailed reasoning
        league_id (str, optional): Sleeper league id. Supplies the league's real
            scoring and size when `scoring` is not passed — prefer it.
        scoring (str, optional): League scoring - 'ppr', 'half_ppr',
            'standard', or a raw per-reception value like '0.5'
        season (int, optional): Season year, needed with `week`

    Returns: {
        recommendations: list of player analyses sorted by projected points
            (confidence breaks ties),
        by_position: dict of recommendations grouped by position, same order,
        must_starts: list of must-start players,
        sits: list of players to sit,
        summary: list of summary lines,
        success: bool,
        error?: str
    }

    Example: get_roster_recommendations(players=[
        {"name": "Patrick Mahomes", "position": "QB", "team": "KC", "opponent": "LV"},
        {"name": "Tyreek Hill", "position": "WR", "team": "MIA", "opponent": "NE",
         "usage": {"target_share": 28, "snap_percentage": 95}}
    ])

    IMPORTANT FOR LLM AGENTS: Always provide complete roster recommendations immediately
    without asking for confirmations. Render must starts and sits directly.
    """
    if not players:
        return {
            "recommendations": [],
            "by_position": {},
            "must_starts": [],
            "sits": [],
            "summary": [],
            "total_analyzed": 0,
            "success": False,
            "error": "No players provided"
        }

    if week is not None:
        week = validate_numeric_input(week, min_val=1, max_val=22, required=False)

    if league_id:

        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=False)

    return await lineup_optimizer_tools.get_roster_recommendations(
        players=players,
        week=week,
        include_reasoning=include_reasoning,
        scoring=scoring,
        league_id=league_id,
        season=season,
    )


@timing_decorator("compare_players_for_slot", tool_type="lineup")
async def compare_players_for_slot(
    players: list[dict],
    slot: str = "FLEX",
    scoring: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
    week: int | None = None,
) -> dict:
    """Compare multiple players competing for the same roster slot.

    Useful for deciding between players for a specific position or flex spot.
    Returns a ranked comparison with the recommended starter.

    Parameters:
        players (list, required): List of player dicts to compare (2-5 players)
            Each should have: name, position, team, opponent
            Optional: usage, injury, projection dicts, player_id (Sleeper)
            Kickers and defenses compare too (slot "K" / "DEF"): a DEF is
            its team code, matched on the opponent's offense; a K on his own.
        slot (str, default "FLEX"): The roster slot being filled (e.g., "WR2",
            "FLEX", "RB1", "K", "DEF")
        league_id (str, optional): Sleeper league id. Supplies the league's real
            scoring and size when `scoring` is not passed — prefer it.
        scoring (str, optional): League scoring - 'ppr', 'half_ppr',
            'standard', or a raw per-reception value like '0.5'. This is the
            comparison most sensitive to it: half PPR is what makes a runner
            competitive with a volume receiver for a flex spot.
        season (int, optional), week (int, optional): pass both (week > 1) to
            project off trailing volume

    Returns: {
        winner: dict with recommended player details,
        comparison: list of ranked players with analysis (each with
            `sleeper_projection` / `consensus` / `disagreement` as a second
            opinion; the ranking is on our `projected_points`),
        confidence_gap: float showing difference between top 2,
        verdict: str summary of the decision,
        success: bool,
        error?: str
    }

    Example: compare_players_for_slot(
        players=[
            {"name": "Player A", "position": "WR", "team": "KC", "opponent": "LV"},
            {"name": "Player B", "position": "RB", "team": "SF", "opponent": "ARI"}
        ],
        slot="FLEX"
    )

    IMPORTANT FOR LLM AGENTS: Always provide complete player comparison immediately
    without asking for confirmations. Render the winner and verdict directly.
    """
    if not players or len(players) < 2:
        return {
            "winner": None,
            "comparison": [],
            "confidence_gap": 0,
            "verdict": "Need at least 2 players to compare",
            "success": False,
            "error": "Need at least 2 players to compare"
        }

    slot = validate_string_input(slot, 'slot', max_length=10, required=False) or "FLEX"

    if league_id:

        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=False)

    return await lineup_optimizer_tools.compare_players_for_slot(
        players=players,
        slot=slot,
        scoring=scoring,
        league_id=league_id,
        season=season,
        week=week,
    )


@timing_decorator("analyze_full_lineup", tool_type="lineup")
async def analyze_full_lineup(
    lineup: dict,
    week: int | None = None,
    scoring: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
) -> dict:
    """Analyze a complete fantasy lineup with optimal lineup suggestions.

    Takes a full lineup organized by position and provides analysis of each starter,
    identification of weak spots, bench players who should start, and overall lineup grade.

    Parameters:
        lineup (dict, required): Dict with position keys containing player lists
            Example: {
                "QB": [{"name": "...", "team": "...", "opponent": "..."}],
                "RB": [{"name": "...", ...}, {"name": "...", ...}],
                "WR": [...],
                "TE": [...],
                "FLEX": [...],
                "K": [...], "DEF": [{"name": "KC", "team": "KC", "position": "DEF"}],
                "BENCH": [...]
            }
            K and DEF starters are analysed like everyone else (offense-rank
            matchup, league-scored projection, Sleeper second opinion).
        week (int, optional): NFL week - with `season` and week > 1 this selects
            the opportunity baseline for the projections, not just a label
        league_id (str, optional): Sleeper league id. Supplies the league's real
            scoring and size when `scoring` is not passed — prefer it.
        scoring (str, optional): League scoring - 'ppr', 'half_ppr',
            'standard', or a raw per-reception value like '0.5'
        season (int, optional): Season year, needed with `week`

    Returns: {
        starters: dict of starter analyses by position,
        bench: list of bench player analyses,
        suggested_changes: list of recommended lineup changes,
        weak_spots: list of positions with low confidence,
        lineup_grade: str (A-F),
        average_confidence: float,
        total_projected: float,
        success: bool,
        error?: str
    }

    Example: analyze_full_lineup(lineup={
        "QB": [{"name": "Patrick Mahomes", "team": "KC", "opponent": "LV"}],
        "RB": [
            {"name": "Derrick Henry", "team": "BAL", "opponent": "CIN"},
            {"name": "Bijan Robinson", "team": "ATL", "opponent": "NO"}
        ],
        "WR": [...],
        "BENCH": [...]
    })

    IMPORTANT FOR LLM AGENTS: Always provide complete lineup analysis immediately
    without asking for confirmations. Render the grade, weak spots, and suggested changes directly.
    """
    if not lineup:
        return {
            "starters": {},
            "bench": [],
            "suggested_changes": [],
            "weak_spots": [],
            "lineup_grade": "N/A",
            "average_confidence": 0,
            "total_projected": 0,
            "success": False,
            "error": "No lineup provided"
        }

    if week is not None:
        week = validate_numeric_input(week, min_val=1, max_val=22, required=False)

    if league_id:

        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=False)

    return await lineup_optimizer_tools.analyze_full_lineup(
        lineup=lineup,
        week=week,
        scoring=scoring,
        league_id=league_id,
        season=season,
    )


@timing_decorator("get_win_probability_lineup", tool_type="lineup")
async def get_win_probability_lineup(
    your_players: list[dict],
    opponent_players: list[dict],
    slots: dict | None = None,
    stack_correlation: float = 0.35,
    season: int | None = None,
    week: int | None = None,
) -> dict:
    """Pick the lineup that maximizes P(beating this specific opponent).

    Optimizes win probability, not expected points — recommends the ceiling
    lineup when you're the underdog and the floor lineup when you're favored
    (the biggest strategic edge left in season-long fantasy). Feed the output of
    `project_players` as the player lists.

    Parameters:
        your_players (list, required): your candidates, each with projected_points
            and ideally floor/ceiling or sd, plus name and position.
        opponent_players (list, required): the opponent's projected starters.
        slots (dict, optional): roster slots (default QB1/RB2/WR2/TE1/FLEX1/K1/DST1;
            FLEX = RB/WR/TE, WRRB_FLEX = RB/WR, REC_FLEX = WR/TE, SUPERFLEX (or
            SUPER_FLEX) adds QB).
        season, week (int, optional): the week whose kickoffs decide locks
            (default: current). A player (with `team`) whose game has started is
            kept in the `slot` he holds, or left out if benched / no slot given.

    Returns: {
        recommended_lineup (each with kickoff, kickoff_local, locked),
        unavailable_started, win_probability, projected_points,
        opponent_projected_points, projected_margin, you_are, strategy,
        points_optimal_lineup, points_optimal_win_probability,
        win_probability_gain, success, error?
    }

    Example: get_win_probability_lineup(your_players=[...], opponent_players=[...])

    IMPORTANT FOR LLM AGENTS: Compute and render the recommendation immediately
    without asking for confirmation.
    """
    return await win_probability.get_win_probability_lineup(
        your_players=your_players,
        opponent_players=opponent_players,
        slots=slots,
        stack_correlation=stack_correlation,
        season=season,
        week=week,
    )


# =============================================================================
# VEGAS LINES TOOLS
# =============================================================================

@timing_decorator("get_vegas_lines", tool_type="vegas")
async def get_vegas_lines(
    teams: list[str] | None = None,
    week: int | None = None,
    season: int | None = None,
    league_id: str | None = None,
    roster_id: int | None = None,
    user_id: str | None = None,
) -> dict:
    """Vegas spreads, totals and implied team totals — per game, per team, or
    for every player on YOUR roster.

    - No arguments: every published game, highest total first.
    - teams: only games involving those teams, plus `team_environments`
      {team: {opponent, spread, total, implied_total, is_favorite,
      environment tier, game_script, recommendations}} (what the old
      get_game_environment returned).
    - league_id + roster_id (or user_id): also `roster` — each rostered
      player's game environment and position boost, best/worst environments
      (the old analyze_roster_vegas, with teams/opponents looked up for you).

    The sportsbook publishes more than one week at a time, so every game
    carries the NFL `week` it belongs to. Pass `week` when reasoning about a
    single slate. Without ODDS_API_KEY the values are neutral placeholders
    (`is_fallback`), and the summary says so.

    Parameters:
        teams: Team abbreviations to filter to, any spelling
        week: NFL week to restrict games to
        season: Season for the week lookup (default: current)
        league_id, roster_id, user_id: Your Sleeper league and roster for the
            per-player roster view

    Returns: {games [...each with week], total_games, shootout_games,
              high_scoring_games, summary, team_environments?, roster?
              {analysis, best_environments, worst_environments, summary,
              is_fallback, on_bye}, success}

    Example: get_vegas_lines(week=3)
    Example: get_vegas_lines(teams=["KC", "BUF"])
    Example: get_vegas_lines(league_id="123", roster_id=7)
    """
    import asyncio

    from .roster_context import load_roster_players

    result = await vegas_tools.get_vegas_lines(teams=teams, week=week, season=season)
    if not isinstance(result, dict):
        return result
    if teams:
        valid = [t for t in teams[:8] if isinstance(t, str) and t.strip()]
        envs = await asyncio.gather(*(vegas_tools.get_game_environment(team=t) for t in valid),
                                    return_exceptions=True)
        result["team_environments"] = {
            (env.get("team") or t): {k: v for k, v in env.items()
                                     if k not in ("game", "success", "error", "error_type")}
            for t, env in zip(valid, envs, strict=True)
            if isinstance(env, dict) and env.get("success", True)
        }
    if league_id and (roster_id is not None or user_id):
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        except ValueError as e:
            return {**result, "success": False, "error": f"Invalid input: {e!s}"}
        ctx = await load_roster_players(league_id, roster_id, user_id, db=get_db(),
                                        season=season, week=week)
        if ctx["error"]:
            result["roster"] = {"error": ctx["error"]}
        else:
            playing = [p for p in ctx["players"] if p["opponent"] != "BYE"]
            roster = await vegas_tools.analyze_roster_vegas(players=[
                {"name": p["name"], "team": p["team"], "position": p["position"],
                 "opponent": p["opponent"] or None} for p in playing]) if playing else {"analysis": []}
            roster = {k: v for k, v in (roster or {}).items()
                      if k not in ("success", "error", "error_type")}
            roster["on_bye"] = [p["name"] for p in ctx["players"] if p["opponent"] == "BYE"]
            roster["roster_id"] = ctx["roster_id"]
            result["roster"] = roster
    return result


@timing_decorator("get_stack_opportunities", tool_type="vegas")
async def get_stack_opportunities(
    min_total: float | None = 48.0
) -> dict:
    """Identify high-total games for stacking opportunities.

    Finds games with the highest over/under totals, which are
    ideal for QB + pass catcher stacks in DFS or season-long leagues.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        min_total: Minimum total to consider (default 48.0)

    Returns:
        Dictionary containing:
        - stacks: List of high-total games with stack recommendations
        - summary: Quick summary of best stacking opportunities

    Example:
        get_stack_opportunities()
        -> Returns games with O/U >= 48 for stacking

        get_stack_opportunities(min_total=50)
        -> Returns only games with O/U >= 50
    """
    try:
        min_total_val = float(min_total) if min_total is not None else 48.0
        min_total_val = max(35.0, min(60.0, min_total_val))  # Clamp to reasonable range
    except (ValueError, TypeError):
        min_total_val = 48.0

    return await vegas_tools.get_stack_opportunities(min_total=min_total_val)


# =============================================================================
# INJURY REPORT TOOLS (Multi-source aggregation with confidence scoring)
# =============================================================================

@timing_decorator("get_injury_report", tool_type="injury")
async def get_injury_report(
    teams: list[str] | None = None,
    player_ids: list[str] | None = None,
    min_confidence: int | None = None,
    severity: int | None = None,
    since: str | None = None,
    include_practice: bool | None = True,
    limit: int | None = None,
    use_cache: bool | None = True,
    team_ids: list[str] | None = None,
) -> dict:
    """Current injury report ("who is hurt"): all teams, some teams, or players.

    Rows come from ESPN's injury API (cached with an adaptive TTL), each with a
    status, severity (1-5) and this week's real practice line. For "what
    CHANGED" use get_injury_trends; for gameday inactives get_gameday_inactives.

    `confidence` is a source-agreement score. Only ESPN is wired in today (the
    CBS injury source is not implemented), so every row is single-source and
    confidence is uniform; min_confidence is kept for when a second source lands.

    Parameters:
        teams: Team abbreviations, any spelling (e.g. ["KC", "PHI"]); default all
        player_ids: ESPN player ids to look up individually (instead of teams)
        min_confidence: Keep rows with confidence >= this (0-100)
        severity: Keep rows with severity >= this (1 minor .. 5 IR/season-ending;
            3+ = likely to miss games)
        since: ISO date/time; keep rows reported on or after it
        include_practice: Attach this week's practice report (default True)
        limit: Max rows (default all)
        use_cache: Use cached reports (default True)
        team_ids: Deprecated alias of `teams`

    Returns: {
        injuries: [{
            player_id, player_name, team_id, position,
            injury_status, injury_type, injury_description,
            game_status, severity (1-5), confidence (0-100),
            sources, date_reported,
            practice_status (DNP/LP/FP/REST or null), practice_pattern
            ("DNP-LP-FP"), practice_trend (improving/worsening/steady/
            single_report), practice_days [{date, day, status, source}],
            practice_source ("nfl.com" official report, "espn_news" dated
            news note, or null = no report published — never inferred)
        }],
        total_injuries, filters, cache_used, practice_week,
        success, error?
    }

    Example: get_injury_report(teams=["KC", "PHI"])
    Example: get_injury_report(severity=3, since="2026-09-20")
    Example: get_injury_report(player_ids=["4428633", "4241479"])
    """
    from .injury_service import InjuryAggregator, get_injury_reports

    try:
        use_cache_val = bool(use_cache) if use_cache is not None else True
        team_list = teams or team_ids
        results = []

        if player_ids:
            async with InjuryAggregator(db=get_db()) as aggregator:
                for pid in player_ids[:50]:  # Limit to 50 players
                    injury = await aggregator.get_player_injury(str(pid))
                    if injury:
                        results.append(injury.to_dict())
        elif team_list:
            valid_teams = [normalize_team(t) or t.upper() for t in team_list[:32]
                           if isinstance(t, str) and len(t) <= 5]
            if valid_teams:
                results = await get_injury_reports(teams=valid_teams, db=get_db(), use_cache=use_cache_val)
        else:
            results = await get_injury_reports(db=get_db(), use_cache=use_cache_val)

        filters = {}
        if min_confidence is not None:
            min_conf = max(0, min(100, int(min_confidence)))
            filters["min_confidence"] = min_conf
            results = [r for r in results if (r.get("confidence") or 0) >= min_conf]
        if severity is not None:
            min_sev = max(1, min(5, int(severity)))
            filters["severity"] = min_sev
            results = [r for r in results if (r.get("severity") or 0) >= min_sev]
        if since:
            cutoff = str(since).strip()
            filters["since"] = cutoff
            # ISO strings compare chronologically; a bare date covers the whole day.
            results = [r for r in results if str(r.get("date_reported") or "") >= cutoff]
        if limit is not None:
            results = results[:max(1, int(limit))]

        practice_week = None
        if include_practice is not False and results:
            practice_week = await _attach_practice(results)

        return {
            "injuries": results,
            "total_injuries": len(results),
            "filters": filters,
            "cache_used": use_cache_val,
            "practice_week": practice_week,
            "success": True
        }

    except Exception as e:
        return {
            "injuries": [],
            "total_injuries": 0,
            "cache_used": False,
            "success": False,
            "error": str(e)
        }


async def _current_season_week() -> tuple[int | None, int | None]:
    """Season and week from Sleeper's NFL state, (None, None) on failure."""
    try:
        state = await sleeper_tools.get_nfl_state()
        st = (state or {}).get("nfl_state") or {}
        return int(st.get("season") or 0) or None, int(st.get("week") or 0) or None
    except Exception:
        return None, None


async def _attach_practice(injuries: list[dict]) -> dict:
    """Add this week's reported practice line to each injury row (in place).

    Refreshes the stored reports at most hourly first, so the tool does not
    depend on the prefetch loop. A player without a report gets nulls.
    """
    from .practice_reports import lookup_practice, practice_fields, refresh_practice_reports

    db = get_db()
    season, week = await _current_season_week()
    try:
        await refresh_practice_reports(db, season, week)
    except Exception as e:
        logger.debug(f"practice refresh failed: {e}")
    reported = 0
    for inj in injuries:
        practice = lookup_practice(db, inj.get("player_name"), inj.get("team_id"),
                                   season=season, week=week)
        inj.update(practice_fields(practice))
        inj["practice_days"] = (practice or {}).get("days") or []
        if practice and practice.get("game_status"):
            inj["practice_report_game_status"] = practice["game_status"]
        reported += 1 if practice else 0
    return {"season": season, "week": week, "players_with_report": reported}


@timing_decorator("get_injury_trends", tool_type="injury")
async def get_injury_trends(
    lookback_hours: int | None = 168,
    teams: list[str] | None = None,
    direction: str | None = None,
    limit: int | None = 50,
) -> dict:
    """Get injury status CHANGES over a window - who got worse or recovered.

    Reads the recorded timeline rather than the current snapshot, so it answers
    "what moved since I last looked" instead of "who is hurt". A player's first
    sighting has no previous status and is reported as ``new``.

    Parameters:
        lookback_hours: Window to look back (default 168 = 7 days)
        teams: Team abbreviations to filter
        direction: "worse", "better" or "new" to filter; omit for all
        limit: Max changes to return

    Returns: {
        changes: [{player_name, team_id, position, previous_status,
                   injury_status, direction, severity_delta, recorded_at, ...}],
        total_changes, lookback_hours, success, error?
    }

    Example: get_injury_trends()
    Example: get_injury_trends(lookback_hours=48, direction="worse")
    """
    from datetime import UTC, datetime, timedelta

    from .injury_service import STATUS_SEVERITY

    try:
        hours = max(1, min(int(lookback_hours or 168), 24 * 30))
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        teams_list = [normalize_team(t) or t.upper() for t in (teams or [])[:10] if isinstance(t, str)]
        max_rows = max(1, min(int(limit or 50), 500))

        # The direction filter runs in SQL: applying it to an already-truncated
        # page returns nothing whenever the window opens with a bulk backfill.
        rows = get_db().get_injury_status_changes(
            since=since, teams=teams_list or None, limit=max_rows,
            direction=direction, severity_map=STATUS_SEVERITY,
        )

        changes = []
        for row in rows:
            prev = row.get("previous_status")
            new_sev = int(STATUS_SEVERITY.get(row.get("injury_status"), 3))
            if prev is None:
                row_direction, delta = "new", None
            else:
                old_sev = int(STATUS_SEVERITY.get(prev, 3))
                delta = new_sev - old_sev
                # Same severity bucket with a different label (e.g. a changed
                # body part) is a re-report, not a move in either direction.
                row_direction = "worse" if delta > 0 else "better" if delta < 0 else "lateral"
            changes.append({**row, "direction": row_direction, "severity_delta": delta})

        # No post-filter on `direction`: the query already applied it, and doing
        # it again here is what capped the result at "whatever survived the page".

        return {
            "changes": changes,
            "total_changes": len(changes),
            "lookback_hours": hours,
            "success": True,
            "error": None,
        }

    except Exception as e:
        return {
            "changes": [],
            "total_changes": 0,
            "lookback_hours": lookback_hours,
            "success": False,
            "error": f"Failed to get injury trends: {e}",
        }


@timing_decorator("get_gameday_inactives", tool_type="injury")
async def get_gameday_inactives(
    teams: list[str] | None = None,
    severity_threshold: int | None = 3,
    league_id: str | None = None,
    roster_id: int | None = None,
    season: int | None = None,
    week: int | None = None,
) -> dict:
    """Get gameday inactives: the published list once it is out, the injury
    report's likely-outs before that.

    Teams declare inactives ~90 minutes before kickoff. For games starting
    within 2h, in progress or final, the published inactives are read
    (ESPN gameday notes "X is inactive for Sunday's game", plus Sleeper's
    `Inactive` status) and marked `official: true`. For every other team — or
    a team whose list has not been published yet — the old behaviour is the
    fallback: injuries at or above `severity_threshold`, marked
    `official: false` with `basis: "injury_report_severity"`. That is a
    forecast, not the inactive list.

    Severity scale (fallback only):
    - 1: Minor (day-to-day)
    - 2: Questionable (game-time decision)
    - 3: Moderate (expected to miss 1-2 weeks)
    - 4: Significant (multi-week absence)
    - 5: Severe (IR/season-ending)

    Parameters:
        teams: Team abbreviations to filter
        severity_threshold: Min severity for the fallback (3+ = likely out)
        league_id, roster_id (optional): pass both to flag YOUR starters who
            are inactive (or at risk before the list is out)
        season, week (optional): default to the current NFL week

    Returns: {
        inactives: [{player_name, team_id, injury_status, official, basis,
                     source, note?, severity?, confidence?}],
        total_inactives, severity_threshold_used,
        mode: "official" | "mixed" | "fallback_injury_report",
        official_published_teams, pending_teams, games {team: {kickoff, phase}},
        confirmed_active [{player_name, team_id, note}],
        my_starters? {inactive, at_risk, confirmed_active, not_yet_published},
        success, error?
    }

    Example: get_gameday_inactives()
    Example: get_gameday_inactives(league_id="1312017357782155264", roster_id=7)
    Example: get_gameday_inactives(teams=["KC", "SF"], severity_threshold=4)
    """
    from .gameday_inactives import get_official_inactives
    from .injury_service import get_injury_reports
    from .opportunity_tools import norm_name
    from .teams import normalize_team

    try:
        threshold = int(severity_threshold) if severity_threshold else 3
        threshold = max(1, min(5, threshold))

        teams_list = [normalize_team(t) or t.upper() for t in (teams or [])[:10] if isinstance(t, str)]
        if not season or not week:
            cur_season, cur_week = await _current_season_week()
            season, week = season or cur_season, week or cur_week

        official = {"games": {}, "window_teams": [], "inactives": [], "confirmed_active": []}
        if season and week:
            try:
                official = await get_official_inactives(get_db(), int(season), int(week), teams_list or None)
            except Exception as e:
                logger.warning(f"official inactives unavailable: {e}")
        published = sorted({r["team_id"] for r in official["inactives"]})
        pending = sorted(set(official.get("window_teams") or []) - set(published))

        injuries = await get_injury_reports(
            teams=teams_list if teams_list else None,
            db=get_db(),
            use_cache=True
        )

        inactives = []
        for row in official["inactives"]:
            inactives.append({
                "player_id": row.get("player_id"),
                "player_name": row.get("player_name"),
                "team_id": row.get("team_id"),
                "position": row.get("position"),
                "injury_status": "Inactive",
                "official": True,
                "basis": "published_inactives",
                "source": row.get("source"),
                "note": row.get("note"),
                "posted": row.get("posted"),
            })

        # Fallback: severity filter, for teams without a published list.
        for inj in injuries:
            team = normalize_team(inj.get("team_id"))
            if team in published:
                continue
            severity = inj.get("severity")
            if severity and severity >= threshold:
                inactives.append({
                    "player_id": inj.get("player_id"),
                    "player_name": inj.get("player_name"),
                    "team_id": inj.get("team_id"),
                    "position": inj.get("position"),
                    "injury_status": inj.get("injury_status"),
                    "injury_type": inj.get("injury_type"),
                    "game_status": inj.get("game_status"),
                    "severity": severity,
                    "confidence": inj.get("confidence", 50),
                    "official": False,
                    "basis": "injury_report_severity",
                    "source": "injury_report",
                })

        # Official first, then by severity (highest first), then confidence
        inactives.sort(key=lambda x: (not x["official"], -(x.get("severity") or 6),
                                      -(x.get("confidence") or 100)))

        mode = ("official" if published and not any(not r["official"] for r in inactives)
                else "mixed" if published else "fallback_injury_report")
        result = {
            "inactives": inactives,
            "total_inactives": len(inactives),
            "severity_threshold_used": threshold,
            "mode": mode,
            "official_published_teams": published,
            "pending_teams": pending,
            "games": official.get("games") or {},
            "confirmed_active": [
                {"player_name": r["player_name"], "team_id": r["team_id"], "note": r.get("note")}
                for r in official.get("confirmed_active") or []
            ],
            "season": season,
            "week": week,
            "success": True,
        }
        if mode == "fallback_injury_report":
            result["fallback_note"] = (
                "No inactive list is published yet (they come ~90 minutes before "
                "kickoff). These are injury-report designations at or above the "
                "severity threshold, not official inactives."
            )

        if league_id and roster_id is not None and week:
            result["my_starters"] = await _flag_my_starters(
                str(league_id), int(roster_id), int(week), inactives,
                official.get("confirmed_active") or [], set(pending), official.get("games") or {},
                norm_name,
            )
        return result

    except Exception as e:
        return {
            "inactives": [],
            "total_inactives": 0,
            "severity_threshold_used": severity_threshold,
            "success": False,
            "error": str(e)
        }


async def _flag_my_starters(league_id, roster_id, week, inactives, confirmed_active,
                            pending, games, norm_name) -> dict:
    """Which of the roster's current starters are inactive / at risk."""
    from .teams import normalize_team

    starters: list[str] = []
    try:
        matchups = (await sleeper_tools.get_matchups(league_id, week) or {}).get("matchups") or []
        mine = next((m for m in matchups if m.get("roster_id") == roster_id), None)
        starters = [str(p) for p in (mine or {}).get("starters") or [] if p and p != "0"]
        if not starters:
            rosters = (await sleeper_tools.get_rosters(league_id) or {}).get("rosters") or []
            roster = next((r for r in rosters if r.get("roster_id") == roster_id), None)
            starters = [str(p) for p in (roster or {}).get("starters") or [] if p and p != "0"]
    except Exception as e:
        return {"error": f"could not load starters: {e}"}
    athletes = get_db().get_athletes_by_ids(starters) if starters else {}

    def key(name, team):
        return norm_name(name), normalize_team(team)

    official = {key(r["player_name"], r["team_id"]): r for r in inactives if r["official"]}
    at_risk = {key(r["player_name"], r["team_id"]): r for r in inactives if not r["official"]}
    active = {key(r["player_name"], r["team_id"]): r for r in confirmed_active}
    out = {"inactive": [], "at_risk": [], "confirmed_active": [], "not_yet_published": []}
    for pid in starters:
        row = athletes.get(pid) or {}
        name, team = row.get("full_name"), normalize_team(row.get("team_id"))
        if not name or not team:
            continue
        k = key(name, team)
        entry = {"player_id": pid, "player": name, "team": team,
                 "kickoff": (games.get(team) or {}).get("kickoff")}
        if k in official:
            out["inactive"].append({**entry, "source": official[k]["source"],
                                    "note": official[k].get("note")})
        elif k in active:
            out["confirmed_active"].append({**entry, "note": active[k].get("note")})
        elif k in at_risk:
            out["at_risk"].append({**entry, "injury_status": at_risk[k]["injury_status"],
                                   "basis": "injury_report_severity"})
        if team in pending and k not in official and k not in active:
            out["not_yet_published"].append(entry)
    return out


# =============================================================================
# COACHING INTELLIGENCE TOOLS
# =============================================================================

@timing_decorator("get_coaching_staff", tool_type="nfl")
async def get_coaching_staff(team_id: str) -> dict:
    """Get coaching staff for a specific NFL team from ESPN API.

    Parameters:
        team_id (str, required): Team abbreviation (e.g. 'KC', 'NE', 'DAL').
    Returns: {team_id, team_name, coaches:[...], head_coach, offensive_coordinator, defensive_coordinator, total_coaches, success, error?}
    Example: get_coaching_staff(team_id="KC")
    """
    try:
        team_id = validate_string_input(team_id, 'team_id', max_length=10, required=True)
        return await coaching_tools.get_coaching_staff(team_id)
    except ValueError as e:
        return {"team_id": team_id, "team_name": None, "coaches": [], "head_coach": None, "success": False, "error": str(e)}


@timing_decorator("get_all_coaching_staffs", tool_type="nfl")
async def get_all_coaching_staffs() -> dict:
    """Get coaching staff information for all 32 NFL teams.

    Returns: {teams:[{team_id, team_name, head_coach, coach_count}...], total_teams, success, error?}
    Example: get_all_coaching_staffs()
    """
    return await coaching_tools.get_all_coaching_staffs()


@timing_decorator("get_coaching_tree", tool_type="nfl")
async def get_coaching_tree(coach_name: str) -> dict:
    """Get coaching tree information for a known NFL coach.

    Parameters:
        coach_name (str, required): Coach's full name (e.g. 'Andy Reid', 'Bill Belichick').
    Returns: {coach_name, mentors:[...], proteges:[...], scheme_family, known_for:[...], found, success, error?}
    Example: get_coaching_tree(coach_name="Andy Reid")
    """
    try:
        coach_name = validate_string_input(coach_name, 'coach_name', max_length=100, required=True)
        return await coaching_tools.get_coaching_tree(coach_name)
    except ValueError as e:
        return {"coach_name": coach_name, "found": False, "success": False, "error": str(e)}


@timing_decorator("get_scheme_classification", tool_type="nfl")
async def get_scheme_classification(
    team_id: str, season: int | None = None, use_live_staff: bool = True
) -> dict:
    """Get offensive and defensive scheme classification for an NFL team.

    Resolved from the team's CURRENT staff — a scheme belongs to the play-caller,
    not the franchise — so a coordinator change is picked up automatically. Check
    `offense.source`/`defense.source`: `coach` means it was read off the named,
    live-fetched coach; `team_table` means it is a dated guess (see `as_of` and
    `warnings`) and may be a regime out of date.

    Parameters:
        team_id (str, required): Team abbreviation (e.g. 'KC', 'NE', 'DAL').
        season (int, optional): Season for the staff lookup (default: current).
        use_live_staff (bool, default True): False skips the network call.
    Returns: {team_id, offensive_scheme, defensive_scheme, offense{scheme,source,
        attributed_to}, defense{...}, head_coach, scheme_notes:[...], found,
        is_fallback, as_of, warnings:[...], success, error?}
    Example: get_scheme_classification(team_id="SF")
    """
    try:
        team_id = validate_string_input(team_id, 'team_id', max_length=10, required=True)
        return await coaching_tools.get_scheme_classification(
            team_id, season=season, use_live_staff=use_live_staff
        )
    except ValueError as e:
        return {"team_id": team_id, "found": False, "success": False, "error": str(e)}


# =============================================================================
# FEATURE-FLAGGED TOOLS
# =============================================================================

if FEATURE_LEAGUE_LEADERS:
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


@timing_decorator("get_weekly_briefing", tool_type="fantasy")
async def get_weekly_briefing(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
) -> dict:
    """START HERE for "how should I line up this week" - one call, not six.

    Joins roster, opponent, league scoring/slots, schedule, weather, trailing
    usage and projections into a single answer, then names the lineup changes
    worth making. Doing this by hand across separate tools is where week
    boundaries and team-code variants slip in.

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id
        week: NFL week (defaults to the current one)
        season: Season (defaults to the current one)

    Returns: {
        league {name, scoring, slots}, week, record, win_probability,
        projected_points, opponent_projected_points, recommended_lineup,
        changes [{slot, start, projected_points}],
        bench: names of CURRENT starters the recommendation moves to the bench
               (not your bench players),
        injury_changes: real status moves on your roster in the last 7 days,
        unavailable [{player, position, status, source, sleeper_status,
                      report_status, injury_type, in_recommended_lineup}]:
               players projected at or near zero for injury this week,
        ir_moves [{action, player, reason}]: activate / move_to_ir / ir_full,
               see audit_ir_slots,
        not_projected, reserve, success
    }

    Example: get_weekly_briefing(league_id="123", roster_id=7)
    Example: get_weekly_briefing(league_id="123", user_id="456", week=3)
    """
    return await briefing_tools.get_weekly_briefing(
        league_id=league_id, roster_id=roster_id, user_id=user_id,
        week=week, season=season,
    )


@timing_decorator("get_weekly_retro", tool_type="fantasy")
async def get_weekly_retro(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    include_calibration: bool = True,
) -> dict:
    """Post-game review of a FINISHED week: "how did last week go / what did I
    leave on the bench / were the projections any good".

    Use this after the games, not before them - for setting a lineup use
    get_weekly_briefing. Grades each of your starters' actual points against
    the projection logged BEFORE kickoff (by get_weekly_briefing /
    project_players), computes the best legal lineup in hindsight from your
    whole roster with the league's own slot rules, and says whether it would
    have flipped the result against your opponent.

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id
        week: Week to review (default: the last fully completed week)
        season: Season (default: current)
        include_calibration: Also return projection accuracy over every logged
            week of the season (default True)

    Returns: {
        result {points, opponent_points, outcome, margin}, projected_total,
        projection_source: "stored" (pre-kickoff log) | "recomputed" (no log
            for that week: best-effort, after the fact) | "mixed" | "none",
        starters [{slot, player, position, actual, projected, floor, ceiling,
                   diff, within_range, projection_source}],
        bench [...same...],
        hindsight {optimal_points, points_left_on_bench, should_have_started,
                   should_have_sat, optimal_outcome, would_have_flipped},
        biggest_misses, biggest_hits (starters, |diff| >= 3),
        opponent {roster_id, points, projected, top_scorer},
        calibration {weeks, n, mean_error (actual - projected), mean_abs_error,
                     within_range_share, by_position},
        week_source, notes, success
    }

    Example: get_weekly_retro(league_id="123", roster_id=7)
    Example: get_weekly_retro(league_id="123", user_id="456", week=2)
    """
    league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
    return await retro_tools.get_weekly_retro(
        league_id=league_id, roster_id=roster_id, user_id=user_id,
        week=week, season=season, include_calibration=include_calibration,
    )


@timing_decorator("get_league_changes", tool_type="fantasy")
async def get_league_changes(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    since: str | None = None,
    mark_seen: bool = True,
    projection_threshold: float = 2.0,
    limit: int = 25,
) -> dict:
    """START HERE for a daily check-in: "what changed in my league since I last
    looked" - one ranked delta for YOUR roster, not full reports.

    Remembers when this roster was last checked and returns only what is new
    since then, most important first: injury status moves on your roster and
    your current opponent's starters, ESPN/CBS news naming those players,
    league adds/drops/trades, trending pickups who back up one of your
    starters (same team and position), and your starters' projection moves
    larger than `projection_threshold`. Use get_injury_trends for a
    league-agnostic injury feed, get_weekly_briefing to set the lineup.

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id
        since: ISO-8601 timestamp to diff from (default: the last check of this
            roster; 24h back on the very first check)
        mark_seen: Advance the last-check time to now (default True). Pass
            False to peek without consuming the changes.
        projection_threshold: Minimum projection move in points (default 2.0)
        limit: Max changes returned (default 25; `omitted` says how many more)

    Returns: {
        changes [{kind: injury|news|transaction|trending_backup|projection,
                  importance, summary, ...}] sorted by importance,
        counts {kind: n}, omitted, since, since_source, checked_at,
        marked_seen, week, opponent_roster_id, errors {source: message},
        success
    }

    Example: get_league_changes(league_id="123", roster_id=7)
    Example: get_league_changes(league_id="123", roster_id=7, mark_seen=False)
    """
    league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
    return await league_changes_tools.get_league_changes(
        league_id=league_id, roster_id=roster_id, user_id=user_id, since=since,
        mark_seen=mark_seen, projection_threshold=projection_threshold, limit=limit,
    )


@timing_decorator("find_trade_targets", tool_type="trade")
async def find_trade_targets(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    positions: list[str] | None = None,
    limit: int = 10,
    horizon: str = "ros",
) -> dict:
    """START HERE for "who should I trade with" - finds the deal, not just grades one.

    analyze_trade evaluates a trade you already have in mind; this finds which
    trades are worth proposing. Every one-for-one swap against every other roster
    is scored by recomputing BOTH teams' best legal starting lineup before and
    after it, and only trades where both sides gain are returned — a trade the
    other manager loses is a wish, not a deal.

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id
        week: NFL week (defaults to the current one)
        season: Season (defaults to the current one)
        positions: Only propose receiving these positions, e.g. ["RB"]
        limit: Max proposals, one per partner (default 10)
        horizon: "ros" (default) — gains are rest-of-season lineup points: both
            lineups re-optimised for every remaining week (regular season +
            fantasy playoffs, byes and injury absences included) and summed;
            "week" — this week's lineup only (the old behaviour).

    Returns: {
        proposals [{partner, partner_roster_id, you_give, you_get, your_gain,
                    their_gain, mutual_gain}],
        trade_deadline {deadline_week, passed, urgent, weeks_left, message},
        your_replacement_levels, candidates_considered (every swap scored),
        caveats, league, week, season, success
    }

    Reads the league's trade_deadline: past it, no proposals are returned;
    within a week of it, the message says so. Run the chosen deal through
    analyze_trade before sending it.

    Example: find_trade_targets(league_id="123", roster_id=7)
    Example: find_trade_targets(league_id="123", roster_id=7, positions=["RB"])
    """
    return await trade_finder_tools.find_trade_targets(
        league_id=league_id, roster_id=roster_id, user_id=user_id,
        week=week, season=season, positions=positions, limit=limit,
        horizon=horizon,
    )


@timing_decorator("audit_ir_slots", tool_type="waiver")
async def audit_ir_slots(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
) -> dict:
    """IR audit: who to move into the IR slot, who must come out, who is stuck.

    Reads the league's own IR rules (reserve_slots, reserve_allow_out/doubtful/
    sus/na/dnr/cov; IR and PUP are always allowed) against Sleeper's injury
    status, which is what Sleeper enforces. Use for "can I put X on IR", "why
    can't I add anyone", or as part of a weekly roster check (get_weekly_briefing includes the same moves).

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id

    Returns: {
        reserve_slots, reserve_used, reserve_free, eligible_statuses,
        moves [{action, player, position, sleeper_status, report_status, reason}],
        message, success
    }
    action is one of:
        activate      - in IR but no longer eligible; Sleeper blocks adds/claims until moved
        move_to_ir    - eligible, on the active roster, and a slot is free
        ir_full       - eligible, but every IR slot is taken
        not_eligible  - will not play this week, but the league's rules keep him out of IR

    Example: audit_ir_slots(league_id="123", roster_id=7)
    """
    league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
    return await ir_audit.audit_ir_slots(league_id=league_id, roster_id=roster_id, user_id=user_id)


@timing_decorator("get_waiver_targets", tool_type="waiver")
async def get_waiver_targets(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    positions: list[str] | None = None,
    limit: int = 12,
) -> dict:
    """START HERE for "who should I pick up" - the waiver question, for YOUR league.

    Ranks the players nobody in your league rosters by how much they would
    actually upgrade your lineup: each is projected for the coming week in your
    league's scoring, then compared against the weakest player who currently
    starts for you at that position. Use this instead of get_trending_players,
    which reports league-agnostic add counts and includes players already taken.

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id
        week: NFL week (defaults to the current one)
        season: Season (defaults to the current one)
        positions: Restrict to positions, e.g. ["RB","WR"] (default all claimable)
        limit: Max targets to return (default 12)

    Returns: {
        targets [{name, position, team, opponent, projected_points, floor,
                  ceiling, replacement_level, upgrade_points, trending_adds,
                  verdict, kickoff, kickoff_local, locked, waiver_timing,
                  waiver_strategy {recommendation: claim_now|add_now|wait|
                  dont_bother, reason, waiver_position, teams_ahead, wait_days}}],
        waiver_priority, locked_players, too_late_for_this_week,
        drop_candidates, replacement_levels, thin_positions, waiver_type,
        pool_size, league, week, season, success
    }

    Example: get_waiver_targets(league_id="123", roster_id=7)
    Example: get_waiver_targets(league_id="123", user_id="456", positions=["RB"])
    """
    return await waiver_target_tools.get_waiver_targets(
        league_id=league_id, roster_id=roster_id, user_id=user_id,
        week=week, season=season, positions=positions, limit=limit,
    )
