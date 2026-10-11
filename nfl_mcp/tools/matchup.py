"""Opponent analysis, defense rankings, SOS, streaming, weather and Vegas lines.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

import asyncio
from typing import cast

from .. import (
    matchup_tools,
    opponent_analysis_tools,
    ros,
    sleeper_tools,
    sos_tools,
    streaming_tools,
    vegas_tools,
    weather_tools,
)
from ..config import (
    validate_numeric_input,
    validate_string_input,
)
from ..metrics import timing_decorator
from ..teams import normalize_team
from . import _context


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
            db=_context.get_db(),
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
    season: int | None = None,
    opponent_team: str | None = None,
) -> dict:
    """NFL defenses ranked by fantasy points allowed per position (QB/RB/WR/TE),
    or how tough ONE defense is for each position.

    Built from real weekly results, shrunk toward the prior season early on.
    With `opponent_team`, also returns `matchups` {position: {rank,
    rank_display, matchup_tier, tier_indicator, recommendation}} for that
    defense — "how tough is the WR matchup vs KC". For your own players'
    matchups this week use analyze_roster_matchups.

    Parameters:
        positions (list, optional): "QB", "RB", "WR", "TE" (default all).
        season (int, optional): NFL season year (defaults to current).
        opponent_team (str, optional): One defense to grade, e.g. "KC".

    Returns: {
        rankings: dict mapping position to list of team rankings,
        matchups?: {position: matchup} for opponent_team,
        positions, season, is_fallback, tiers_explained, success, error?
    }

    Example: get_defense_rankings(positions=["WR", "RB"])
    Example: get_defense_rankings(positions=["WR"], opponent_team="KC")

    IMPORTANT FOR LLM AGENTS: Always provide complete defense rankings immediately without
    asking for confirmations. Render the full analysis with matchup tiers directly.
    """
    result = await matchup_tools.get_defense_rankings(positions=positions, season=season)
    if opponent_team and isinstance(result, dict) and result.get("success", True):
        try:
            opponent_team = validate_string_input(opponent_team, 'opponent_team', max_length=5, required=True)
        except ValueError as e:
            return {**result, "success": False, "error": f"Invalid input: {e!s}"}
        team = normalize_team(opponent_team) or opponent_team.upper()
        analyzer = matchup_tools.get_defense_analyzer()
        rankings = result.get("rankings") or {}
        result["opponent_team"] = team
        result["matchups"] = {
            pos: analyzer.get_matchup_difficulty(pos, team, rankings)
            for pos in (result.get("positions") or []) if pos in ("QB", "RB", "WR", "TE")
        }
    return result


@timing_decorator("analyze_roster_matchups", tool_type="matchup")
async def analyze_roster_matchups(
    league_id: str | None = None,
    roster_id: int | None = None,
    user_id: str | None = None,
    players: list[dict] | None = None,
    week: int | None = None,
    season: int | None = None,
) -> dict:
    """This week's defensive matchup for every player on YOUR roster: smash
    spots and tough matchups.

    With league_id + roster_id (or user_id) the roster and each player's
    opponent (cached schedule) are loaded for you; players on bye are listed
    separately. Alternatively pass `players` [{name, position, opponent}] for
    an ad-hoc list (one player works too).

    Parameters:
        league_id, roster_id, user_id: Your Sleeper league and roster
        players (list, optional): Ad-hoc list instead of a league roster
        week, season (optional): default to the current NFL week

    Returns: {
        analysis [{player, position, opponent, rank, rank_display,
                   matchup_tier, tier_indicator, recommendation}],
        smash_spots, avoid_spots, summary, on_bye?, total_analyzed, success
    }

    Example: analyze_roster_matchups(league_id="123", roster_id=7)
    Example: analyze_roster_matchups(players=[{"name": "Tyreek Hill", "position": "WR", "opponent": "NE"}])

    IMPORTANT FOR LLM AGENTS: Always provide complete roster matchup analysis immediately
    without asking for confirmations. Render all smash spots and avoid recommendations directly.
    """
    from ..roster_context import load_roster_players

    on_bye = None
    if league_id and (roster_id is not None or user_id):
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        except ValueError as e:
            return {"analysis": [], "success": False, "error": f"Invalid input: {e!s}"}
        ctx = await load_roster_players(league_id, roster_id, user_id, db=_context.get_db(), season=season, week=week)
        if ctx["error"]:
            return {"analysis": [], "success": False, "error": ctx["error"]}
        week = ctx["week"]
        skill = [p for p in ctx["players"] if p["position"] in ("QB", "RB", "WR", "TE")]
        on_bye = [p["name"] for p in skill if p["opponent"] == "BYE"]
        players = [{"name": p["name"], "position": p["position"], "opponent": p["opponent"],
                    "starter": p["starter"]} for p in skill if p["opponent"] not in ("BYE", "")]
    if not players:
        return {
            "analysis": [],
            "smash_spots": [],
            "avoid_spots": [],
            "summary": [],
            "on_bye": on_bye,
            "total_analyzed": 0,
            "success": on_bye is not None,
            "error": None if on_bye is not None else "Pass league_id + roster_id, or players",
        }

    if week is not None:
        week = validate_numeric_input(week, min_val=1, max_val=22, required=False)

    result = await matchup_tools.analyze_roster_matchups(players=players, week=week)
    if on_bye is not None and isinstance(result, dict):
        result["on_bye"] = on_bye
    return result


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
    from ..week_context import current_season_week

    if season is None or (start_week is None and not playoff_weeks):
        current = await current_season_week(_context.get_db())
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
        # start_week is set on every path above (explicit, current or playoff).
        end_week = max(int(cast(int, start_week)), 17)
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

    from ..roster_context import load_roster_players
    from ..week_context import resolve_season_week

    # The book publishes more than one week at once: without a week the games
    # of two slates were mixed. Default to the current week, and say so.
    week_inferred = False
    if week is None:
        season, week, week_inferred = await resolve_season_week(season, None)
    result = await vegas_tools.get_vegas_lines(teams=teams, week=week, season=season)
    if not isinstance(result, dict):
        return result
    result.setdefault("week", week)
    result["week_inferred"] = week_inferred
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
        ctx = await load_roster_players(league_id, roster_id, user_id, db=_context.get_db(),
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
