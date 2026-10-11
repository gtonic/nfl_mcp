"""Weekly / ROS / opportunity projections and usage trends.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import (
    opportunity_tools,
    projections,
    ros,
    usage_trends,
)
from ..config import validate_string_input
from ..metrics import timing_decorator
from . import _context

# =============================================================================
# PROJECTION TOOLS (transparent weekly fantasy point projections)
# =============================================================================

@timing_decorator("project_players", tool_type="projection")
async def project_players(
    players: list[dict | str],
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
            A bare name (a string, or a dict without team/position) is
            resolved from the athlete cache (fantasy position, active, market
            rank first); one that cannot be is listed in `unresolved` and not
            projected, and an ambiguous name adds a `warnings` entry.
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
              week_inferred, total, unresolved, warnings, success}
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
        season=season, week=week, db=_context.get_db(), league_id=league_id,
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

        this week = the weekly projection (Sleeper-first blend)
        later     = 0.25 × our per-game baseline (trailing opportunity in the
                    league's full scoring_settings, regressed toward the
                    position prior for small samples) × that week's
                    defense-vs-position matchup
                    + 0.75 × Sleeper's projection for that week (priced in the
                    league's scoring; our model alone where Sleeper has none)
        0 on bye weeks (cached schedule, missing weeks fetched) and inside the
        expected injury absence (report text when it states a timeline, else
        1 week for Out, 4 for IR/PUP/NFI; a hurt player Sleeper does not
        project until a later week stays at 0 until then).

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
              baseline_source, ros_source (sleeper_blend|model|mixed),
              week_sources, value_trajectory, weekly? [{week, opponent, points,
              source, model_points, sleeper_points, reason}]}],
              regular_season_weeks, playoff_weeks, sleeper_ros {active, weeks,
              weeks_missing, model_weight}, unresolved, elapsed_seconds, success}

    value_trajectory per player: where his TRADE value is headed over the next
    ~3 games — {trajectory: rising|falling|stable, signal: buy_low|sell_high|
    hold, expected_value_change {pct, per_game, ros_points}, change_week,
    market_gap {our_rank, market_rank, gap, read}, reasons}. Read from a
    teammate due back (the backup's inflated rate ends), inherited volume
    ending, his own return from a multi-week absence, a recent role shift,
    and our per-game rank vs the FantasyCalc positional rank.

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
        db=_context.get_db(),
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
        weeks=weeks, season=season, through_week=through_week, db=_context.get_db(),
    )
