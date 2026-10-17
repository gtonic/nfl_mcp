"""Late-game risk: when a doubtful starter has to be decided, and on what.

A questionable starter is decided at his team's inactives, ~90 minutes before
his kickoff -- but only if a replacement is still movable then. Every
replacement locks at *its* kickoff. Mike Evans (SF, Monday night, Tue 02:15
Vienna time) questionable with every bench and free-agent option playing on
Sunday is the case this is for: by Monday's inactives the alternatives have
all locked, so the call has to be made before Sunday's earliest relevant
kickoff, on the injury report alone.

For each starter whose availability is in doubt (a Q/D/Out tag, a DNP latest
practice day, a news flag such as "unlikely to play" or "game-time
decision", gameday list not published yet) this works out:

- his kickoff, when his inactives are expected (kickoff - 90 min), his
  expected points (the calibrated projection: the questionable discount is
  already in it; when an availability news flag moves P(plays), P(plays) x
  his if-active points instead), what he scores *if active* (`projections.confirmed_active_mult`)
  and a P(plays) proxy (`PLAY_PROBABILITY`, the same 2023-25 practice backtest);
- the replacements that can take his slot: bench players and instant-add free
  agents, directly or by moving a flex starter into his slot, each with its
  kickoff and projection, and the drop an add would need on a full roster;
- three options and their expected points: **keep** him; **swap** before the
  replacement locks; **wait** until his inactives with a fallback that is still
  unlocked then (same-day games, a later kickoff) -- worth his expected points
  plus the fallback's in the share of weeks he sits.

The recommendation is the best of the three; ``decision_deadline`` is the
earliest lock among the best replacements still unlocked (with "wait", the
fallback's kickoff). Times are UTC plus Europe/Vienna (``*_local``).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .game_clock import LOCAL_TZ, game_lock, local_time, parse_kickoff, week_games
from .lineup_slots import normalize_slot, slot_accepts
from .teams import normalize_team

logger = logging.getLogger(__name__)

# Teams declare inactives 90 minutes before kickoff; look a few minutes later.
INACTIVES_LEAD = timedelta(minutes=90)
CHECK_AFTER = timedelta(minutes=5)
# Time to make a move before a lock (open the app, add, drop, set the slot).
ACTION_MARGIN = timedelta(minutes=5)
# A swap has to beat keeping him by this much to be recommended outright:
# below it the two are inside the projection's noise.
SWAP_MARGIN = 1.0
# Replacements within this many points of the best one count as "the best"
# for the deadline: losing the top option by a few tenths is no deadline.
BEST_BAND = 1.0
MAX_REPLACEMENTS = 5
# Points a free-agent add that costs a drop has to clear over a bench option.
ADD_COST = 0.5

# P(he plays) by designation and practice week, from the 2023-25 practice
# backtest (`evals/backtest/practice_backtest.py`, see the table above
# `projections.QUESTIONABLE_REALISED`): Q + FP 86%, Q + LP 66%, Q + DNP 56%
# played; a single DNP so far is the mix of how such weeks end (2026 weeks
# 3-4: 14% DNP / 51% LP / 35% FP -> ~0.72); a Q with no practice line is the
# all-Q average (~0.68). Doubtful: 1 of 60 played.
PLAY_PROBABILITY = {
    "Q_FP": 0.86, "Q_REST": 0.86, "Q_LP": 0.66, "Q_DNP": 0.56, "Q_DNP_SINGLE": 0.72,
    "Q_NONE": 0.68, "doubtful": 0.02, "out": 0.0, "uncertain": 0.9,
    # No designation, but a DNP latest practice day (designations come with
    # the final report; a DNP on Wednesday is often rest).
    "healthy_dnp": 0.9,
}
# News caps / floors on the proxy (`news_signals.AVAILABILITY_FLAGS`).
NEWS_CAP = {"ruled_out": 0.0, "unlikely_to_play": 0.25, "game_time_decision": 0.6}
NEWS_FLOOR = {"expected_to_play": 0.85}
_RISK_NEWS = {"ruled_out", "unlikely_to_play", "game_time_decision"}


def _now() -> datetime:
    return datetime.now(UTC)


def _flags(player: dict) -> set[str]:
    out = set()
    for f in player.get("news_flags") or []:
        name = f.get("flag") if isinstance(f, dict) else f
        if isinstance(name, str):
            out.add(name)
    return out


def _practice(player: dict) -> tuple[str | None, list[str]]:
    from .projections import _latest_practice, _practice_days
    days = _practice_days(player.get("practice_pattern"))
    latest = _latest_practice(player.get("practice_status"), days) or None
    return latest, days


def play_probability(player: dict) -> tuple[float, str]:
    """``(P(plays), basis)`` -- a proxy from the designation, the practice week,
    news flags and, once published, the gameday list (which settles it)."""
    p, basis, _ = _play_probability(player)
    return p, basis


def _play_probability(player: dict) -> tuple[float, str, bool]:
    """`play_probability` plus whether a news flag moved it (the projection
    does not price availability news: `news_signals` gives those flags
    ``model_mult`` 1.0)."""
    from .projections import availability
    gameday = (player.get("gameday_status") or "").lower()
    if gameday == "inactive":
        return 0.0, "gameday: inactive (published)", False
    if gameday == "active":
        return 1.0, "gameday: active (published)", False
    kind = availability(player.get("injury_status"))
    latest, days = _practice(player)
    if kind == "questionable":
        key = ("Q_DNP" if len(days) >= 2 else "Q_DNP_SINGLE") if latest == "DNP" else \
              f"Q_{latest}" if latest in ("FP", "REST", "LP") else "Q_NONE"
        p, basis = PLAY_PROBABILITY[key], f"questionable, practice {player.get('practice_pattern') or latest or 'none'}"
    elif kind in ("doubtful", "out", "uncertain"):
        p, basis = PLAY_PROBABILITY[kind], kind
    elif kind == "healthy" and latest == "DNP":
        p, basis = PLAY_PROBABILITY["healthy_dnp"], "no designation, DNP latest practice"
    elif kind == "healthy":
        p, basis = 1.0, "healthy"
    else:  # unrecognised designation: treated as questionable elsewhere too
        p, basis = PLAY_PROBABILITY["Q_NONE"], f"status {player.get('injury_status')!r}"
    flags = _flags(player)
    moved = False
    for flag, cap in NEWS_CAP.items():
        if flag in flags and p > cap:
            p, basis, moved = cap, f"{basis}; news: {flag.replace('_', ' ')}", True
    for flag, floor in NEWS_FLOOR.items():
        if flag in flags and p < floor and kind not in ("out", "doubtful"):
            p, basis, moved = floor, f"{basis}; news: {flag.replace('_', ' ')}", True
    return round(p, 2), basis, moved


def risk_reasons(player: dict) -> list[str]:
    """Why his availability is in doubt; empty when it is not."""
    from .projections import availability
    gameday = (player.get("gameday_status") or "").lower()
    if gameday == "active":
        return []
    reasons = []
    if gameday == "inactive":
        reasons.append("officially inactive")
    kind = availability(player.get("injury_status"))
    if kind not in ("healthy",):
        reasons.append(f"designation {player.get('injury_status')}")
    latest, _ = _practice(player)
    if latest in ("DNP", "LP"):
        reasons.append(f"practice {player.get('practice_pattern') or latest}")
    for flag in sorted(_flags(player) & _RISK_NEWS):
        reasons.append(f"news: {flag.replace('_', ' ')}")
    # A limited week with no designation is not a risk on its own.
    if reasons and kind == "healthy" and latest != "DNP" and not (_flags(player) & _RISK_NEWS) \
            and gameday != "inactive":
        return []
    return reasons


def if_active_points(player: dict) -> float | None:
    """His projection if he suits up: the pre-inactives discount swapped for
    the played-only share (`projections.confirmed_active_mult`)."""
    from .projections import confirmed_active_mult, practice_adjusted_mult
    proj = float(player.get("projected_points") or 0.0)
    status = player.get("injury_status")
    pre = practice_adjusted_mult(status, player.get("practice_status"), player.get("practice_pattern"))
    post = confirmed_active_mult(status, player.get("practice_status"), player.get("practice_pattern"))
    if pre <= 0.0:
        return None
    return round(max(proj, proj * post / pre), 1)


def _kick(player: dict) -> datetime | None:
    return parse_kickoff(player.get("kickoff"))


def _fmt(moment: datetime | None) -> str | None:
    """``Sun 18.10. 19:00`` in the user's zone."""
    return moment.astimezone(LOCAL_TZ).strftime("%a %d.%m. %H:%M") if moment else None


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z") if moment else None


