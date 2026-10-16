"""Sleeper league/roster/matchup/user/draft endpoints, fantasy context, bye weeks, playoff odds.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import (
    playoff_tools,
    sleeper_tools,
)
from ..config import (
    LIMITS,
    validate_numeric_input,
    validate_string_input,
)
from ..metrics import timing_decorator
from . import _context

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

    Sleeper files the Wednesday waiver run under the PREVIOUS leg: on the
    Wednesday of week 5 that morning's claims are in leg 4 and leg 5 is still
    empty. For the current week (or no week) this tool therefore merges the
    previous leg in, deduped by transaction_id; each row keeps Sleeper's `leg`
    and the response lists `legs` and `leg_note`. A past week is read alone.

    Failed claims are listed apart under `failed_claims`, each with Sleeper's
    own `note` ("Unfortunately, your roster will have too many players after
    this transaction.") and a `reason_code` (roster_full, claimed_by_other,
    insufficient_budget, player_locked, other). To check a whole plan against
    what happened, use plan_roster_moves(verify=True).

    Parameters:
        league_id (str, required): Sleeper league id.
        week (int, optional): NFL week (alias: `round`); default the current week.
    Returns: {transactions: [...], failed_claims [{wanted, would_have_dropped,
        note, reason_code, roster_ids, processed_at}], failed_claims_count,
        week, legs?, count, success, error?}
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        # Accept either week or deprecated round
        effective_week = week if week is not None else round
        if effective_week is not None:
            effective_week = validate_numeric_input(
                effective_week, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=True)
        result = await sleeper_tools.get_transactions(
            league_id, round=effective_week, week=effective_week, include_previous_leg=None)
        if isinstance(result, dict):
            from ..move_planner import failed_claims
            failed = failed_claims(result.get("transactions"))
            result["failed_claims"] = failed
            result["failed_claims_count"] = len(failed)
        return result
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
        return await sleeper_tools.get_trending_players(_context.get_db(), trend_type, lookback_hours, limit)
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
    from .. import bye_week_tools
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
    except ValueError as e:
        return {"weeks": [], "success": False, "error": f"Invalid input: {e!s}"}
    return await bye_week_tools.get_bye_week_plan(
        league_id=league_id, roster_id=roster_id, user_id=user_id,
        weeks_ahead=weeks_ahead, week=week, season=season,
        include_free_agents=include_free_agents, db=_context.get_db(),
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
        score_sd=score_sd, my_roster_id=my_roster_id, seed=seed, db=_context.get_db(),
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
