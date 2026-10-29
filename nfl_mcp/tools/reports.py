"""Weekly briefing, retro, projection accuracy, signal review and league changes.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import (
    briefing_tools,
    league_changes_tools,
    projection_accuracy,
    retro_tools,
    signal_review,
)
from ..config import (
    validate_numeric_input,
    validate_string_input,
)
from ..metrics import timing_decorator
from . import _context


@timing_decorator("get_weekly_briefing", tool_type="fantasy")
async def get_weekly_briefing(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    risk_mode: str = "auto",
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
        risk_mode: "auto" (default) | "neutral" | "seek_variance" |
            "protect_floor". Auto reads this week's P(win) and your season
            playoff odds: underdog or long shot -> ceiling, favourite -> floor.

    Returns: {
        risk_mode, risk_reason, playoff_pct, risk_adjustment {swaps,
        mean_delta, p_win_points_optimal, p_win_risk_adjusted, summary} | null,
        points_optimal_win_probability, points_optimal_projected,
        league {name, scoring, slots}, week, record, win_probability,
        projected_points, opponent_projected_points, recommended_lineup,
        opponent_projection_basis ("set_lineup": win_probability and
            risk_mode use the lineup the opponent has SET, his kicked-off
            starters at their actual points), opponent_set_lineup_points,
            opponent_best_lineup_points (his best lineup from players he can
            still start), opponent_points_at_risk,
            win_probability_if_opponent_fixes_lineup,
            opponent_lineup_issues [{slot, player, issue: empty|bye|out|
            inactive|doubtful|unprojected|zero_projection}],
            opponent_best_lineup_changes, opponent_set_lineup,
            opponent_locked_players, opponent_projection_note — a day-to-day
            jump in opponent_projected_points is usually him fixing one of
            those issues,
        changes [{slot, start, projected_points}],
        bench: every active-roster player outside recommended_lineup (IR and
               taxi apart), highest projection first,
        move_to_bench: the CURRENT starters among them (the recommendation
               sits them),
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
        week=week, season=season, risk_mode=risk_mode,
    )


@timing_decorator("get_weekly_retro", tool_type="fantasy")
async def get_weekly_retro(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    include_calibration: bool = True,
    include_signal_review: bool = False,
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
        include_signal_review: Also list, per starter, the signals that moved
            his projection, plus a signal_review section (the week's signal
            recommendations and your players' biggest misses; default False)

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
        signal_review {recommendations, signals_to_watch, roster_misses
                       [{player, projected, actual, diff, signals}]}
            (only with include_signal_review; starters then carry signals),
        week_source, notes, success
    }

    Example: get_weekly_retro(league_id="123", roster_id=7)
    Example: get_weekly_retro(league_id="123", user_id="456", week=2)
    """
    league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
    return await retro_tools.get_weekly_retro(
        league_id=league_id, roster_id=roster_id, user_id=user_id,
        week=week, season=season, include_calibration=include_calibration,
        include_signal_review=bool(include_signal_review),
    )