def _usable(player: dict, at: datetime) -> bool:
    """Still movable at ``at`` (with time to make the move). Unknown kickoff:
    movable -- the recoverable error, as in `game_clock.game_lock`."""
    if player.get("locked"):
        return False
    kick = _kick(player)
    return kick is None or kick - ACTION_MARGIN > at


def _locks_at(*players: dict) -> datetime | None:
    times = [k for p in players if (k := _kick(p)) is not None]
    return min(times) if times else None


def _candidates(starter: dict, starters: list[dict], pool: list[dict]) -> list[dict]:
    """Every way to fill his slot: a pool player straight in, or a flex
    starter moved into his slot and the pool player into the flex."""
    slot = starter.get("slot")
    out = []
    for x in pool:
        if x.get("player_id") and x.get("player_id") == starter.get("player_id"):
            continue
        if slot_accepts(slot, x.get("position")):
            out.append({"in": x, "via": None, "moves": [
                {"player": x["name"], "from_slot": x["source"], "to_slot": slot},
                {"player": starter["name"], "from_slot": slot, "to_slot": "BENCH"}]})
            continue
        for f in starters:
            if f is starter or f.get("locked") or normalize_slot(f.get("slot")) == normalize_slot(slot):
                continue
            if slot_accepts(slot, f.get("position")) and slot_accepts(f.get("slot"), x.get("position")):
                out.append({"in": x, "via": f, "moves": [
                    {"player": f["name"], "from_slot": f["slot"], "to_slot": slot},
                    {"player": x["name"], "from_slot": x["source"], "to_slot": f["slot"]},
                    {"player": starter["name"], "from_slot": slot, "to_slot": "BENCH"}]})
                break
    return out


