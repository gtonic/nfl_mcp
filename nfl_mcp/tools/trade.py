"""Trade analysis, trade targets, trade market and FantasyCalc player values.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import (
    player_values,
    trade_analyzer_tools,
    trade_finder_tools,
)
from ..config import (
    validate_numeric_input,
    validate_string_input,
)
from ..metrics import timing_decorator
from . import _context

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
    include_trending: bool = True,
    suggest_counters: bool = False,
    risk_mode: str = "auto",
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
        suggest_counters (bool, default False): also return `counter_offers` —
            1-3 adjusted versions (add a bench sweetener, ask for less, send a
            player at their need) that keep team 1's ROS gain while raising
            team 2's acceptance_likelihood. Team 1 is YOU.
        risk_mode (str, default "auto"): team 1's season lens — auto reads your
            playoff odds: a long shot weighs lineup ceiling (risk.upside_gain),
            a contender the fantasy-playoff weeks (risk.playoff_gain); also
            "neutral" | "seek_variance" | "protect_floor". The verdict stays on
            ros_points_delta.

    Market-value fairness counts unpriced players (K/DEF/deep bench) as zero
    and discounts extra bodies in an uneven-count deal unless they would start
    for the receiver, so padding a deal does not buy fairness. Alongside it,
    `ros_points_delta` is each team's rest-of-season change in its best
    lineup (every remaining week re-optimised) — prefer it for the call;
    `verdict` leads with it.

    Timing: each traded player carries value_trajectory {trajectory, signal
    (sell_high / buy_low / hold), expected_value_change, market_gap, reasons}
    — e.g. a backup whose starter returns is a sell_high — and each side has
    timing_notes ("You are selling high on X", "buying low on Y", or the
    reverse as a caution). Timing informs the negotiation; the ROS lineup
    change stays the verdict.

    Returns: {
        risk: {risk_mode, reason, playoff_pct, upside_gain, playoff_gain,
               ros_points_delta, risk_adjusted_delta, note},
        counter_offers: {original {your_gain, their_gain, acceptance_likelihood,
               acceptance_label, acceptance_factors, market}, counters [{change,
               you_give, you_get, your_gain, their_gain, acceptance_likelihood,
               expected_gain, ...}], message} | null,
        recommendation: str (fair, needs_adjustment, unfair, etc.),
        fairness_score: float (0-100, higher = more fair),
        verdict: str, ros_points_delta: {team1, team2}, ros: {...},
        team1_analysis: {gives, receives (each with value_trajectory),
                         positional_needs, timing_notes},
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
            nfl_db=_context.get_db(),
            include_trending=include_trending,
            suggest_counters=bool(suggest_counters),
            risk_mode=risk_mode,
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
                dynasty=dynasty, db=_context.get_db())
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
        dynasty=dynasty, position=position, limit=limit, db=_context.get_db(),
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
    max_package_size: int = 2,
    risk_mode: str = "auto",
) -> dict:
    """START HERE for "who should I trade with" - finds the deal, not just grades one.

    analyze_trade evaluates a trade you already have in mind; this finds which
    trades are worth proposing. Every one-for-one swap against every other roster
    — and, up to max_package_size players a side, package trades (2-for-1,
    1-for-2, 2-for-2; 3-for-2 at size 3) — is scored by recomputing BOTH teams'
    best legal starting lineup before and after it, and only trades where both
    sides gain are returned — a trade the other manager loses is a wish, not a
    deal. Packages are built from each side's surplus (players who start in
    under half the remaining weeks) plus at most one starter; a side receiving
    more players than it sends drops its least valuable active player when the
    roster is full (reported as your_drops / their_drops).

    Ranking adds a small timing bonus (reported separately in `timing`, never
    used to pass the both-sides bar): +1 per player you sell high (his value
    is about to fall — e.g. a backup whose starter returns) or buy low (value
    rising — a growing role, a return from injury), -1 for the reverse. Each
    player carries value_trajectory {trajectory, signal, reasons}.

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
            "week" — this week's lineup only (the old behaviour, 1-for-1 only).
        max_package_size: 1 = one-for-one only; 2 (default) adds 2-for-1,
            1-for-2 and 2-for-2; 3 adds 3-for-2 and 2-for-3 (slower).
        risk_mode: "auto" (default) reads your playoff odds: a long shot ranks
            by ceiling too (risk.upside_gain: the weekly lineup's ceiling beyond
            its mean), a contender by fantasy-playoff weeks (risk.playoff_gain);
            "neutral" | "seek_variance" | "protect_floor". your_gain stays the
            pure ROS delta and the both-sides bar. For partners' needs and
            acceptance likelihood use get_trade_market.

    Returns: {
        risk_mode, risk_reason, playoff_pct,
        proposals [{partner, partner_roster_id, shape, you_give, you_get,
                    your_gain, their_gain, mutual_gain, timing {score, bonus,
                    notes}, rank_score, risk {risk_mode, upside_gain,
                    playoff_gain, risk_score}}]  (one-for-one, best per partner),
        package_proposals [{... you_give: [..], you_get: [..], your_drops,
                    their_drops ...}]  (best package per partner),
        package_search {shapes, screened, rescored_weekly, search_seconds},
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
        horizon=horizon, max_package_size=max_package_size, risk_mode=risk_mode,
    )