@timing_decorator("get_projection_accuracy", tool_type="fantasy")
async def get_projection_accuracy(
    weeks: list[int] | None = None,
    position: str | None = None,
    by_signal: bool = True,
    season: int | None = None,
    league_id: str | None = None,
) -> dict:
    """How accurate were the projections, week by week — and which signals help?

    Grades every pre-kickoff projection logged by get_weekly_briefing,
    project_players and get_league_changes (the last one before kickoff) against
    the points actually scored, in the scoring it was made in (plus a neutral
    half-PPR actual). Finished weeks not graded yet are graded on the fly; the
    background prefetch grades each week once it is final.

    Use for "are the projections any good", "do we over-project QBs", "does
    the role-change / returning-teammate / QB-coupling adjustment work". For
    one roster's week (bench points, lineup mistakes) use get_weekly_retro.

    Parameters:
        weeks (list[int], optional): default every graded week of the season
        position (optional): QB/RB/WR/TE/K/DEF
        by_signal (bool, default True): MAE/bias with vs without each signal
            (role_down/role_up, returning_teammates, inherited_volume,
            qb_coupling, practice_dnp/limited, questionable, doubtful,
            model_only, sleeper_disagreement, news:<flag>)
        season (optional): default current
        league_id (optional): only that league's logged projections

    Returns: {season, weeks, overall {n, mae, bias}, by_position,
              by_projection_source, components {model, sleeper, blend} (same
              rows), trend [{week, n, mae, bias}], by_signal {signal: {with,
              without, bias_gap, mae_gap, small_sample}}, interpretation [..],
              graded_now, definitions, success}
        bias = projected − actual (+ = projections too high).

    Example: get_projection_accuracy()
    Example: get_projection_accuracy(position="WR", weeks=[3, 4])
    """
    if league_id is not None:
        try:
            league_id = validate_string_input(league_id, 'league_id', max_length=50)
        except ValueError as e:
            return {"success": False, "error": f"Invalid input: {e!s}"}
    if isinstance(weeks, int):
        weeks = [weeks]
    try:
        weeks = [int(w) for w in weeks] if weeks else None
    except (TypeError, ValueError):
        return {"success": False, "error": "weeks must be a list of week numbers"}
    return await projection_accuracy.get_projection_accuracy(
        weeks=weeks, position=position, by_signal=bool(by_signal), season=season,
        league_id=league_id, db=_context.get_db(),
    )


@timing_decorator("get_weekly_signal_review", tool_type="fantasy")
async def get_weekly_signal_review(
    week: int | None = None,
    league_id: str | None = None,
    roster_id: int | None = None,
    user_id: str | None = None,
    season: int | None = None,
) -> dict:
    """Weekly calibration review of the projection SIGNALS: is each weight
    (practice buckets, role trend, QB coupling, news flags, ...) still right?

    For the last graded week (and cumulative over the season) every signal
    gets n, the realised/projected ratio of its rows, that ratio relative to a
    baseline (healthy players for injury/practice buckets, all other rows
    otherwise) with a 95% bootstrap interval, bias and MAE vs the rows without
    it, the implied multiplier where the signal has one, and a recommendation
    under explicit minimum-sample rules (n>=10 to read, n>=60 over >=2 weeks
    before anything is worth changing). Also the week's biggest individual
    misses with the signals that were active. The prefetch stores each newly
    graded week's review, so this is cheap.

    Use for "which adjustments are miscalibrated", "is the questionable / LP
    discount right", "review last week's signals". Overall MAE/bias per
    position: get_projection_accuracy. One roster's week: get_weekly_retro.

    Parameters:
        week (optional): default the last graded week
        league_id (optional): only that league's logged projections, and its
            players' misses
        roster_id / user_id (optional, with league_id): misses of that
            roster's players only
        season (optional): default current

    Returns: {season, week, weeks_cumulative, rows_week, rows_cumulative,
              overall_week, overall_cumulative {n, mae, bias},
              signals [{signal, group, label, baseline, param, current?,
                        implied?, implied_ci?, week {...}, cumulative {n, weeks,
                        realised_ratio, baseline_ratio, relative_ratio,
                        relative_ci, bias, mae, baseline_bias, baseline_mae,
                        mae_gap}, verdict (review|watch|calibrated|
                        insufficient), recommendation}],
              recommendations [..], biggest_misses [{player, projected, actual,
              diff, signals [..]}], misses_scope, rules, source
              (stored|computed), graded_now, success}

    Example: get_weekly_signal_review()
    Example: get_weekly_signal_review(week=5, league_id="123", roster_id=7)
    """
    try:
        if league_id is not None:
            league_id = validate_string_input(league_id, 'league_id', max_length=50)
        if week is not None:
            week = validate_numeric_input(week, min_val=1, max_val=22, required=False)
    except ValueError as e:
        return {"success": False, "error": f"Invalid input: {e!s}"}
    return await signal_review.get_weekly_signal_review(
        week=week, league_id=league_id, roster_id=roster_id, user_id=user_id,
        season=season, db=_context.get_db(),
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