def _pick_drop(bench: list[dict], keep_ids: set, at: datetime) -> dict | None:
    """The bench player an add would cost on a full roster: lowest projected
    this week, still unlocked at ``at``; Out players are a separate call."""
    from .projections import availability
    options = [b for b in bench if b.get("player_id") not in keep_ids and _usable(b, at)
               and availability(b.get("injury_status")) != "out"]
    # Market value first (a 0-point week for a hurt WR1 is no reason to cut
    # him), this week's projection without one.
    return min(options, key=lambda b: (
        b["value"] if isinstance(b.get("value"), (int, float)) else float("inf"),
        float(b.get("projected_points") or 0.0))) if options else None


def _replacement_row(c: dict, his_check: datetime | None, drop: dict | None,
                     needs_drop: bool) -> dict:
    x, via = c["in"], c["via"]
    locks = _locks_at(x, via) if via else _kick(x)
    row = {
        "player": x["name"], "player_id": x.get("player_id"), "position": x.get("position"),
        "team": x.get("team"), "source": x["source"],
        "projected_points": round(float(x.get("projected_points") or 0.0), 1),
        "injury_status": x.get("injury_status"),
        "kickoff": _iso(_kick(x)), "kickoff_local": local_time(_kick(x)),
        "locks_at": _iso(locks), "locks_at_local": local_time(locks),
        # Still movable when his inactives are out: a fallback, not a guess.
        "unlocked_at_his_inactives": bool(his_check is None or locks is None
                                          or locks - ACTION_MARGIN > his_check),
    }
    if via is not None:
        # Not a straight swap: the flex starter moves into his slot.
        row["via"] = via["name"]
        row["moves"] = c["moves"]
    if x["source"] == "free_agent":
        row["add"] = "instant (free agent)"
        if needs_drop:
            row["drop"] = drop["name"] if drop else None
    return row


