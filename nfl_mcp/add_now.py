"""Free-agent upgrades to make right now, for the weekly briefing.

`get_waiver_targets` already knows which free agents beat a starter this
week and whether the add is instant; the briefing did not say so. On game
day that cost real points: a free-agent defense worth +2.0 sat on the wire
while the user's own defense played the early (Madrid) game -- and once that
game kicks off the defense he would swap out is locked: it can be neither
benched nor dropped, so the move is gone.

This reuses the waiver computation as is (no second ranking) and keeps only
the moves the briefing should push:

- a real upgrade (``verdict == "upgrade"``) and, outside FAAB, the waiver
  strategy says ``add_now``;
- an instant add (free agent, no waiver period) in time for his kickoff;
- a drop the waiver tool is willing to make (or an open roster spot).

Each carries a ``deadline``: the earliest of the add's kickoff, the kickoff
of the starter he replaces in the lineup and the kickoff of the suggested
drop -- a locked player can be neither benched nor dropped.

Additive and bounded: the read runs under a time budget and never raises;
on failure the block is empty with an ``error``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from .game_clock import LOCAL_TZ, local_time, parse_kickoff

logger = logging.getLogger(__name__)

# The briefing `start`s the read first thing and `collect`s it last, so the
# read gets the briefing's own duration for free; past that the briefing
# waits this much longer. A fixed 20 s from the start timed out under
# game-day load (2026-11-09: two briefings in parallel, each with a waiver
# read that takes ~12 s on an idle server).
ADD_NOW_GRACE_SECONDS = 15.0
# A read nobody collects (the briefing failed first) stops by then.
ADD_NOW_CAP_SECONDS = 90.0
# For a caller with no work of its own to overlap (`build_block`), as
# `late_risk` does for its free agents.
ADD_NOW_BUDGET_SECONDS = 20.0


def _now() -> datetime:
    """The clock the deadlines read; a seam for tests."""
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _fmt(moment: datetime) -> str:
    """``Sun 15:30`` in the user's zone."""
    return moment.astimezone(LOCAL_TZ).strftime("%a %H:%M")


async def read_targets(league_id: str, roster_id: int, season: int, week: int) -> dict:
    """`get_waiver_targets` for one roster (a seam for tests)."""
    from .waiver_target_tools import get_waiver_targets
    return await get_waiver_targets(league_id, roster_id=roster_id, week=week, season=season)


def _deadline(target: dict) -> tuple[datetime, str] | None:
    """The earliest lock among the add, the starter he replaces and the drop."""
    name = target.get("name")
    replaces = target.get("replaces") or {}
    drop = target.get("drop") or {}
    locks: list[tuple[datetime | None, str]] = [
        (parse_kickoff(target.get("kickoff")), f"{name} kicks off — the add must land before")]
    same = bool(replaces) and replaces.get("player_id") == drop.get("player_id")
    if replaces:
        locks.append((parse_kickoff(replaces.get("kickoff")),
                      f"{replaces.get('name')} (the starter he replaces"
                      + (" and the drop" if same else "") + ") kicks off — after that he can "
                      + ("be neither benched nor dropped" if same else "no longer be benched")))
    if drop and not same:
        locks.append((parse_kickoff(drop.get("kickoff")),
                      f"{drop.get('name')} (the drop) kicks off — a locked player cannot be "
                      "dropped"))
    known = [(at, why) for at, why in locks if at is not None]
    return min(known, key=lambda x: x[0]) if known else None


def from_targets(resp: dict, now: datetime | None = None) -> list[dict]:
    """The briefing's ``add_now`` moves from a `get_waiver_targets` response."""
    now = now or _now()
    open_spots = int(resp.get("open_roster_spots") or 0)
    moves = []
    for t in resp.get("targets") or []:
        if t.get("verdict") != "upgrade" or t.get("locked"):
            continue
        timing = t.get("waiver_timing") or {}
        if not timing.get("instant_add") or timing.get("in_time_for_kickoff") is not True:
            continue
        strategy = t.get("waiver_strategy")
        if strategy is not None and strategy.get("recommendation") != "add_now":
            continue
        drop = t.get("drop")
        if drop is None and open_spots <= 0:
            continue  # nobody on the bench is worth less: not a move to push
        found = _deadline(t)
        if found is None or found[0] <= now:
            continue
        deadline, why = found
        replaces = t.get("replaces") or {}
        gain = t.get("upgrade_points")
        swap = (f" for {replaces.get('name')}" if replaces else " into an empty slot")
        moves.append({
            "player": t.get("name"),
            "player_id": t.get("player_id"),
            "position": t.get("position"),
            "team": t.get("team"),
            "opponent": t.get("opponent"),
            "projected_points": t.get("projected_points"),
            "gain": gain,
            "replaces": replaces.get("name"),
            "replaces_projected_points": replaces.get("projected_points"),
            "drop": (drop or {}).get("name"),
            "drop_note": t.get("drop_note"),
            "kickoff_local": t.get("kickoff_local"),
            "deadline": _iso(deadline),
            "deadline_local": local_time(deadline),
            "deadline_reason": why,
            "summary": (f"Add {t.get('name')} ({t.get('position')}, +{gain} pts){swap}"
                        + (f", dropping {drop.get('name')}" if drop else "")
                        + f" before {_fmt(deadline)}: {why}."),
        })
    moves.sort(key=lambda m: (-(m["gain"] or 0.0), m["deadline"]))
    return moves


def start(league_id: str, roster_id: int, season: int, week: int, *,
          cap: float = ADD_NOW_CAP_SECONDS) -> asyncio.Task:
    """Begin the waiver read alongside the caller's own work; `collect` turns
    it into the block. Stops after ``cap`` seconds whether or not it is
    collected."""
    return asyncio.create_task(
        asyncio.wait_for(read_targets(league_id, roster_id, season, week), cap))


async def collect(task: asyncio.Task, *, now: datetime | None = None,
                  grace: float = ADD_NOW_GRACE_SECONDS) -> dict:
    """``{moves, free_agents_checked[, error]}`` from a `start`ed read,
    waiting at most ``grace`` seconds more for it. Never raises."""
    try:
        resp = await asyncio.wait_for(asyncio.shield(task), grace)
    except TimeoutError:
        if not task.done():
            task.cancel()
        return {"moves": [], "error": f"waiver targets not read within {grace:.0f}s of the briefing"}
    except Exception as e:  # additive: never sink the briefing
        logger.warning(f"[add_now] waiver targets unavailable: {e}")
        return {"moves": [], "error": str(e) or type(e).__name__}
    try:
        resp = resp or {}
        if resp.get("success") is False:
            return {"moves": [], "error": resp.get("error") or "waiver targets unavailable"}
        return {"moves": from_targets(resp, now), "free_agents_checked": resp.get("pool_size")}
    except Exception as e:
        logger.warning(f"[add_now] could not read the waiver targets: {e}")
        return {"moves": [], "error": str(e) or type(e).__name__}


async def build_block(league_id: str, roster_id: int, season: int, week: int, *,
                      now: datetime | None = None,
                      budget: float = ADD_NOW_BUDGET_SECONDS) -> dict:
    """`start` and `collect` in one, within ``budget`` seconds. Never raises."""
    return await collect(start(league_id, roster_id, season, week, cap=budget), now=now,
                         grace=budget)
