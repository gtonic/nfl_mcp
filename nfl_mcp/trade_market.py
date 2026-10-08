"""The league's trade market: who needs what, who has it, and who would say yes.

`find_trade_targets` answers "which trade improves both lineups". A deal also
has to be *accepted*, and that depends on the other manager: whether the trade
fills a hole he can see, whether the market values (the trade calculators every
manager checks) look fair to him, whether he is chasing a title or playing for
next week, and whether he gives up a player he does not use. This module reads
the whole league once and answers:

* every roster's **needs** and **surpluses** by position, from lineup impact
  over the rest of the season (the #245 positional need: how much of a solid
  starter's points — the league's ``num_teams``-th best at the position — the
  roster's weekly best lineup would actually gain), plus its bye-week crunches
  and injured starters;
* every team's **situation**: record, playoff odds (`risk_mode.season_odds`),
  contender / bubble / long shot;
* for the requesting roster, the **natural partners** (their need ∩ my surplus
  and vice versa), with candidate packages pre-scored by `find_trade_targets`
  (both sides must gain) and an **acceptance_likelihood** heuristic;
* a **counter-offer generator**: given an offer, 1-3 adjusted versions that
  keep the user's gain while making it easier for the partner to accept.

acceptance_likelihood is a logistic score, not a fitted model:

    logit = ACCEPT_BIAS
          + W_GAIN    × clamp(their ROS gain per week / GAIN_SCALE, -1.5, 1)
          + W_MARKET  × clamp(market value balance for them / MARKET_SCALE, -2, 1)
          + W_NEED    × their need (0..1) at the positions they receive
          + W_SURPLUS × share of what they give that is their surplus
          + W_SITUATION × situation fit (contender: near-term points; long shot:
                          openness to shake-ups)
          + W_TIMING  × their sell-high / buy-low timing (value_trajectory)

Each factor is reported with its contribution, so the number can be argued
with rather than trusted.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

from .errors import create_success_response

logger = logging.getLogger(__name__)

TRADEABLE_POSITIONS = ("QB", "RB", "WR", "TE")
NEED_SCALE = 10
# A position is a real need / surplus from this level (0..10) up.
MARKET_MIN_LEVEL = 3
# A player starting in fewer than this share of the remaining weeks is
# surplus (trade_finder_tools.STARTER_WEEK_SHARE).
SURPLUS_START_SHARE = 0.5
# A week whose best lineup projects below this share of the roster's median
# week is a crunch (byes or injuries hitting at once).
CRUNCH_SHARE = 0.85
# Weeks counted as "near term" for a contender's appetite.
NEAR_TERM_WEEKS = 4

# acceptance_likelihood weights (see the module docstring).
ACCEPT_BIAS = -0.4
W_GAIN = 1.1
GAIN_SCALE = 2.5          # points per week of the partner's lineup gain = full effect
W_MARKET = 1.3
MARKET_SCALE = 0.25       # a 25% market-value edge either way = full effect
W_NEED = 0.5
W_SURPLUS = 0.3
W_SITUATION = 0.4
W_TIMING = 0.25
HIGH_ACCEPTANCE = 0.6
MEDIUM_ACCEPTANCE = 0.35
# Counter offers keep at least this share of the original gain (and the bar).
COUNTER_KEEP_SHARE = 0.5
MAX_COUNTERS = 3
COUNTER_MIN_IMPROVEMENT = 0.03


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def acceptance_label(p: float) -> str:
    return "high" if p >= HIGH_ACCEPTANCE else "medium" if p >= MEDIUM_ACCEPTANCE else "low"


# --------------------------------------------------------------------------
# League context
# --------------------------------------------------------------------------

async def build_context(league_id: str, *, week: int | None = None, season: int | None = None,
                        db=None, with_odds: bool = True, await_odds: bool = True) -> dict:
    """Everything the market reads, once: league, rosters, ROS for every
    rostered player, market values, playoff odds. ``{"error": ...}`` on a
    blocking failure. ``await_odds=False`` leaves the odds running in
    ``ctx["_odds_task"]`` (see `await_odds`) so other work overlaps them."""
    from . import risk_mode as rm
    from . import ros, sleeper_tools
    from .database import get_shared_db
    from .player_values import get_values_service
    from .roster_needs import lineup_slots
    from .trade_analyzer_tools import league_format_from_settings
    from .trade_finder_tools import _team_names
    from .value_trajectory import annotate
    from .week_context import current_season_week

    started = time.monotonic()
    db = db or get_shared_db()
    if week is None or season is None:
        current = await current_season_week(db)
        week = week or int(current["week"])
        season = season or int(current["season"])
    # The playoff odds run their own rest-of-season projection; started after
    # ours (below) they find the per-week projection caches warm instead of
    # fetching the same weeks twice at once.
    odds_task = None
    try:
        league_resp, roster_state = await asyncio.gather(
            sleeper_tools.get_league(league_id), sleeper_tools.load_rosters(league_id, "lineup"))
    except BaseException:
        if odds_task:
            odds_task.cancel()
        raise
    league = (league_resp or {}).get("league") or {}
    if not league or roster_state["blocking_error"]:
        if odds_task:
            odds_task.cancel()
        return {"error": roster_state["blocking_error"] or f"Could not load league {league_id}."}
    rosters = roster_state["rosters"]
    names = await _team_names(sleeper_tools, league_id, rosters)

    ids_by_roster: dict[int, list[str]] = {}
    reserve_ids: set[str] = set()
    for r in rosters:
        taxi = {str(p) for p in (r.get("taxi") or [])}
        reserve_ids |= {str(p) for p in (r.get("reserve") or [])}
        ids_by_roster[r["roster_id"]] = [str(p) for p in (r.get("players") or [])
                                         if p and str(p) != "0" and str(p) not in taxi]
    all_ids = [pid for ids in ids_by_roster.values() for pid in ids]
    fmt = league_format_from_settings(league)
    values_task = asyncio.create_task(get_values_service(db).get_values(
        ppr=fmt["ppr"], num_qbs=fmt["num_qbs"], num_teams=fmt["num_teams"],
        is_dynasty=fmt["is_dynasty"]))
    try:
        by_id, meta = await ros.ros_for_ids(all_ids, league=league, season=int(season),
                                            week=int(week), db=db)
    except BaseException:
        values_task.cancel()
        if odds_task:
            odds_task.cancel()
        raise
    if with_odds:
        odds_task = asyncio.create_task(rm.season_odds(league_id, db=db))
    annotate(list(by_id.values()), week=week)
    projected_at = time.monotonic()
    try:
        values = await values_task
    except Exception as e:
        logger.warning(f"market values unavailable: {e}")
        values = {"by_id": {}, "by_name": {}, "source": "unavailable", "stale": True}

    regular = list(meta["windows"]["regular"])
    playoff = list(meta["windows"]["playoff"])
    weeks = sorted(set(regular) | set(playoff))
    slots = lineup_slots(league.get("roster_positions"))

    scored: dict[int, list[dict]] = {}
    for rid, ids in ids_by_roster.items():
        out = []
        for pid in ids:
            e = by_id.get(pid)
            if not e or e["position"] not in TRADEABLE_POSITIONS:
                continue
            out.append({**e, "name": e["player"], "on_reserve": pid in reserve_ids,
                        "projected_points": e["total_points"]})
        scored[rid] = out

    ctx = {
        "league_id": league_id, "league": league, "rosters": rosters,
        "roster_by_id": {r["roster_id"]: r for r in rosters},
        "roster_state": roster_state, "names": names, "season": int(season),
        "week": int(week), "by_id": by_id, "meta": meta, "weeks": weeks,
        "regular_weeks": [w for w in regular if w in weeks],
        "playoff_weeks": [w for w in playoff if w in weeks],
        "slots": slots, "scored": scored, "values": values, "fmt": fmt,
        "odds": {}, "_odds_task": odds_task,
        "num_teams": int(league.get("total_rosters") or len(rosters) or 12),
        "bar": max(1.0, 0.5 * len(weeks)),
        "_base": {},
        "timing": {"projection_seconds": round(projected_at - started, 2)},
    }
    if await_odds:
        await await_odds_into(ctx)
    return ctx


async def await_odds_into(ctx: dict) -> None:
    """Wait for the playoff odds started by `build_context` and store them."""
    task = ctx.pop("_odds_task", None)
    if task is None:
        return
    try:
        odds = await task
    except Exception as e:  # additive
        logger.debug(f"playoff odds unavailable for the market: {e}")
        odds = {}
    ctx["odds"] = (odds or {}).get("by_roster") or {}


def market_value(ctx: dict, entry: dict) -> float:
    """FantasyCalc value (league-scoring adjusted), 0 when unpriced."""
    from .player_values import get_values_service
    service = get_values_service()
    hit = service.lookup(ctx["values"], player_id=str(entry.get("player_id") or ""),
                         name=entry.get("name") or entry.get("player"),
                         position=entry.get("position"))
    if not hit:
        return 0.0
    value = float(hit.get("value") or hit.get("redraft_value") or 0.0)
    model = ctx["fmt"].get("scoring_model")
    if model is not None:
        value *= model.value_multiplier(entry.get("position"))
    return round(value, 1)


def base_total(ctx: dict, rid: int) -> float:
    from . import ros
    cache = ctx["_base"]
    if rid not in cache:
        cache[rid] = ros.weekly_lineup_total(ctx["scored"].get(rid, []), ctx["slots"], ctx["weeks"])
    return cache[rid]


def _yardsticks(ctx: dict) -> dict[str, dict]:
    """Per position, the league's ``num_teams``-th best rostered player: a
    solid starter, the bar a need is measured against."""
    if "_yard" in ctx:
        return ctx["_yard"]
    by_pos: dict[str, list[dict]] = {}
    for players in ctx["scored"].values():
        for p in players:
            by_pos.setdefault(p["position"], []).append(p)
    out = {}
    for pos, players in by_pos.items():
        players.sort(key=lambda p: p["total_points"], reverse=True)
        out[pos] = players[min(len(players), ctx["num_teams"]) - 1]
    ctx["_yard"] = out
    return out


# --------------------------------------------------------------------------
# Roster profiles: needs, surpluses, crunches, situation
# --------------------------------------------------------------------------

# Players per roster (best first) checked for tradeable surplus, and how many
# of the keenest other teams the demand for one of them averages over.
SURPLUS_CANDIDATES = 10
DEMAND_TEAMS = 2
# Demand share × unused share at which a player is a full 10/10 surplus: in a
# deep league a strong bench piece adds ~40% of his points to the teams that
# want him most.
SURPLUS_FULL_SHARE = 0.4
MAX_SURPLUS_COST_SHARE = 0.5


def _season_base(ctx: dict, rid: int) -> float:
    from .roster_needs import starting_lineup_total
    cache = ctx.setdefault("_season_base", {})
    if rid not in cache:
        cache[rid] = starting_lineup_total(ctx["scored"].get(rid, []), ctx["slots"])
    return cache[rid]


def league_demand(ctx: dict, owner: int, player: dict) -> float:
    """Season points `player` would add to the lineups of the DEMAND_TEAMS
    other teams that want him most (mean; season-total screen)."""
    from .roster_needs import starting_lineup_total
    gains = []
    for rid, players in ctx["scored"].items():
        if rid == owner or not players:
            continue
        gains.append(starting_lineup_total([*players, player], ctx["slots"]) - _season_base(ctx, rid))
    gains.sort(reverse=True)
    top = gains[:DEMAND_TEAMS]
    return max(0.0, sum(top) / len(top)) if top else 0.0


def tradeable_surplus(ctx: dict, owner: int, player: dict) -> tuple[float, float, float]:
    """``(level 0..1, demand, own cost)``: the share of his points the keenest
    other teams would start, times the share his own lineup does not need.

    A bench player nobody would start is no surplus however many points he
    projects (a backup QB in a one-QB league); a starter whose replacement
    is nearly as good is (a deep WR room's flex)."""
    from .roster_needs import starting_lineup_total
    points = float(player.get("total_points") or 0.0)
    if points <= 0:
        return 0.0, 0.0, 0.0
    without = [p for p in ctx["scored"].get(owner, []) if p is not player]
    cost = max(0.0, _season_base(ctx, owner) - starting_lineup_total(without, ctx["slots"]))
    demand = league_demand(ctx, owner, player)
    # Others' use of his points × (the share his own lineup does not need)²
    # — squared so a team's own RB1 is not called surplus because a backup
    # would cover part of him — scaled so SURPLUS_FULL_SHARE reads as 10. A
    # player whose loss costs more than MAX_SURPLUS_COST_SHARE of his points
    # is a starter, not surplus.
    if cost / points > MAX_SURPLUS_COST_SHARE:
        return 0.0, demand, cost
    level = (demand / points) * max(0.0, 1.0 - cost / points) ** 2
    return max(0.0, min(1.0, level / SURPLUS_FULL_SHARE)), demand, cost


def roster_profile(ctx: dict, rid: int) -> dict:
    from . import ros
    from .lineup_slots import league_starts
    from .roster_needs import starting_lineup, starting_lineup_total
    from .trade_analyzer_tools import _fit_share, _window_points
    from .trade_finder_tools import week_starts

    players = ctx["scored"].get(rid, [])
    slots, weeks = ctx["slots"], ctx["weeks"]
    n_weeks = max(1, len(weeks))
    base = base_total(ctx, rid)
    starts = week_starts(players, slots, weeks) if players else {}
    yard = _yardsticks(ctx)
    positions = [p for p in TRADEABLE_POSITIONS
                 if league_starts(ctx["league"].get("roster_positions"), p)]

    needs: dict[str, int] = {}
    weak_spots = []
    for pos in positions:
        ref = yard.get(pos)
        if not ref:
            continue
        clone = {**ref, "player_id": f"__need_{pos}"}
        gain = ros.weekly_lineup_total([*players, clone], slots, weeks) - base
        needs[pos] = round(NEED_SCALE * _fit_share(gain, _window_points(ref, weeks)))
        starters = [p for p in players if p["position"] == pos
                    and starts.get(str(p["player_id"]), 0) >= SURPLUS_START_SHARE * n_weeks]
        weakest = min(starters, key=lambda p: p["total_points"], default=None)
        weak_spots.append({
            "position": pos,
            "need": needs[pos],
            "weakest_starter": weakest["name"] if weakest else None,
            "weakest_starter_per_week": round(weakest["total_points"] / n_weeks, 1) if weakest else 0.0,
            "league_starter_per_week": round(ref["total_points"] / n_weeks, 1),
            "league_starter": ref["name"],
        })

    surplus: dict[str, int] = dict.fromkeys(positions, 0)
    surplus_players = []
    pool = [p for p in sorted(players, key=lambda p: p["total_points"], reverse=True)
            if p["total_points"] > 0 and p["position"] in surplus][:SURPLUS_CANDIDATES]
    for p in pool:
        share = starts.get(str(p["player_id"]), 0) / n_weeks
        # Surplus is what the league would pay for minus what it costs this
        # lineup (see `tradeable_surplus`).
        frac, demand, cost = tradeable_surplus(ctx, rid, p)
        level = round(NEED_SCALE * frac)
        surplus[p["position"]] = max(surplus[p["position"]], level)
        if level > 0:
            surplus_players.append({
                "player_id": p["player_id"], "name": p["name"], "position": p["position"],
                "team": p.get("team"), "total_points": p["total_points"],
                "start_share": round(share, 2), "surplus_level": level,
                # Season points the keenest teams would add / this lineup
                # loses without him.
                "league_demand": round(demand, 1),
                "lineup_cost": round(cost, 1),
                "market_value": market_value(ctx, p),
            })
    surplus_players.sort(key=lambda p: (p["surplus_level"], p["total_points"]), reverse=True)
    surplus_players = surplus_players[:6]

    # Bye / injury crunches in the regular season.
    crunch = []
    core = [p for p in players
            if starts.get(str(p["player_id"]), 0) >= SURPLUS_START_SHARE * n_weeks]
    totals = {}
    for w in ctx["regular_weeks"]:
        week_players = [{"position": p["position"],
                         "projected_points": float((p.get("weekly_points") or {}).get(w, 0.0) or 0.0)}
                        for p in players]
        totals[w] = starting_lineup_total(week_players, slots)
    if totals:
        typical = sorted(totals.values())[len(totals) // 2]
        for w, total in totals.items():
            if typical > 0 and total < CRUNCH_SHARE * typical:
                missing = [p["name"] for p in core
                           if float((p.get("weekly_points") or {}).get(w, 0.0) or 0.0) <= 0.0]
                crunch.append({"week": w, "lineup_points": round(total, 1),
                               "typical": round(typical, 1), "missing": missing})
    injured = [
        {"name": p["name"], "position": p["position"], "status": p.get("injury_status"),
         "weeks_out": len(p.get("injury_weeks") or [])}
        for p in core if p.get("injury_status") and p.get("injury_status") != "Active"
        and (p.get("injury_weeks") or p.get("on_reserve"))
    ]
    lineup = starting_lineup(players, slots)
    return apply_situation(ctx, {
        "roster_id": rid,
        "name": ctx["names"].get(rid, f"Roster {rid}"),
        "ros_lineup_points": round(base, 1),
        "ros_lineup_per_week": round(base / n_weeks, 1),
        "needs": needs,
        "surpluses": surplus,
        "weak_spots": weak_spots,
        "surplus_players": surplus_players,
        "bye_crunch": crunch,
        "injured_starters": injured,
        "core_starters": [p["name"] for p in lineup],
    })


_POSTURE = {
    "contender": "buyer: wants points now and in the fantasy playoffs, pays with depth",
    "bubble": "balanced: takes a fair deal that raises the lineup",
    "long_shot": "gambler: open to shake-ups and upside, will sell steady depth",
    "unknown": "unknown (no playoff odds)",
}


def apply_situation(ctx: dict, profile: dict) -> dict:
    """Record, playoff odds and contender / bubble / long_shot on a profile
    (re-applied once the odds arrive). Mutates and returns it."""
    from . import risk_mode as rm
    rid = profile["roster_id"]
    odds = ctx["odds"].get(rid) or {}
    settings = (ctx["roster_by_id"].get(rid) or {}).get("settings") or {}
    pct = odds.get("playoff_pct")
    situation = rm.team_situation(pct)
    profile.update({
        "record": odds.get("record") or f"{int(settings.get('wins') or 0)}-{int(settings.get('losses') or 0)}",
        "playoff_pct": pct, "avg_seed": odds.get("avg_seed"), "mean_ppg": odds.get("mean_ppg"),
        "situation": situation, "posture": _POSTURE[situation],
    })
    return profile


# --------------------------------------------------------------------------
# Scoring a concrete trade: gains, market balance, acceptance
# --------------------------------------------------------------------------

def _entries(ctx: dict, rid: int, ids: list[str]) -> tuple[list[dict], list[str]]:
    """(scored entries on roster `rid` for `ids`, ids not scorable there)."""
    held = {str(p["player_id"]): p for p in ctx["scored"].get(rid, [])}
    found, missing = [], []
    for pid in ids:
        e = held.get(str(pid))
        (found.append(e) if e else missing.append(str(pid)))
    return found, missing


def _ros_meta_entry(ctx: dict, pid: str) -> dict:
    e = ctx["by_id"].get(str(pid)) or {}
    return {"player_id": str(pid), "name": e.get("player") or str(pid),
            "position": e.get("position"), "team": e.get("team"),
            "total_points": e.get("total_points")}


def evaluate_trade(ctx: dict, profiles: dict[int, dict], me: int, partner: int,
                   give_ids: list[str], get_ids: list[str]) -> dict:
    """Both lineups' ROS change (with drops when a full roster takes more
    players than it sends), the market balance, and the partner's
    acceptance_likelihood for: `me` sends `give_ids`, receives `get_ids`."""
    from . import ros
    from .trade_finder_tools import _free_slots, roster_after
    from .value_trajectory import compact

    give, give_missing = _entries(ctx, me, give_ids)
    get, get_missing = _entries(ctx, partner, get_ids)
    rp = ctx["league"].get("roster_positions")
    roster_me, roster_them = ctx["roster_by_id"].get(me) or {}, ctx["roster_by_id"].get(partner) or {}
    my_after, my_drops = roster_after(ctx["scored"].get(me, []), tuple(give), tuple(get),
                                      _free_slots(roster_me, rp))
    their_after, their_drops = roster_after(ctx["scored"].get(partner, []), tuple(get), tuple(give),
                                            _free_slots(roster_them, rp))
    my_gain = round(ros.weekly_lineup_total(my_after, ctx["slots"], ctx["weeks"])
                    - base_total(ctx, me), 1)
    their_gain = round(ros.weekly_lineup_total(their_after, ctx["slots"], ctx["weeks"])
                       - base_total(ctx, partner), 1)
    near = ctx["regular_weeks"][:NEAR_TERM_WEEKS]
    their_near = round(ros.weekly_lineup_total(their_after, ctx["slots"], near)
                       - ros.weekly_lineup_total(ctx["scored"].get(partner, []), ctx["slots"], near), 1) \
        if near else 0.0
    give_value = sum(market_value(ctx, p) for p in give)
    get_value = sum(market_value(ctx, p) for p in get)

    acceptance = acceptance_likelihood(
        ctx, profiles.get(partner) or {}, their_gain=their_gain, their_near_gain=their_near,
        they_receive=give, they_send=get, receive_value=give_value, send_value=get_value)

    def _out(p: dict) -> dict:
        return {"player_id": p["player_id"], "name": p["name"], "position": p["position"],
                "team": p.get("team"), "total_points": p["total_points"],
                "market_value": market_value(ctx, p),
                "value_trajectory": compact(p.get("value_trajectory"))}

    n = max(1, len(ctx["weeks"]))
    return {
        "partner_roster_id": partner,
        "partner": ctx["names"].get(partner, f"Roster {partner}"),
        "shape": f"{len(give_ids)}-for-{len(get_ids)}",
        "you_give": [_out(p) for p in give] + [_ros_meta_entry(ctx, i) for i in give_missing],
        "you_get": [_out(p) for p in get] + [_ros_meta_entry(ctx, i) for i in get_missing],
        "your_gain": my_gain,
        "their_gain": their_gain,
        "your_gain_per_week": round(my_gain / n, 2),
        "their_gain_per_week": round(their_gain / n, 2),
        "your_drops": [p["name"] for p in my_drops],
        "their_drops": [p["name"] for p in their_drops],
        "market": {"you_give_value": round(give_value, 1), "you_get_value": round(get_value, 1),
                   "balance_for_them": round(give_value - get_value, 1)},
        "both_gain": my_gain >= ctx["bar"] and their_gain >= ctx["bar"],
        **acceptance,
        "unscored_players": give_missing + get_missing,
    }


def acceptance_likelihood(ctx: dict, partner_profile: dict, *, their_gain: float,
                          their_near_gain: float, they_receive: list[dict], they_send: list[dict],
                          receive_value: float, send_value: float) -> dict:
    """``{acceptance_likelihood, acceptance_label, acceptance_factors}`` for
    the partner (see the module docstring for the formula)."""
    from .value_trajectory import timing_score

    n = max(1, len(ctx["weeks"]))
    factors = []

    def _add(name: str, score: float, weight: float, note: str):
        factors.append({"factor": name, "score": round(score, 2),
                        "contribution": round(score * weight, 2), "note": note})
        return score * weight

    logit = ACCEPT_BIAS
    per_week = their_gain / n
    logit += _add("their_lineup_gain", _clamp(per_week / GAIN_SCALE, -1.5, 1.0), W_GAIN,
                  f"their ROS lineup {their_gain:+.1f} ({per_week:+.2f}/week)")
    if send_value > 0 or receive_value > 0:
        balance = (receive_value - send_value) / max(send_value, receive_value, 1.0)
        logit += _add("market_value", _clamp(balance / MARKET_SCALE, -2.0, 1.0), W_MARKET,
                      f"FantasyCalc: they get {receive_value:.0f} for {send_value:.0f} "
                      f"({balance * 100:+.0f}%)")
    needs = partner_profile.get("needs") or {}
    if they_receive:
        need = sum(needs.get(p["position"], 0) for p in they_receive) / (NEED_SCALE * len(they_receive))
        logit += _add("positional_need", need, W_NEED,
                      "their need at " + ", ".join(
                          f"{p['position']} {needs.get(p['position'], 0)}/10" for p in they_receive))
    surplus_ids = {str(p["player_id"]) for p in partner_profile.get("surplus_players") or []}
    if they_send:
        share = sum(1 for p in they_send if str(p["player_id"]) in surplus_ids) / len(they_send)
        logit += _add("gives_from_surplus", share, W_SURPLUS,
                      f"{share * 100:.0f}% of what they send is bench depth for them")
    situation = partner_profile.get("situation") or "unknown"
    if situation == "contender":
        sit = _clamp(their_near_gain / (GAIN_SCALE * NEAR_TERM_WEEKS), -1.0, 1.0)
        note = f"contender: next {NEAR_TERM_WEEKS} weeks {their_near_gain:+.1f}"
    elif situation == "long_shot":
        sit = 0.5
        note = "long shot: open to shake-ups"
    else:
        sit = 0.0
        note = f"{situation}: no situational lean"
    logit += _add("situation", sit, W_SITUATION, note)
    timing = timing_score(they_send, they_receive)
    logit += _add("value_timing", _clamp(timing / 2.0, -1.0, 1.0), W_TIMING,
                  "their sell-high / buy-low timing " + (f"{timing:+d}" if timing else "neutral"))
    p = _sigmoid(logit)
    return {"acceptance_likelihood": round(p, 2), "acceptance_label": acceptance_label(p),
            "acceptance_factors": factors}


# --------------------------------------------------------------------------
# Counter offers
# --------------------------------------------------------------------------

def counter_offers(ctx: dict, profiles: dict[int, dict], me: int, partner: int,
                   give_ids: list[str], get_ids: list[str],
                   max_counters: int = MAX_COUNTERS) -> dict:
    """1-3 adjusted versions of an offer that keep `me`'s gain (at least
    COUNTER_KEEP_SHARE of it and the both-sides bar) while raising the
    partner's acceptance_likelihood. Variants: add a sweetener from my
    surplus, ask for less (drop or downgrade a piece I receive), or swap what I
    send for a player at the partner's need."""
    original = evaluate_trade(ctx, profiles, me, partner, give_ids, get_ids)
    give_ids = [str(i) for i in give_ids]
    get_ids = [str(i) for i in get_ids]
    keep = max(ctx["bar"], COUNTER_KEEP_SHARE * original["your_gain"])
    their_profile = profiles.get(partner) or {}
    their_needs = their_profile.get("needs") or {}

    variants: list[tuple[str, list[str], list[str]]] = []
    mine = sorted(ctx["scored"].get(me, []), key=lambda p: p["total_points"], reverse=True)
    theirs = sorted(ctx["scored"].get(partner, []), key=lambda p: p["total_points"], reverse=True)
    # 1) Sweeten: add a player my lineup can spare (his loss costs at most
    #    MAX_SURPLUS_COST_SHARE of his points: bench or spare depth),
    #    the partner's needs first; whether my gain survives is checked below.
    spare = []
    for p in mine:
        if str(p["player_id"]) in give_ids or p["total_points"] <= 0:
            continue
        _, _, cost = tradeable_surplus(ctx, me, p)
        if cost / p["total_points"] <= MAX_SURPLUS_COST_SHARE:
            spare.append(p)
    for p in sorted(spare, key=lambda p: (their_needs.get(p["position"], 0), p["total_points"]),
                    reverse=True):
        variants.append((f"add {p['name']} ({p['position']}, depth you can spare) as a sweetener",
                         [*give_ids, str(p["player_id"])], get_ids))
    # 2) Ask for less: drop one piece I receive (multi-player asks).
    if len(get_ids) > 1:
        for pid in get_ids:
            name = _ros_meta_entry(ctx, pid)["name"]
            variants.append((f"drop {name} from your ask",
                             give_ids, [g for g in get_ids if g != pid]))
    # 3) Downgrade an ask: a lesser player of theirs at the same position.
    held = {str(p["player_id"]): p for p in theirs}
    for pid in get_ids:
        ask = held.get(pid)
        if not ask:
            continue
        lesser = [p for p in theirs if p["position"] == ask["position"]
                  and str(p["player_id"]) not in get_ids
                  and p["total_points"] < ask["total_points"]][:2]
        for p in lesser:
            variants.append((f"ask for {p['name']} instead of {ask['name']}",
                             give_ids, [p2 if p2 != pid else str(p["player_id"]) for p2 in get_ids]))
    # 4) Re-shape what I send: one of my players at the partner's biggest need
    #    in place of a piece of mine at a position they need less.
    need_order = sorted(their_needs.items(), key=lambda kv: kv[1], reverse=True)
    my_held = {str(p["player_id"]): p for p in mine}
    for pos, level in need_order[:2]:
        if level < MARKET_MIN_LEVEL:
            continue
        for alt in [p for p in mine if p["position"] == pos
                    and str(p["player_id"]) not in give_ids][:2]:
            for pid in give_ids:
                g = my_held.get(pid)
                if g and g["position"] != pos and their_needs.get(g["position"], 0) < level:
                    variants.append((f"send {alt['name']} ({pos}, their need {level}/10) "
                                     f"instead of {g['name']}",
                                     [x if x != pid else str(alt["player_id"]) for x in give_ids],
                                     get_ids))

    seen = {(tuple(sorted(give_ids)), tuple(sorted(get_ids)))}
    counters = []
    for change, g_ids, r_ids in variants:
        key = (tuple(sorted(g_ids)), tuple(sorted(r_ids)))
        if key in seen or not g_ids or not r_ids:
            continue
        seen.add(key)
        ev = evaluate_trade(ctx, profiles, me, partner, g_ids, r_ids)
        if ev["your_gain"] < keep or ev["their_gain"] <= 0:
            continue
        if ev["acceptance_likelihood"] < original["acceptance_likelihood"] + COUNTER_MIN_IMPROVEMENT:
            continue
        ev["change"] = change
        ev["expected_gain"] = round(ev["acceptance_likelihood"] * ev["your_gain"], 1)
        counters.append(ev)
    counters.sort(key=lambda c: (c["expected_gain"], c["acceptance_likelihood"]), reverse=True)
    original["expected_gain"] = round(original["acceptance_likelihood"] * original["your_gain"], 1)
    return {
        "original": original,
        "counters": counters[:max_counters],
        "variants_checked": len(seen) - 1,
        "keep_your_gain_at_least": round(keep, 1),
        "message": (f"{len(counters[:max_counters])} counter(s) keep at least "
                    f"{round(keep, 1)} of your ROS gain and raise their acceptance "
                    f"from {original['acceptance_likelihood']:.0%}."
                    if counters else
                    "No adjustment keeps your gain and makes the deal easier to accept "
                    f"(original acceptance {original['acceptance_likelihood']:.0%})."),
    }


async def counters_for_trade(league_id: str, my_roster_id: int, partner_roster_id: int,
                             give_ids: list[str], get_ids: list[str], db=None) -> dict:
    """`counter_offers` for analyze_trade (team1 = the user). Never raises."""
    try:
        ctx = await build_context(league_id, db=db)
        if ctx.get("error"):
            return {"error": ctx["error"], "counters": []}
        profiles = {rid: roster_profile(ctx, rid) for rid in (my_roster_id, partner_roster_id)}
        return counter_offers(ctx, profiles, my_roster_id, partner_roster_id,
                              [str(i) for i in give_ids], [str(i) for i in get_ids])
    except Exception as e:
        logger.warning(f"counter offers unavailable: {e}")
        return {"error": str(e), "counters": []}


# --------------------------------------------------------------------------
# Partners and the tool
# --------------------------------------------------------------------------

def partner_fit(me: dict, them: dict, my_players: dict[str, list[str]],
                their_players: dict[str, list[str]]) -> dict:
    """Where their need meets my surplus and vice versa."""
    they_need = []
    you_need = []
    for pos, level in (them.get("needs") or {}).items():
        mine = (me.get("surpluses") or {}).get(pos, 0)
        if level >= MARKET_MIN_LEVEL and mine >= MARKET_MIN_LEVEL:
            they_need.append({"position": pos, "their_need": level, "your_surplus": mine,
                              "your_players": my_players.get(pos, [])})
    for pos, level in (me.get("needs") or {}).items():
        theirs = (them.get("surpluses") or {}).get(pos, 0)
        if level >= MARKET_MIN_LEVEL and theirs >= MARKET_MIN_LEVEL:
            you_need.append({"position": pos, "your_need": level, "their_surplus": theirs,
                             "their_players": their_players.get(pos, [])})
    score = (sum(min(x["their_need"], x["your_surplus"]) for x in they_need)
             + sum(min(x["your_need"], x["their_surplus"]) for x in you_need))
    return {"they_need_you_have": they_need, "you_need_they_have": you_need,
            "fit_score": score, "natural_partner": bool(they_need and you_need)}


def _surplus_names(profile: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in profile.get("surplus_players") or []:
        out.setdefault(p["position"], []).append(p["name"])
    return out


async def get_trade_market(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    offer: dict | None = None,
    risk_mode: str | None = "auto",
    max_partners: int = 5,
    packages_per_partner: int = 3,
) -> dict:
    """The league's trade market for one roster: every team's needs and
    surpluses, its situation, the natural partners with pre-scored packages
    and their acceptance likelihood, and counter offers for `offer`."""
    from . import risk_mode as rm
    from . import sleeper_tools, trade_finder_tools
    from .briefing_tools import _staleness_warnings
    from .database import get_shared_db

    started = time.monotonic()
    try:
        risk_mode = rm.normalize_risk_mode(risk_mode)
    except ValueError as e:
        return create_success_response({"success": False, "error": str(e)})
    db = get_shared_db()
    ctx = await build_context(league_id, week=week, season=season, db=db, await_odds=False)
    if ctx.get("error"):
        return create_success_response({"success": False, "error": ctx["error"]})
    mine, error = sleeper_tools.find_roster(ctx["rosters"], league_id, roster_id, user_id,
                                            purpose="advise")
    if error:
        if ctx.get("_odds_task"):
            ctx["_odds_task"].cancel()
        return create_success_response({"success": False, "error": error})
    me = mine["roster_id"]

    profiles = {rid: roster_profile(ctx, rid) for rid in ctx["scored"]}
    profiled_at = time.monotonic()

    # Candidate packages: the trade finder's both-sides-gain search on the
    # same ROS data, several per partner.
    finder = await trade_finder_tools.find_trade_targets(
        league_id, me, week=ctx["week"], season=ctx["season"], limit=ctx["num_teams"],
        max_package_size=2, risk_mode=risk_mode, per_partner=packages_per_partner,
        ros_data=(ctx["by_id"], ctx["meta"]))
    searched_at = time.monotonic()
    await await_odds_into(ctx)
    for prof in profiles.values():
        apply_situation(ctx, prof)
    proposals = list((finder or {}).get("proposals") or []) + list(
        (finder or {}).get("package_proposals") or [])

    def _ids(side) -> list[str]:
        side = side if isinstance(side, list) else [side]
        return [str(p.get("player_id")) for p in side]

    by_partner: dict[int, list[dict]] = {}
    for prop in proposals:
        partner = prop["partner_roster_id"]
        ev = evaluate_trade(ctx, profiles, me, partner, _ids(prop["you_give"]), _ids(prop["you_get"]))
        ev["timing"] = prop.get("timing")
        ev["risk"] = prop.get("risk")
        # The finder's pure ROS delta, as the finder scored it.
        ev["finder_your_gain"] = prop.get("your_gain")
        ev["finder_their_gain"] = prop.get("their_gain")
        ev["expected_gain"] = round(ev["acceptance_likelihood"] * ev["your_gain"], 1)
        by_partner.setdefault(partner, []).append(ev)

    my_names = _surplus_names(profiles[me])
    partners = []
    for rid, prof in profiles.items():
        if rid == me:
            continue
        fit = partner_fit(profiles[me], prof, my_names, _surplus_names(prof))
        packages = sorted(by_partner.get(rid, []),
                          key=lambda e: (e["expected_gain"], e["your_gain"]), reverse=True)
        if not packages and not fit["natural_partner"]:
            continue
        partners.append({
            "partner_roster_id": rid, "partner": prof["name"], "record": prof["record"],
            "playoff_pct": prof["playoff_pct"], "situation": prof["situation"],
            "posture": prof["posture"], **fit,
            "packages": packages[:packages_per_partner],
            "best_expected_gain": packages[0]["expected_gain"] if packages else 0.0,
        })
    partners.sort(key=lambda p: (p["best_expected_gain"], p["natural_partner"], p["fit_score"]),
                  reverse=True)
    partners = partners[:max(1, int(max_partners or 5))]

    counters = None
    if offer:
        partner = offer.get("partner_roster_id")
        give_ids = [str(i) for i in offer.get("you_give") or []]
        get_ids = [str(i) for i in offer.get("you_get") or []]
        if partner is None or not give_ids or not get_ids:
            counters = {"error": "offer needs partner_roster_id, you_give and you_get (player ids)"}
        elif int(partner) not in profiles:
            counters = {"error": f"roster {partner} is not in this league"}
        else:
            counters = counter_offers(ctx, profiles, me, int(partner), give_ids, get_ids)

    freshness = db.get_data_freshness() if hasattr(db, "get_data_freshness") else {}
    my_profile = profiles[me]
    elapsed = time.monotonic() - started
    teams = sorted(profiles.values(), key=lambda p: -(p["playoff_pct"] or 0.0))
    message = (
        f"{my_profile['name']} ({my_profile['record']}, "
        + (f"{my_profile['playoff_pct']:.0f}% playoff odds, " if my_profile["playoff_pct"] is not None else "")
        + f"{my_profile['situation']}): needs "
        + (", ".join(f"{k} {v}/10" for k, v in sorted(my_profile["needs"].items(),
                                                       key=lambda kv: -kv[1]) if v >= MARKET_MIN_LEVEL)
           or "nothing pressing")
        + "; surplus "
        + (", ".join(f"{k} {v}/10" for k, v in my_profile["surpluses"].items() if v >= MARKET_MIN_LEVEL)
           or "none")
        + f". {len(partners)} partner(s) listed"
        + (f"; best: {partners[0]['partner']}" if partners else "") + "."
    )
    return sleeper_tools.mark_roster_staleness(create_success_response({
        "league": {"league_id": league_id, "name": ctx["league"].get("name"),
                   "num_teams": ctx["num_teams"]},
        "season": ctx["season"], "week": ctx["week"], "roster_id": me,
        "you": my_profile,
        "teams": teams,
        "partners": partners,
        "counter_offers": counters,
        "risk_mode": (finder or {}).get("risk_mode"),
        "risk_reason": (finder or {}).get("risk_reason"),
        "trade_deadline": (finder or {}).get("trade_deadline"),
        "weeks_scored": ctx["weeks"],
        "minimum_gain": round(ctx["bar"], 1),
        "values_source": ctx["values"].get("source"),
        "values_stale": ctx["values"].get("stale", False),
        "data_freshness": freshness,
        "stale_data_warnings": _staleness_warnings(freshness) if freshness else [],
        "timing": {**ctx["timing"],
                   "profile_seconds": round(profiled_at - started - ctx["timing"]["projection_seconds"], 2),
                   "search_seconds": round(searched_at - profiled_at, 2),
                   "elapsed_seconds": round(elapsed, 2)},
        "method": (
            "needs: share of a solid starter's (the league's num_teams-th best at the "
            "position) rest-of-season points the roster's weekly best lineup would gain, "
            "0-10; surpluses: bench players (start < half the remaining weeks) as a share "
            "of that starter, 0-10. Packages come from find_trade_targets (both lineups "
            "must gain over the rest of the season); acceptance_likelihood is a logistic "
            "heuristic over the partner's lineup gain, FantasyCalc balance, need, depth "
            "given up, situation and value timing (see acceptance_factors)."
        ),
        "caveats": [
            "acceptance_likelihood is a heuristic, not a fitted model — read the factors.",
            "Market values are FantasyCalc redraft values in this league's format; "
            "managers who price differently will answer differently.",
            "Playoff odds come from a cached Monte Carlo (get_playoff_odds, 2000 sims).",
        ],
        "message": message,
    }), ctx["roster_state"])


__all__ = ["acceptance_likelihood", "build_context", "counter_offers", "counters_for_trade",
           "evaluate_trade", "get_trade_market", "partner_fit", "roster_profile"]
