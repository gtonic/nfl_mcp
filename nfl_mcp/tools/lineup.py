"""Start/sit, slot comparison, lineup analysis and win-probability lineups.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import (
    lineup_optimizer_tools,
    win_probability,
)
from ..config import (
    validate_numeric_input,
    validate_string_input,
)
from ..metrics import timing_decorator
from . import _context

# =============================================================================
# LINEUP OPTIMIZER TOOLS (Start/Sit Recommendations)
# =============================================================================

@timing_decorator("get_start_sit_recommendation", tool_type="lineup")
async def get_start_sit_recommendation(
    player_name: str | None = None,
    players: list | None = None,
    position: str | None = None,
    team: str | None = None,
    opponent: str = "",
    player_id: str | None = None,
    injury_status: str | None = None,
    league_id: str | None = None,
    scoring: str | None = None,
    season: int | None = None,
    week: int | None = None,
    include_reasoning: bool = True,
) -> dict:
    """Start or sit? For one player (player_name) or several (players).

    Name the player; team, position, Sleeper id, opponent (cached schedule),
    last week's snap share, injury designation and this week's practice report
    are looked up server-side — do not guess them. Decided on projected points
    (matchup, Vegas, usage, injury), with a confidence-weighted verdict. For
    the whole lineup of a league roster use analyze_lineup; for one slot
    between 2-5 candidates compare_players_for_slot; for a full weekly plan
    get_weekly_briefing.

    K and DEF are priced off Vegas totals (own for K, opponent's for DEF) or,
    without live lines, the season's scoring; their matchup is an offense rank.

    Parameters:
        player_name (str): One player's name (single mode).
        players (list): Several players, as names or dicts {name, position?,
            team?, opponent?, player_id?} (list mode — replaces the old
            get_roster_recommendations).
        position, team, player_id (optional): Disambiguate a common name.
        opponent (optional): Opponent abbreviation or "BYE"; default from the
            schedule. A team with no game that week is must_sit (`on_bye`).
        injury_status (optional): Override the looked-up designation
            (healthy, questionable, doubtful, out).
        league_id (optional): Sleeper league id — the league's real scoring and
            size. Prefer it over `scoring`.
        scoring (optional): 'ppr', 'half_ppr', 'standard' or '0.5'.
        season, week (optional): default to the current NFL week.
        include_reasoning (bool, list mode): Include per-player reasoning.

    Returns (single): {recommendation {player, position, team, opponent,
        decision, decision_display, projected_points, floor, ceiling,
        sleeper_projection, consensus, disagreement, implied_total, on_bye,
        kickoff_local, locked, ...}, confidence, confidence_level,
        matchup_tier, reasoning, factors, resolved {team, position,
        player_id, snap_percentage}, success}
    Returns (list): {recommendations (by projected points), by_position,
        must_starts, sits, on_bye, locked, summary, success}

    Example: get_start_sit_recommendation(player_name="Tyreek Hill", league_id="123")
    Example: get_start_sit_recommendation(players=["Puka Nacua", "DeVonta Smith"], league_id="123")

    IMPORTANT FOR LLM AGENTS: Always provide complete start/sit recommendation immediately
    without asking for confirmations. Render the decision and reasoning directly.
    """
    from .. import lineup_tools
    from ..week_context import resolve_season_week

    try:
        if league_id:
            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=False)
        opponent = validate_string_input(opponent or '', 'opponent', max_length=8, required=False)
    except ValueError as e:
        return {"recommendation": None, "confidence": 0, "success": False, "error": f"Invalid input: {e!s}"}
    if not player_name and not players:
        return {"recommendation": None, "confidence": 0, "success": False,
                "error": "Pass player_name (one player) or players (a list)."}
    season, week, _ = await resolve_season_week(season, week)
    db = _context.get_db()

    if players:
        if week is not None:
            week = validate_numeric_input(week, min_val=1, max_val=22, required=False)
        inputs = [lineup_tools.player_input(db, p, season, week) for p in players[:30]]
        unresolved = [p.get("name") for p in inputs if not (p.get("team") and p.get("position"))]
        inputs = [p for p in inputs if p.get("team") and p.get("position")]
        if not inputs:
            return {"recommendations": [], "success": False, "unresolved": unresolved,
                    "error": "None of the players could be resolved to a team and position."}
        result = await lineup_optimizer_tools.get_roster_recommendations(
            players=inputs, week=week, include_reasoning=include_reasoning,
            scoring=scoring, league_id=league_id, season=season,
        )
        if isinstance(result, dict):
            result["unresolved"] = unresolved
        return result

    # Non-empty here: the no-input case returned above and `players` returned.
    assert player_name is not None
    try:
        player_name = validate_string_input(player_name, 'player_name', max_length=100, required=True)
    except ValueError as e:
        return {"recommendation": None, "confidence": 0, "success": False, "error": f"Invalid input: {e!s}"}
    who = lineup_tools.resolve_player(db, player_name, team, position, player_id)
    if not who["team"] or not who["position"]:
        return {"recommendation": None, "confidence": 0, "success": False,
                "error": f"Could not resolve {player_name!r} to a team and position — pass team and position."}
    snap = lineup_tools.recent_snap_share(db, who["player_id"], season, week)
    usage = lineup_tools.recent_usage(db, who["player_id"], who["position"], who["team"],
                                      season, week)
    result = await lineup_optimizer_tools.get_start_sit_recommendation(
        player_name=who["name"] or player_name,
        position=who["position"].upper(),
        team=who["team"].upper(),
        opponent=opponent.upper(),
        player_id=who["player_id"],
        snap_percentage=snap,
        injury_status=injury_status,
        scoring=scoring,
        league_id=league_id,
        season=season,
        week=week,
        usage=usage,
    )
    if isinstance(result, dict):
        result["resolved"] = {"team": who["team"], "position": who["position"],
                              "player_id": who["player_id"], "snap_percentage": snap, **usage}
    return result


@timing_decorator("compare_players_for_slot", tool_type="lineup")
async def compare_players_for_slot(
    players: list,
    slot: str = "FLEX",
    scoring: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
    week: int | None = None,
    roster_id: int | None = None,
    risk_mode: str = "auto",
) -> dict:
    """Compare multiple players competing for the same roster slot.

    Useful for deciding between players for a specific position or flex spot.
    Returns a ranked comparison with the recommended starter.

    Parameters:
        players (list, required): 2-5 players, as names or dicts {name,
            position?, team?, opponent?, player_id?}. Team, position, opponent,
            snap share, injury and practice are looked up when omitted.
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
        roster_id (int, optional): your roster in `league_id`. Then each player
            gets his P(win) — your lineup with him in the slot vs this week's
            opponent — and the risk mode can pick on it.
        risk_mode (str, default "auto"): "auto" maximises P(win) when the
            matchup is known (ceiling when you trail, floor when you lead,
            extra ceiling for a long-shot season by playoff odds) and ranks on
            expected points otherwise; "neutral" = expected points;
            "seek_variance" / "protect_floor" force the tilt.

    Returns: {
        winner: dict with recommended player details (the risk-mode pick),
        points_winner: the expected-points favourite,
        risk: {risk_mode, reason, has_matchup, playoff_pct, choice,
               points_choice, p_win {player: %}, summary ("Starting X over Y
               raises P(win) 41%→44% although mean −0.6") or null},
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
            "Puka Nacua", {"name": "Christian McCaffrey", "team": "SF"}
        ],
        slot="FLEX", league_id="123"
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

    from .. import lineup_tools
    from ..week_context import resolve_season_week
    season, week, _ = await resolve_season_week(season, week)
    players = [lineup_tools.player_input(_context.get_db(), p, season, week) for p in players[:5]]
    # A name the athlete cache cannot place has no team/position to project
    # from (position None used to crash the comparison); report it, as the
    # list mode of get_start_sit_recommendation does.
    unresolved = [p.get("name") for p in players if not (p.get("team") and p.get("position"))]
    players = [p for p in players if p.get("team") and p.get("position")]
    if len(players) < 2:
        return {"winner": None, "comparison": [], "confidence_gap": 0,
                "unresolved": unresolved, "success": False,
                "verdict": "Need at least 2 resolvable players to compare",
                "error": ("Could not resolve team/position for: " + ", ".join(map(str, unresolved))
                          + " — pass team and position, or check the spelling.")}

    result = await lineup_optimizer_tools.compare_players_for_slot(
        players=players,
        slot=slot,
        scoring=scoring,
        league_id=league_id,
        season=season,
        week=week,
        risk_mode=risk_mode,
        roster_id=roster_id,
    )
    if isinstance(result, dict) and unresolved:
        result["unresolved"] = unresolved
    return result


@timing_decorator("analyze_lineup", tool_type="lineup")
async def analyze_lineup(
    league_id: str | None = None,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    lineup: dict | None = None,
    risk_mode: str = "auto",
) -> dict:
    """Grade the lineup YOU have set this week and name the swaps worth making.

    Reads your roster and this week's set starters straight from the league:
    every starter and bench player is projected in the league's scoring (with
    injury, practice, matchup, usage), the best legal lineup is computed with
    the league's slots, and the grade is the share of those optimal points you
    are actually starting. Players whose game has started stay locked. For a
    full weekly plan with win probability use get_weekly_briefing.

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id
        week, season: default to the current NFL week
        lineup (dict, optional): grade a hypothetical lineup instead, keyed by
            slot ({"QB": [{name, team, position, opponent}], ..., "BENCH": [...]});
            league_id then only supplies the scoring
        risk_mode (default "auto"): auto | neutral | seek_variance |
            protect_floor. Auto weighs this week's P(win) against the
            opponent's projected starters and your season playoff odds: an
            underdog or long shot chases ceiling, a favourite protects floor.
            Reported in `risk`; the grade stays on expected points.

    Returns: {
        risk {risk_mode, reason, objective, playoff_pct, win_probability,
              points_optimal_win_probability, projected_points,
              opponent_projected_points, adjustment {swaps, mean_delta,
              summary} | null, recommended_lineup (only when it differs)},
        lineup_grade (A-F), lineup_efficiency_pct, total_projected,
        optimal_projected, optimal_lineup [{slot, player, position,
        projected_points}], suggested_changes [{action (fill|swap), bench_in,
        bench_out, slot (where bench_in starts), out_slot, empty_slot,
        moves [{player, from_slot, to_slot}], gain, reason}],
        marginal_changes (swaps the optimum makes that gain < 2 pts),
        locked_players, weak_spots, starters {slot: [...]},
        bench [...], empty_slots, roster_id, success
    }

    Example: analyze_lineup(league_id="123", roster_id=7)

    IMPORTANT FOR LLM AGENTS: Always provide complete lineup analysis immediately
    without asking for confirmations. Render the grade, weak spots, and suggested changes directly.
    """
    from .. import lineup_tools

    try:
        if league_id:
            league_id = validate_string_input(league_id, 'league_id', max_length=20, required=False)
        if week is not None:
            week = validate_numeric_input(week, min_val=1, max_val=22, required=False)
    except ValueError as e:
        return {"lineup_grade": "N/A", "success": False, "error": f"Invalid input: {e!s}"}
    return await lineup_tools.analyze_lineup(
        league_id=league_id, roster_id=roster_id, user_id=user_id,
        week=week, season=season, lineup=lineup, db=_context.get_db(), risk_mode=risk_mode,
    )


@timing_decorator("get_win_probability_lineup", tool_type="lineup")
async def get_win_probability_lineup(
    your_players: list[dict],
    opponent_players: list[dict],
    slots: dict | None = None,
    stack_correlation: float = 0.35,
    season: int | None = None,
    week: int | None = None,
    risk_mode: str = "auto",
    league_id: str | None = None,
    roster_id: int | None = None,
    opponent_bench: list[dict] | None = None,
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
        risk_mode (str, default "auto"): auto = maximise P(win), plus a further
            ceiling tilt in a close game when the season is a long shot (needs
            league_id + roster_id for the playoff odds); "neutral" = expected
            points; "seek_variance" / "protect_floor" = beat a target above /
            below your own expectation.
        league_id, roster_id (optional): read your playoff odds for auto.
        opponent_bench (list, optional): the opponent's other startable
            players. P(win) stays against opponent_players (the lineup he has
            SET); his best lineup from both is reported as the "if he fixes
            his lineup" risk figure. An opponent player whose game has kicked
            off counts his `actual_points` when given.

    Returns: {
        opponent_projection_basis ("as_given"), opponent_best_lineup_points,
        win_probability_if_opponent_fixes_lineup (with opponent_bench),
        opponent_lineup_issues [{player, issue: bye|out|inactive|doubtful|
            zero_projection}], opponent_locked_players, warnings?,
        risk_mode, risk_reason, risk_objective, playoff_pct,
        risk_adjustment {swaps, mean_delta, summary} | null,
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
        risk_mode=risk_mode,
        league_id=league_id,
        roster_id=roster_id,
        opponent_bench=opponent_bench,
    )


@timing_decorator("get_lineup_contingencies", tool_type="lineup")
async def get_lineup_contingencies(
    league_id: str,
    roster_id: int | None = None,
    week: int | None = None,
    user_id: str | None = None,
    season: int | None = None,
) -> dict:
    """When to decide on a questionable starter, and what to do: keep him,
    swap before the replacement locks, or wait for his inactives.

    Inactives come ~90 min before each kickoff, but every replacement locks at
    ITS kickoff: a Monday-night starter whose bench and free-agent options all
    play Sunday has to be decided before Sunday's earliest relevant kickoff.
    For each starter with an availability question (Q/D/Out, DNP practice,
    news "unlikely"/"game-time decision", list not out yet) this returns his
    kickoff and expected inactives time, expected points, points if active,
    P(plays), the replacements (bench players and instant-add free agents
    that fit his slot -- directly or by moving a flex starter -- each with
    kickoff and projection, plus the drop an add needs on a full roster), and
    the options keep / swap / wait with their expected points. Times are UTC
    and Europe/Vienna (*_local). The same block is `late_game_risk` in
    get_weekly_briefing and analyze_lineup.

    Parameters:
        league_id: Sleeper league id
        roster_id: your roster id (or pass user_id)
        week, season: default to the current NFL week

    Returns: {late_game_risk {at_risk [{player, slot, kickoff_local,
        inactives_expected_local, expected_points, if_active_points,
        play_probability, replacements [...], options {keep, swap, wait},
        recommendation (keep|swap_now|wait|no_alternative),
        decision_deadline_local, summary}], earliest_deadline_local,
        summary}, roster_id, week, season, success}

    Example: get_lineup_contingencies(league_id="1388610560915959808", roster_id=1)
    """
    from .. import lineup_tools

    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
        if week is not None:
            week = validate_numeric_input(week, min_val=1, max_val=22, required=False)
    except ValueError as e:
        return {"success": False, "error": f"Invalid input: {e!s}"}
    result = await lineup_tools.analyze_lineup(
        league_id=league_id, roster_id=roster_id, user_id=user_id, week=week, season=season,
        db=_context.get_db(), risk_mode="neutral")
    if not isinstance(result, dict) or not result.get("success"):
        error = result.get("error") if isinstance(result, dict) else None
        return {"success": False, "error": error or "lineup unavailable"}
    return {
        "success": True,
        "late_game_risk": result.get("late_game_risk"),
        "league": result.get("league"),
        "roster_id": result.get("roster_id"),
        "week": result.get("week"),
        "season": result.get("season"),
        **({"warnings": result["warnings"]} if result.get("warnings") else {}),
    }