def plan_starter(starter: dict, starters: list[dict], bench: list[dict],
                 free_agents: list[dict], open_spots: int | None,
                 now: datetime) -> dict | None:
    """The contingency for one starter, or None when he is not at risk (or
    his game has started)."""
    reasons = risk_reasons(starter)
    if not reasons or starter.get("locked"):
        return None
    kick = _kick(starter)
    inactives = kick - INACTIVES_LEAD if kick else None
    check = inactives + CHECK_AFTER if inactives else None
    if check is not None and check < now:
        check = now  # the list should be out: look now
    p, basis, news_moved = _play_probability(starter)
    expected = round(float(starter.get("projected_points") or 0.0), 1)
    active_pts = if_active_points(starter)
    expected_basis = "projection (designation and practice discount)"
    if news_moved and active_pts is not None:
        # The projection prices the designation and practice week only; when
        # news moved P(plays), keep = P(plays) x his if-active points, the
        # same P the "wait" option uses.
        expected = round(p * active_pts, 1)
        expected_basis = "P(plays) x if-active points (news moved P(plays))"

    pool = ([{**b, "source": "bench"} for b in bench if float(b.get("projected_points") or 0) > 0]
            + [{**f, "source": "free_agent"} for f in free_agents
               if float(f.get("projected_points") or 0) > 0])
    needs_drop = open_spots is not None and open_spots <= 0
    rows, viable = [], []
    for c in _candidates(starter, starters, pool):
        x, via = c["in"], c["via"]
        if not _usable(x, now) or (via is not None and not _usable(via, now)):
            continue
        drop = None
        if x["source"] == "free_agent" and needs_drop:
            # Dropped when the add is made: now for a swap; a fallback's add
            # may wait for his inactives, so prefer a drop still movable then.
            later = check if check is not None and check > now else now
            drop = (_pick_drop(bench, {starter.get("player_id")}, later)
                    or _pick_drop(bench, {starter.get("player_id")}, now))
            if drop is None:
                continue
        rows.append((float(x.get("projected_points") or 0.0), _replacement_row(c, check, drop, needs_drop)))
    # An add that costs a drop has to be worth it: within ADD_COST points a
    # bench player (or an open spot) comes first.
    rows.sort(key=lambda r: -(r[0] - (ADD_COST if r[1].get("drop") else 0.0)))
    seen: set = set()
    for value, row in rows:  # one row per player (the direct move wins)
        if row["player"] in seen:
            continue
        seen.add(row["player"])
        viable.append((value, row))

    keep_ev = expected
    swap = viable[0] if viable else None
    fallbacks = [(v, r) for v, r in viable if r["unlocked_at_his_inactives"]]
    wait = fallbacks[0] if fallbacks else None
    wait_ev = round(keep_ev + (1.0 - p) * wait[0], 1) if wait else None
    # The deadline: the earliest lock among the best still-unlocked options.
    top = max((v for v, _ in viable), default=0.0)
    best_band = [r for v, r in viable if v >= top - BEST_BAND]
    locks = [parse_kickoff(r["locks_at"]) for r in best_band if r["locks_at"]]
    band_deadline = min((t for t in locks if t is not None), default=None)

    plan: dict[str, Any] = {
        "player": starter["name"], "player_id": starter.get("player_id"),
        "slot": starter.get("slot"), "position": starter.get("position"), "team": starter.get("team"),
        "injury_status": starter.get("injury_status"),
        "practice_pattern": starter.get("practice_pattern"),
        "gameday_status": starter.get("gameday_status"),
        "risk_reasons": reasons,
        "kickoff": _iso(kick), "kickoff_local": local_time(kick),
        "inactives_expected": _iso(inactives), "inactives_expected_local": local_time(inactives),
        "expected_points": expected,
        "expected_points_basis": expected_basis,
        "if_active_points": active_pts,
        "play_probability": p, "play_probability_basis": basis,
        "replacements": [r for _, r in viable[:MAX_REPLACEMENTS]],
        **({"drop_note": "roster full: a free-agent add costs the named bench player (lowest "
                         "market value, unlocked, not Out)"}
           if needs_drop and any(r["source"] == "free_agent" for _, r in viable[:MAX_REPLACEMENTS])
           else {}),
        "options": {
            "keep": {"expected_points": keep_ev},
            "swap": ({"player": swap[1]["player"], "expected_points": round(swap[0], 1),
                      "act_by": swap[1]["locks_at"], "act_by_local": swap[1]["locks_at_local"]}
                     if swap else None),
            "wait": ({"fallback": wait[1]["player"], "expected_points": wait_ev,
                      "check_at": _iso(check), "check_at_local": local_time(check),
                      "fallback_locks_at_local": wait[1]["locks_at_local"]} if wait else None),
        },
    }
    name = starter["name"]
    poss = f"{name}'" if name.endswith("s") else f"{name}'s"
    if (starter.get("gameday_status") or "").lower() == "inactive":
        if swap:
            rec, deadline = "swap_now", parse_kickoff(swap[1]["locks_at"])
            why = f"{name} is officially inactive: start {swap[1]['player']} ({swap[0]:.1f})"
        else:
            rec, deadline = "no_alternative", None
            why = f"{name} is officially inactive and no eligible replacement is still unlocked"
    elif not swap:
        rec, deadline = "no_alternative", None
        why = f"no eligible bench player or instant-add free agent is still unlocked for {poss} slot"
    elif wait and wait_ev is not None and wait_ev >= max(keep_ev, swap[0]):
        rec, deadline = "wait", parse_kickoff(wait[1]["locks_at"])
        why = (f"wait for {poss} inactives (~{_fmt(inactives)}): if he is out, start "
               f"{wait[1]['player']} ({wait[0]:.1f}, locks {_fmt(deadline)}) -- "
               f"{wait_ev:.1f} expected vs keep {keep_ev:.1f} / swap {swap[0]:.1f}")
    elif swap[0] > keep_ev + SWAP_MARGIN:
        rec, deadline = "swap_now", parse_kickoff(swap[1]["locks_at"])
        why = (f"start {swap[1]['player']} ({swap[0]:.1f}) over {name} ({keep_ev:.1f} expected, "
               f"P(plays) {p:.0%}) before {_fmt(deadline)}"
               + (f" -- {poss} inactives (~{_fmt(inactives)}) come after every "
                  "replacement has locked" if not fallbacks else ""))
    else:
        rec, deadline = "keep", band_deadline
        close = swap[0] > keep_ev
        why = (f"keep {name} ({keep_ev:.1f} expected, {active_pts or keep_ev:.1f} if active, "
               f"P(plays) {p:.0%}) over {swap[1]['player']} ({swap[0]:.1f})"
               + (" -- a coin flip" if close else "")
               + (f"; the alternative is gone at {_fmt(band_deadline)}"
                  + (f", before his inactives (~{_fmt(inactives)})" if not fallbacks else "")
                  if band_deadline else ""))
        if close:
            plan["close_call"] = True
    if rec != "wait" and band_deadline and (deadline is None or band_deadline < deadline):
        deadline = band_deadline
    plan.update({
        "recommendation": rec,
        # With "wait": when to look (his list is out), before the fallback locks.
        **({"check_at": _iso(check), "check_at_local": local_time(check)} if rec == "wait" else {}),
        "decision_deadline": _iso(deadline), "decision_deadline_local": local_time(deadline),
        "summary": why,
    })
    return plan


