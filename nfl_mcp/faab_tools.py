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
from datetime import UTC, datetime

from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .game_clock import game_lock, parse_kickoff, week_games
from .player_values import get_values_service
from .roster_needs import lineup_gain, lineup_slots
from .sleeper_tools import (
    active_enriched,
    get_league,
    get_nfl_state,
    get_rosters,
    get_transactions,
    get_trending_players,
)
from .teams import normalize_team
from .trade_analyzer_tools import league_format_from_settings
from .waiver_rules import horizon_worth, latest_drops, priority_strategy, trend_demand

logger = logging.getLogger(__name__)

# Regular-season fantasy weeks (playoffs typically start week 15).
_FANTASY_REGULAR_WEEKS = 14
# Never recommend blowing more than this share of budget on a single player.
_MAX_BID_PCT = 75.0


def _now() -> datetime:
    """The clock the waiver timing reads; a seam for tests."""
    return datetime.now(UTC)


async def _waiver_timing_inputs(db, league_id: str, target: dict, season, week) -> dict:
    """Kickoffs (this week, last week) and last drop for one player — what
    `priority_strategy` needs to say when he clears. Every piece is optional:
    missing ones only make the timing coarser."""
    pid = str(target.get("player_id") or "")
    team = normalize_team(target.get("team"))
    if not team and db is not None and pid and hasattr(db, "get_athlete_by_id"):
        try:
            team = normalize_team((db.get_athlete_by_id(pid) or {}).get("team_id"))
        except Exception as e:
            logger.debug(f"athlete lookup failed for {pid}: {e}")
    games = week_games(db, season, week) if team else {}
    previous = week_games(db, season, week - 1) if team and week and week > 1 else {}
    dropped_at = None
    try:
        for wk in {week, max(1, (week or 1) - 1)} if week else set():
            txns = await get_transactions(league_id, week=wk)
            when = latest_drops((txns or {}).get("transactions")).get(pid)
            if when and (dropped_at is None or when > dropped_at):
                dropped_at = when
    except Exception as e:
        logger.debug(f"transactions unavailable for waiver timing: {e}")
    return {
        "game": games.get(team) if team else None,
        "kickoff": parse_kickoff((games.get(team) or {}).get("kickoff")) if team else None,
        "previous_kickoff": parse_kickoff((previous.get(team) or {}).get("kickoff")) if team else None,
        "dropped_at": dropped_at,
    }


async def _horizon_gains(league: dict, roster: dict, target_id: str, season: int,
                         week: int, db) -> dict | None:
    """This week's and the rest-of-season lineup gain of adding the target,
    through the rule get_waiver_targets uses (`waiver_rules.horizon_worth`).
    None when either side cannot be projected."""
    from . import ros
    from .roster_needs import lineup_slots as whole_slots

    unavailable = {str(p) for key in ("reserve", "taxi") for p in (roster.get(key) or [])}
    mine_ids = [str(p) for p in (roster.get("players") or [])
                if p and str(p) != "0" and str(p) not in unavailable]
    if not mine_ids or not target_id:
        return None
    try:
        by_id, meta = await ros.ros_for_ids([*mine_ids, target_id], league=league,
                                            season=season, week=week, db=db)
    except Exception as e:
        logger.warning(f"ROS unavailable for the FAAB horizons: {e}")
        return None
    candidate = by_id.get(target_id)
    mine = [by_id[i] for i in mine_ids if i in by_id]
    windows = (meta or {}).get("windows") or {}
    weeks = sorted(set(windows.get("regular") or []) | set(windows.get("playoff") or []))
    if not candidate or not mine or not weeks:
        return None
    gains = ros.lineup_gains(mine, candidate, whole_slots(league.get("roster_positions")),
                             week, weeks)
    return horizon_worth(gains["week_gain"], gains["ros_gain"], gains["ros_weeks"])


def _slot_takes(slot: str, position: str) -> bool:
    from .lineup_slots import slot_accepts
    return slot_accepts(slot, position)


def _priority_advice(tier: str, upgrade_score: float | None) -> str:
    """How hard to spend a waiver-priority claim: "high", "medium" or "low".

    A priority claim is not free — under rolling waivers a successful one sends
    you to the back of the order — so it is worth spending on a real lineup
    upgrade or a scarce, high-value add, not on depth. `upgrade_score` is None
    without roster context, when only the tier can speak.
    """
    if tier == "must_add":
        return "high"  # scarce enough to claim even as depth
    if upgrade_score is None:
        return "medium" if tier in ("strong", "solid") else "low"
    if upgrade_score <= 0:
        return "low"
    if upgrade_score >= 0.25 and tier in ("strong", "solid"):
        return "high"
    return "medium" if tier in ("strong", "solid", "speculative") else "low"


