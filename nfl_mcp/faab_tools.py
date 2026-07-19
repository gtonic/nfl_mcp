"""
FAAB (Free Agent Acquisition Budget) bid recommendations.

Waiver claims are often where leagues are won or lost, yet most managers bid on
gut feeling. This turns a bid into a data-driven number by combining:

    - the player's real market value      (player_values / FantasyCalc)
    - the marginal upgrade for YOUR roster (value over your current starter at
      that position)
    - league demand                        (how many managers are adding him -
      get_trending_players)
    - budget & timing                      (your remaining FAAB, weeks left)

Output is a recommended bid as a percentage of the total FAAB budget (plus an
absolute number when the budget is known), a tier, an aggressive/safe range, and
a transparent breakdown.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .sleeper_tools import get_league, get_rosters, get_trending_players, get_nfl_state
from .player_values import get_values_service
from .trade_analyzer_tools import league_format_from_settings
from .errors import create_success_response, create_error_response, ErrorType, handle_http_errors

logger = logging.getLogger(__name__)

# Starter slots per position (used to find your replacement-level player).
_STARTER_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1, "DST": 1}
# Regular-season fantasy weeks (playoffs typically start week 15).
_FANTASY_REGULAR_WEEKS = 14
# Never recommend blowing more than this share of budget on a single player.
_MAX_BID_PCT = 75.0


def _tier(pct: float) -> str:
    if pct >= 30:
        return "must_add"
    if pct >= 15:
        return "strong"
    if pct >= 5:
        return "solid"
    if pct >= 1:
        return "speculative"
    return "hold_or_stream"


@handle_http_errors(default_data={"recommendation": None}, operation_name="recommending FAAB bid")
async def recommend_faab_bid(
    league_id: str,
    player_id: Optional[str] = None,
    player_name: Optional[str] = None,
    my_roster_id: Optional[int] = None,
    db=None,
) -> Dict:
    """Recommend a FAAB waiver bid for a player (as % of budget, + absolute).

    Args:
        league_id: Sleeper league id.
        player_id: Sleeper player id of the target (preferred).
        player_name: Player name (fallback lookup).
        my_roster_id: Your roster id — enables roster-need (marginal upgrade)
            weighting. Without it, the bid reflects absolute value + demand only.

    Returns: {recommendation: {bid_pct, bid_absolute, range, tier, reasoning,
              breakdown, ...}, success}
    """
    if not player_id and not player_name:
        return create_error_response("Provide player_id or player_name", ErrorType.VALIDATION,
                                     {"recommendation": None})

    # --- League format + budget context ---
    league_res = await get_league(league_id)
    if not league_res.get("success") or not league_res.get("league"):
        return create_error_response(f"Could not load league: {league_res.get('error')}",
                                     ErrorType.HTTP, {"recommendation": None})
    league = league_res["league"]
    fmt = league_format_from_settings(league)
    settings = league.get("settings", {}) or {}
    total_budget = settings.get("waiver_budget", 0) or 0
    is_faab = settings.get("waiver_type") == 2 and total_budget > 0

    # --- Values ---
    service = get_values_service(db)
    values = await service.get_values(fmt["ppr"], fmt["num_qbs"], fmt["num_teams"], fmt["is_dynasty"])
    target = service.lookup(values, player_id=player_id, name=player_name)
    if not target:
        return create_success_response({
            "recommendation": None,
            "is_faab_league": is_faab,
            "message": (f"'{player_id or player_name}' not in the consensus value list "
                        "(deep bench / K / DST) — minimal FAAB (0-1%) or a priority claim."),
        })
    target_value = float(target.get("value") or 0)
    position = (target.get("position") or "").upper()
    max_value = max((float(v.get("value") or 0) for v in values.get("list", [])), default=target_value or 1)

    warnings: List[str] = []

    # --- Marginal upgrade vs your roster ---
    upgrade = target_value
    replacement_value = 0.0
    my_roster = None
    if my_roster_id is not None:
        rosters_res = await get_rosters(league_id)
        for r in (rosters_res.get("rosters", []) if rosters_res.get("success") else []):
            if r.get("roster_id") == my_roster_id:
                my_roster = r
                break
        if my_roster is not None:
            my_pos_vals = []
            for p in my_roster.get("players_enriched", []):
                if (p.get("position") or "").upper() == position:
                    v = service.lookup(values, player_id=p.get("player_id"), name=p.get("full_name"))
                    if v and v.get("value") is not None:
                        my_pos_vals.append(float(v["value"]))
            my_pos_vals.sort(reverse=True)
            slots = _STARTER_SLOTS.get(position, 2)
            if len(my_pos_vals) >= slots:
                replacement_value = my_pos_vals[slots - 1]  # your last starter at the position
            upgrade = max(0.0, target_value - replacement_value)
            if upgrade <= 0:
                warnings.append(f"You're already strong at {position} — this is depth, not an upgrade")
        else:
            warnings.append(f"Roster {my_roster_id} not found; bidding on absolute value only")

    value_score = min(1.0, target_value / max_value) if max_value else 0.0
    upgrade_score = min(1.0, (upgrade / target_value)) if target_value else 0.0

    # --- Demand (how contested is he) ---
    demand_mult = 1.0
    demand_label = "low"
    try:
        trend = await get_trending_players(db, "add", 48, 100)
        if trend.get("success"):
            order = [str(tp.get("player_id")) for tp in trend.get("trending_players", [])]
            pid = str(target.get("player_id"))
            if pid in order:
                idx = order.index(pid)
                if idx < 10:
                    demand_mult, demand_label = 1.30, "high"
                elif idx < 30:
                    demand_mult, demand_label = 1.15, "moderate"
                else:
                    demand_mult, demand_label = 1.05, "light"
    except Exception as e:
        logger.debug(f"trending fetch failed: {e}")

    # --- Timing (weeks left) ---
    timing_mult = 1.0
    weeks_left = None
    try:
        state = await get_nfl_state()
        wk = state.get("nfl_state", {}).get("week") if state.get("success") else None
        if wk:
            weeks_left = max(0, _FANTASY_REGULAR_WEEKS - int(wk))
            if weeks_left <= 3:
                timing_mult = 1.2  # spend it before playoffs
                warnings.append("Few weeks left — spend aggressively if contending")
    except Exception:
        pass

    # --- Bid model ---
    base_pct = 100.0 * value_score * (0.5 + 0.5 * upgrade_score)
    bid_pct = round(min(_MAX_BID_PCT, base_pct * demand_mult * timing_mult), 1)
    tier = _tier(bid_pct)

    # Budget context
    remaining_budget = None
    bid_absolute = None
    aggressive_abs = safe_abs = None
    if is_faab:
        used = (my_roster.get("settings", {}) or {}).get("waiver_budget_used") if my_roster else None
        remaining_budget = (total_budget - used) if used is not None else total_budget
        bid_absolute = round(bid_pct / 100.0 * total_budget)
        if remaining_budget is not None:
            bid_absolute = min(bid_absolute, remaining_budget)
            if bid_absolute >= remaining_budget * 0.9 and remaining_budget > 0:
                warnings.append("This would use most of your remaining budget")
        aggressive_abs = min(round(bid_pct * 1.25 / 100.0 * total_budget), remaining_budget or 10**9)
        safe_abs = round(bid_pct * 0.7 / 100.0 * total_budget)
    else:
        warnings.append("Not a FAAB league (waiver priority) — use your claim priority instead of a $ bid")

    reasoning = [
        f"Market value {int(target_value)} ({position} #{target.get('position_rank')})",
        (f"Marginal upgrade for you: +{int(upgrade)} over your replacement ({int(replacement_value)})"
         if my_roster_id is not None else "No roster context — absolute value used"),
        f"League demand: {demand_label}",
    ]
    if weeks_left is not None:
        reasoning.append(f"{weeks_left} regular-season weeks left")

    return create_success_response({
        "recommendation": {
            "player": target.get("name"),
            "position": position,
            "bid_pct": bid_pct,
            "bid_absolute": bid_absolute,
            "range_pct": {"safe": round(bid_pct * 0.7, 1), "aggressive": round(min(_MAX_BID_PCT, bid_pct * 1.25), 1)},
            "range_absolute": {"safe": safe_abs, "aggressive": aggressive_abs},
            "tier": tier,
            "reasoning": reasoning,
            "warnings": warnings,
            "breakdown": {
                "value_score": round(value_score, 3),
                "upgrade_score": round(upgrade_score, 3),
                "demand_mult": demand_mult,
                "timing_mult": timing_mult,
                "base_pct": round(base_pct, 1),
            },
        },
        "is_faab_league": is_faab,
        "total_budget": total_budget if is_faab else None,
        "remaining_budget": remaining_budget,
        "message": (f"Bid ~{bid_pct}% "
                    + (f"(${bid_absolute} of {total_budget}) " if bid_absolute is not None else "")
                    + f"on {target.get('name')} [{tier}]"),
    })