def plan_contingencies(starters: list[dict], bench: list[dict],
                       free_agents: list[dict] | None = None, *,
                       open_spots: int | None = None, now: datetime | None = None,
                       free_agents_note: str | None = None) -> dict:
    """The late-game risk block: one plan per at-risk, unlocked starter.

    ``starters`` / ``bench`` / ``free_agents``: ``{name, player_id, position,
    team, slot (starters), projected_points, kickoff, locked, injury_status,
    practice_status, practice_pattern, news_flags, gameday_status}``.
    ``open_spots``: free roster spots (None: unknown, no drop named).
    """
    now = now or _now()
    plans = [p for s in starters
             if (p := plan_starter(s, starters, bench, free_agents or [], open_spots, now))]
    plans.sort(key=lambda p: p["decision_deadline"] or "9999")
    deadlines = [p["decision_deadline"] for p in plans if p["decision_deadline"]]
    first = parse_kickoff(min(deadlines)) if deadlines else None
    return {
        "as_of": _iso(now), "as_of_local": local_time(now), "timezone": str(LOCAL_TZ),
        "at_risk": plans,
        "earliest_deadline": _iso(first), "earliest_deadline_local": local_time(first),
        "free_agents_checked": free_agents is not None,
        **({"free_agents_note": free_agents_note} if free_agents_note else {}),
        "summary": ("; ".join(p["summary"] for p in plans) if plans
                    else "No starter with an availability question."),
        "method": ("inactives come ~90 min before each kickoff; a replacement locks at its own "
                   "kickoff. keep = his expected points; swap = the replacement's; wait = his "
                   "expected points plus a still-unlocked fallback's in the share of weeks he "
                   "sits (1 - P(plays))."),
    }


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _lock_fields(team: str | None, games: dict, now: datetime) -> dict:
    lock = game_lock(games.get(normalize_team(team) or ""), now)
    return {"kickoff": lock["kickoff"], "locked": lock["locked"]}