def _priority_message(advice: str, name: str, tier: str, value: float, upgrade: float,
                      has_roster: bool, rolling: bool, clear_days,
                      strategy: dict | None = None, horizons: dict | None = None) -> str:
    cost = (" Rolling waivers: a successful claim sends you to the back of the order."
            if rolling else "")
    # With timing known, the shared strategy says what to do and when, so this
    # message and get_waiver_targets never give two different answers.
    if strategy:
        cost += f" {strategy['recommendation'].upper()}: {strategy['reason']}"
    free = bool(strategy) and strategy["recommendation"] == "add_now"
    gain = f"+{int(upgrade)} to your best lineup" if has_roster else "no roster context"
    if horizons:
        gain = (f"+{horizons['week_gain']} pts this week, +{horizons['ros_gain']} rest of "
                f"season to your best lineup")
    view = (" Weighs this week and rest of season, as get_waiver_targets does."
            if horizons else " Season-long view; get_waiver_targets answers a one-week need.")
    if advice == "high":
        return (f"Non-FAAB league — {'a must-have add' if free else 'worth a high waiver-priority claim'}"
                f" on {name} [{tier}] (value {int(value)}, {gain}).{cost}")
    if advice == "medium":
        return (f"Non-FAAB league — low-to-middle priority on {name} [{tier}] "
                f"(value {int(value)}, {gain})"
                + ("." if free else ": claim him only if nobody better is on your list.")
                + cost)
    wait = (f"wait the {clear_days} clear day(s) and add him as a free agent"
            if clear_days else "wait until he clears waivers and add him as a free agent")
    return (f"Non-FAAB league — don't burn waiver priority on {name} [{tier}] "
            f"(value {int(value)}, {gain})" + ("." if strategy else f": {wait}.")
            + f"{cost}{view}")


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
    player_id: str | None = None,
    player_name: str | None = None,
    my_roster_id: int | None = None,
    db=None,
) -> dict:
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
    position = (target.get("position") or "").upper()
    # Market values are priced for a stock league at this PPR; this league's
    # other settings (TE premium, 6-pt pass TD …) move them per position.
    model = fmt["scoring_model"]
    target_value = float(target.get("value") or 0) * model.value_multiplier(position)

    # A player someone already rosters is not a waiver target at any price —
    # reserve and taxi included, since those are owned too.
    rosters_res = await get_rosters(league_id)
    all_rosters = rosters_res.get("rosters", []) if rosters_res.get("success") else []
    target_id = str(target.get("player_id") or player_id or "")
    owner = next((
        r for r in all_rosters
        if target_id and target_id in {
            str(pid) for key in ("players", "reserve", "taxi") for pid in (r.get(key) or [])
        }
    ), None)
    if owner is not None:
        return create_success_response({
            "recommendation": None,
            "is_faab_league": is_faab,
            "rostered_by": owner.get("roster_id"),
            "message": (
                f"{target.get('name')} is already on your roster — nothing to claim."
                if my_roster_id is not None and owner.get("roster_id") == my_roster_id else
                f"{target.get('name')} is already rostered (roster {owner.get('roster_id')}) "
                "— not on waivers; use analyze_trade to acquire him."),
        })
    max_value = max((float(v.get("value") or 0) for v in values.get("list", [])), default=target_value or 1)

    warnings: list[str] = []

    # --- Marginal upgrade vs your roster ---
    upgrade = target_value
    replacement_value = 0.0
    my_roster = None
    if my_roster_id is not None:
        for r in all_rosters:
            if r.get("roster_id") == my_roster_id:
                my_roster = r
                break
        if my_roster is not None:
            # The league's own slots, FLEX included, scored as the change in the
            # best starting lineup at market value. A fixed table (RB2/WR2/TE1,
            # no FLEX, always a K) called a TE who would start over the TE1
            # "depth" while get_waiver_targets ranked him the top claim.
            slots = lineup_slots(league.get("roster_positions"))
            mine = []
            # Exclude IR/taxi: a stashed RB1 counted as a live starter, which
            # inflated the bar and produced "you're already strong at RB" for
            # exactly the roster that needs the replacement.
            for p in active_enriched(my_roster):
                v = service.lookup(values, player_id=p.get("player_id"), name=p.get("full_name"))
                if v and v.get("value") is not None:
                    pos = (p.get("position") or "").upper()
                    mine.append({"position": pos, "projected_points":
                                 float(v["value"]) * model.value_multiplier(pos)})
            upgrade = max(0.0, lineup_gain(mine, slots, {"position": position,
                                                          "projected_points": target_value}))
            replacement_value = target_value - upgrade
            if not any(_slot_takes(slot, position) for slot in slots):
                warnings.append(f"League {league_id} starts no {position} — he cannot enter your lineup")
            elif upgrade <= 0:
                warnings.append(f"You're already strong at {position} — this is depth, not an upgrade")
        else:
            warnings.append(f"Roster {my_roster_id} not found; bidding on absolute value only")

    value_score = min(1.0, target_value / max_value) if max_value else 0.0
    upgrade_score = min(1.0, (upgrade / target_value)) if target_value else 0.0

    # --- Demand (how contested is he) ---
    demand_mult = 1.0
    demand_label = "low"
    trend_rank = None
    try:
        trend = await get_trending_players(db, "add", 48, 100)
        if trend.get("success"):
            order = [str(tp.get("player_id")) for tp in trend.get("trending_players", [])]
            pid = str(target.get("player_id"))
            if pid in order:
                idx = order.index(pid)
                trend_rank = idx
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
    wk = season = None
    try:
        state = await get_nfl_state()
        wk = state.get("nfl_state", {}).get("week") if state.get("success") else None
        season = state.get("nfl_state", {}).get("season") if state.get("success") else None
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
        remaining_budget = max(0, (total_budget - used) if used is not None else total_budget)
        # Every number is capped by what is left. `remaining or 10**9` treated
        # an exhausted budget as unlimited and recommended bids you cannot make.
        bid_absolute = min(round(bid_pct / 100.0 * total_budget), remaining_budget)
        if bid_absolute >= remaining_budget * 0.9 and remaining_budget > 0:
            warnings.append("This would use most of your remaining budget")
        if remaining_budget == 0:
            warnings.append("No FAAB left — only $0 bids are possible")
        aggressive_abs = min(round(bid_pct * 1.25 / 100.0 * total_budget), remaining_budget)
        safe_abs = min(round(bid_pct * 0.7 / 100.0 * total_budget), remaining_budget)
    else:
        warnings.append("Not a FAAB league (waiver priority) — use your claim priority instead of a $ bid")

    has_roster = my_roster is not None
    try:
        wk_int = int(wk) if wk else None
        season_int = int(season) if season else None
    except (TypeError, ValueError):
        wk_int = season_int = None

    # Both horizons, this week and rest of season, in lineup points — the
    # same computation and rule get_waiver_targets reports per target.
    horizons = None
    if has_roster and db is not None and wk_int and season_int:
        horizons = await _horizon_gains(league, my_roster, target_id, season_int, wk_int, db)
    if horizons and horizons.get("worth") is None:
        horizons = None

    priority_advice = None if is_faab else (
        horizons["worth"] if horizons else
        _priority_advice(tier, upgrade_score if has_roster else None))
    rolling = not is_faab and settings.get("waiver_type") == 0

    # Non-FAAB: when he clears, whether he is contested, and so whether to
    # claim now, wait for free agency or leave him — the same helper and, with
    # both horizons known, the same worth get_waiver_targets uses.
    waiver_strategy = None
    lock = None
    if not is_faab:
        timing = await _waiver_timing_inputs(db, league_id, target, season_int, wk_int)
        now = _now()
        lock = game_lock(timing["game"], now)
        waiver_strategy = priority_strategy(
            league, my_roster, worth=priority_advice, demand=trend_demand(trend_rank),
            kickoff=timing["kickoff"], previous_kickoff=timing["previous_kickoff"],
            dropped_at=timing["dropped_at"], now=now,
            this_week=bool(horizons and horizons["this_week_only"]),
        )

    reasoning = [
        f"Market value {int(target_value)} ({position} #{target.get('position_rank')})",
        (f"Marginal upgrade for you: +{int(upgrade)} to your best starting lineup "
         f"(he displaces {int(replacement_value)} of value)"
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
            # Non-FAAB only: how hard to spend a priority claim on him.
            "priority_advice": priority_advice,
            # Lineup gain this week and rest of season, and the combined
            # worth (`waiver_rules.horizon_worth`) — None without a roster
            # or projections, when the market-value tier decides.
            "horizons": horizons,
            # Non-FAAB only: claim now / wait / add now / don't bother, with
            # your waiver position and when he clears.
            "waiver_strategy": waiver_strategy,
            "kickoff": (lock or {}).get("kickoff"),
            "kickoff_local": (lock or {}).get("kickoff_local"),
            "locked": (lock or {}).get("locked"),
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
        # The bid is on market value, which is season-long. The claim advice
        # (non-FAAB) weighs this week and rest of season (`horizons`) by the
        # rule get_waiver_targets uses, so the two agree on a player.
        "horizon": "rest_of_season",
        "horizons_reported": ["this_week", "rest_of_season"] if horizons else ["rest_of_season"],
        "scoring_used": model.summary(),
        "total_budget": total_budget if is_faab else None,
        "remaining_budget": remaining_budget,
        "message": (
            (f"Bid ~{bid_pct}% "
             + (f"(${bid_absolute} of {total_budget}) " if bid_absolute is not None else "")
             + f"on {target.get('name')} [{tier}]")
            if is_faab else
            _priority_message(priority_advice, target.get("name"), tier, target_value,
                              upgrade, has_roster, rolling, settings.get("waiver_clear_days"),
                              waiver_strategy, horizons)
        ),
    })