@timing_decorator("get_trade_market", tool_type="trade")
async def get_trade_market(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    offer: dict | None = None,
    risk_mode: str = "auto",
    max_partners: int = 5,
    week: int | None = None,
    season: int | None = None,
) -> dict:
    """The league's trade market for YOUR roster: who needs what, who has it,
    and who would say yes.

    One read of the whole league: every roster's needs and surpluses by
    position (from rest-of-season lineup impact — how much of a solid
    starter's points its weekly best lineup would gain; surplus = players the
    keenest other teams would start that this lineup can spare), bye-week
    crunches and injured starters, and each team's situation (record, playoff
    odds, contender / bubble / long_shot). Then your natural partners (their
    need ∩ your surplus and vice versa) with candidate packages pre-scored by
    find_trade_targets (both lineups must gain over the rest of the season),
    each with an acceptance_likelihood (0-1) and the factors behind it: their
    lineup gain, FantasyCalc market balance, their positional need, whether
    they give from depth, contender vs long shot, value timing. Pass `offer`
    to get counter offers. Use find_trade_targets for the raw search,
    analyze_trade to grade one deal.

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id
        offer (dict, optional): an offer to counter, {"partner_roster_id": 1,
            "you_give": ["8228", ...], "you_get": ["9997", ...]} (player ids).
            Returns 1-3 adjusted versions that keep your gain while raising
            the partner's acceptance likelihood.
        risk_mode: "auto" (default; long shots weight ceiling, contenders the
            fantasy-playoff weeks) | "neutral" | "seek_variance" | "protect_floor"
        max_partners: partners listed (default 5)
        week, season: default to the current NFL week

    Returns: {
        you {needs {pos: 0-10}, surpluses {pos: 0-10}, surplus_players,
             weak_spots, bye_crunch, injured_starters, situation, posture,
             playoff_pct, record},
        teams [same, every roster],
        partners [{partner_roster_id, partner, situation, fit_score,
                   natural_partner, they_need_you_have, you_need_they_have,
                   packages [{you_give, you_get, your_gain, their_gain,
                              acceptance_likelihood, acceptance_label,
                              acceptance_factors, market, expected_gain,
                              risk, timing}]}],
        counter_offers {original, counters [{change, ...}], message} | null,
        risk_mode, risk_reason, trade_deadline, timing, success
    }

    Example: get_trade_market(league_id="123", roster_id=7)
    Example: get_trade_market(league_id="123", roster_id=7,
        offer={"partner_roster_id": 1, "you_give": ["8228"], "you_get": ["9997"]})
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if roster_id is not None:
            roster_id = validate_numeric_input(roster_id, min_val=1, max_val=32, required=False)
        max_partners = validate_numeric_input(max_partners, min_val=1, max_val=20, required=False) or 5
        if offer is not None and not isinstance(offer, dict):
            raise ValueError("offer must be a dict {partner_roster_id, you_give, you_get}")
    except ValueError as e:
        return {"partners": [], "success": False, "error": f"Invalid input: {e!s}"}
    from .. import trade_market
    return await trade_market.get_trade_market(
        league_id=league_id, roster_id=roster_id, user_id=user_id, week=week, season=season,
        offer=offer, risk_mode=risk_mode, max_partners=max_partners,
    )