def from_projection(p: dict, inp: dict | None, games: dict, now: datetime,
                    slot: str | None = None) -> dict:
    """A `project_players` row (and its input) in the planner's shape."""
    inj = (inp or {}).get("injury") or {}
    row = {
        "name": p.get("player") or p.get("name"), "player_id": (inp or {}).get("player_id")
        or p.get("player_id"),
        "position": p.get("position"), "team": p.get("team"),
        "projected_points": p.get("projected_points") or p.get("mean") or 0.0,
        "injury_status": p.get("injury_status") or inj.get("status"),
        "practice_status": p.get("practice_status") or inj.get("practice_status"),
        "practice_pattern": p.get("practice_pattern") or inj.get("practice_pattern"),
        "news_flags": p.get("news_flags") or [],
        "gameday_status": p.get("gameday_status"),
        **_lock_fields(p.get("team"), games, now),
    }
    if slot is not None:
        row["slot"] = slot
    return row


def from_analysis(a: dict, slot: str | None = None) -> dict:
    """A `PlayerAnalysis.to_dict()` row in the planner's shape."""
    row = {
        "name": a.get("player_name"), "player_id": a.get("player_id") or None,
        "position": a.get("position"), "team": a.get("team"),
        "projected_points": a.get("projected_points") or 0.0,
        "injury_status": a.get("injury_status"),
        "practice_status": a.get("practice_status"),
        "practice_pattern": a.get("practice_pattern"),
        "news_flags": a.get("news_flags") or [],
        "gameday_status": a.get("gameday_status"),
        "kickoff": a.get("kickoff"), "locked": bool(a.get("locked")),
    }
    if slot is not None:
        row["slot"] = slot
    return row


def open_roster_spots(league: dict, mine: dict) -> int | None:
    """Free active-roster spots (reserve and taxi do not count)."""
    positions = (league or {}).get("roster_positions") or []
    size = sum(1 for s in positions if str(s).upper() not in ("IR", "TAXI"))
    if not size:
        return None
    unavailable = {str(p) for p in (mine.get("reserve") or [])} | {str(p) for p in (mine.get("taxi") or [])}
    active = sum(1 for p in (mine.get("players") or []) if p and str(p) not in unavailable)
    return max(0, size - active)


FREE_AGENT_LIMIT = 6
FREE_AGENT_BUDGET_SECONDS = 20.0


async def free_agent_options(league: dict, rosters: list[dict], db, season: int, week: int,
                             positions: list[str], now: datetime | None = None,
                             limit: int = FREE_AGENT_LIMIT) -> list[dict]:
    """The best instant-add free agents at ``positions`` this week, in the
    planner's shape (projected in the league's scoring; `waiver_rules`
    decides whether an add is instant)."""
    from . import sleeper_tools
    from .injury_match import build_injury_index
    from .player_pool import playing_options
    from .projections import project_players
    from .scoring import league_scoring
    from .waiver_rules import latest_drops, waiver_status
    from .waiver_target_tools import _is_claimable, _to_projection_input

    now = now or _now()
    wanted = sorted({p.upper() for p in positions if p})
    if not wanted or db is None:
        return []
    taken: set[str] = set()
    for roster in rosters or []:
        for key in ("players", "reserve", "taxi"):
            taken.update(str(p) for p in (roster.get(key) or []))
    opponents: dict[str, str] = {}
    for team, opp in (db.get_week_opponents(season, week) or {}).items():
        canon_team, canon_opp = normalize_team(team), normalize_team(opp)
        if canon_team and canon_opp:
            opponents[canon_team] = canon_opp
    rows = db.get_athletes_by_positions(wanted)
    pool_rows = playing_options([r for r in rows if r["id"] not in taken and _is_claimable(r)],
                                reference=rows)
    injury_index = build_injury_index(db.get_all_current_injuries())
    inputs = [p for r in pool_rows if (p := _to_projection_input(r, opponents, injury_index))]
    if not inputs:
        return []
    games = week_games(db, season, week)
    previous = week_games(db, season, week - 1) if week > 1 else {}
    proj, *tx = await asyncio.gather(
        project_players(inputs, scoring=league_scoring(league),
                        num_teams=int(league.get("total_rosters") or 12), season=season, week=week),
        *(sleeper_tools.get_transactions(str(league.get("league_id")), week=wk)
          for wk in sorted({week, max(1, week - 1)})),
        return_exceptions=True,
    )
    if isinstance(proj, BaseException):
        raise proj
    dropped: dict = {}
    for resp in tx:
        if isinstance(resp, BaseException):
            continue
        for pid, when in latest_drops((resp or {}).get("transactions")).items():
            if pid not in dropped or when > dropped[pid]:
                dropped[pid] = when
    by_key = {(i["name"], i["team"]): i for i in inputs}
    out: dict[str, list[dict]] = {}
    for p in (proj or {}).get("projections") or []:
        inp = by_key.get((p.get("player"), p.get("team")))
        row = from_projection(p, inp, games, now)
        timing = waiver_status(
            league, kickoff=parse_kickoff(row["kickoff"]),
            previous_kickoff=parse_kickoff((previous.get(row["team"]) or {}).get("kickoff")),
            dropped_at=dropped.get(str(row["player_id"])), now=now)
        if not timing.get("instant_add"):
            continue
        out.setdefault(row["position"], []).append(row)
    best = []
    for rows_ in out.values():
        rows_.sort(key=lambda r: -float(r["projected_points"] or 0.0))
        best.extend(rows_[:limit])
    return best


def needed_positions(starters: list[dict]) -> list[str]:
    """Positions that could take an at-risk starter's slot (flex slots:
    everything they accept), for the free-agent read."""
    from .lineup_slots import SLOT_ELIGIBILITY
    wanted: set[str] = set()
    for s in starters:
        if s.get("locked") or not risk_reasons(s):
            continue
        allowed = SLOT_ELIGIBILITY.get(normalize_slot(s.get("slot")))
        wanted |= set(allowed) if allowed else {str(s.get("position") or "").upper()}
    return sorted(p for p in wanted if p)


def attach_news(starters: list[dict], db) -> list[dict]:
    """Starters with their availability news flags filled in where the row
    came without them (`analyze_lineup` rows carry none; `project_players`
    rows do): the same index the projections read (`news_signals.index_for`
    over the injury blurbs and the stored news items)."""
    from . import news_signals
    rows = list(db.get_all_current_injuries() or []) if hasattr(db, "get_all_current_injuries") else []
    index = news_signals.index_for(db, rows)
    if not index:
        return starters
    out = []
    for s in starters:
        if not s.get("news_flags") and (s.get("gameday_status") or "").lower() != "active":
            flags = news_signals.signals_for(index, s.get("name"), s.get("team"))
            if flags:
                s = {**s, "news_flags": flags}
        out.append(s)
    return out


async def build_block(starters: list[dict], bench: list[dict], *, league: dict | None,
                      rosters: list[dict] | None, mine: dict | None, db, season: int | None,
                      week: int | None, now: datetime | None = None,
                      budget: float = FREE_AGENT_BUDGET_SECONDS) -> dict:
    """`plan_contingencies` with the free agents read (bounded) when a starter
    is at risk. Never raises."""
    now = now or _now()
    free_agents: list[dict] | None = None
    note = None
    if db is not None and any(not s.get("news_flags") for s in starters):
        try:
            starters = await asyncio.to_thread(attach_news, starters, db)
        except Exception as e:  # news is additive
            logger.debug(f"[late_risk] news flags unavailable: {e}")
    positions = needed_positions(starters)
    open_spots = open_roster_spots(league or {}, mine or {}) if league and mine else None
    if positions and league and rosters is not None and season and week:
        try:
            free_agents = await asyncio.wait_for(
                free_agent_options(league, rosters, db, int(season), int(week), positions, now),
                budget)
        except TimeoutError:
            note = "free agents not read in time: bench players only"
        except Exception as e:
            logger.warning(f"[late_risk] free agents unavailable: {e}")
            note = f"free agents unavailable ({e}): bench players only"
    if free_agents and open_spots is not None and open_spots <= 0:
        bench = await _with_values(bench, league or {}, db)
    try:
        return plan_contingencies(starters, bench, free_agents, open_spots=open_spots, now=now,
                                  free_agents_note=note)
    except Exception as e:  # additive: never sink the briefing
        logger.warning(f"[late_risk] planner failed: {e}")
        return {"at_risk": [], "error": str(e)}


async def _with_values(bench: list[dict], league: dict, db) -> list[dict]:
    """Bench rows with their market value (for the drop an add costs), as
    `get_waiver_targets` prices them. Unchanged when values are unavailable."""
    try:
        from .player_values import get_values_service
        from .trade_analyzer_tools import league_format_from_settings
        service = get_values_service(db)
        fmt = league_format_from_settings(league)
        values = await asyncio.wait_for(service.get_values(
            fmt["ppr"], fmt["num_qbs"], fmt["num_teams"], fmt["is_dynasty"]), 8.0)
        model = fmt.get("scoring_model")
        out = []
        for b in bench:
            hit = service.lookup(values, player_id=b.get("player_id"), name=b.get("name"),
                                 position=b.get("position"))
            mult = model.value_multiplier(b.get("position")) if model is not None else 1.0
            value = round(float(hit["value"]) * mult) if hit and hit.get("value") is not None else None
            out.append({**b, "value": value})
        return out
    except Exception as e:
        logger.debug(f"[late_risk] values unavailable for the drop: {e}")
        return bench
