"""Bye-week plan for one roster: which weeks the lineup runs short, and what to do.

A bye is known months ahead, which is exactly why it is easy to forget: the
roster looks fine every week until the one where three starters sit at once.
This walks the next few weeks with the league's own slots and the rest-of-season
projection (`ros.ros_for_ids`: 0 on byes and inside expected injury absences),
fills the best legal lineup for each week, and names the weeks where a slot
cannot be filled or the lineup drops noticeably — with the position to add and,
optionally, free agents who would fill it that week (`get_waiver_targets` for
that week, bounded by a timeout).

Replaces the four canned strategy stubs (matchup preview, bye coordination,
trade-deadline analysis, playoff plan) that returned hard-coded team samples.
"""
from __future__ import annotations

import asyncio
import logging

from .database import NFLDatabase
from .errors import create_success_response
from .lineup_slots import SLOT_ELIGIBILITY, normalize_slot, optimal_lineup, starting_slot_list

logger = logging.getLogger(__name__)

# A week counts as a crunch when a slot cannot be filled, or when byes cost at
# least this many points (or this share) against the same roster at full strength.
CRUNCH_POINTS = 6.0
CRUNCH_SHARE = 0.08
# Free-agent lookups project the whole pool; bound them so the plan stays fast.
FREE_AGENT_TIMEOUT_SECONDS = 25.0
FREE_AGENT_WEEKS = 2


def _rate(entry: dict) -> float:
    """Per-game value used to tell starters from bench (bye-independent)."""
    rate = entry.get("per_game")
    if rate:
        return float(rate)
    pts = [v for v in (entry.get("weekly_points") or {}).values() if v]
    return max(pts) if pts else 0.0


def _eligible(slot: str) -> list[str]:
    return sorted(SLOT_ELIGIBILITY.get(normalize_slot(slot), {slot}))


def _week_row(players: list[dict], slot_list: list[str], week: int, core_ids: set[str]) -> dict:
    """Best lineup for one week, what the byes cost, and which slots stay empty."""
    def pts(p: dict) -> float:
        return float((p.get("weekly_points") or {}).get(week, 0.0) or 0.0)

    playing = [p for p in players if pts(p) > 0]
    lineup = optimal_lineup(playing, slot_list, value=pts)
    total = round(sum(pts(p) for p in lineup if p), 1)

    # The same roster as if nobody were on bye: bye players at their usual rate.
    def full_value(p: dict) -> float:
        return pts(p) if week not in (p.get("bye_weeks") or []) else _rate(p)

    full_pool = [p for p in players if full_value(p) > 0]
    full = optimal_lineup(full_pool, slot_list, value=full_value)
    full_total = round(sum(full_value(p) for p in full if p), 1)

    holes = [{"slot": s, "eligible": _eligible(s)} for s, p in zip(slot_list, lineup, strict=True) if p is None]

    def _who(p: dict) -> dict:
        return {"player": p["player"], "position": p["position"], "team": p["team"],
                "role": "starter" if p.get("player_id") in core_ids else "bench"}

    on_bye = [_who(p) for p in players if week in (p.get("bye_weeks") or [])]
    injured = [{**_who(p), "reason": p.get("injury_window")}
               for p in players if week in (p.get("injury_weeks") or [])]
    available: dict[str, int] = {}
    for p in playing:
        available[p["position"]] = available.get(p["position"], 0) + 1

    bye_cost = round(max(0.0, full_total - total), 1)
    crunch = bool(holes) or bye_cost >= max(CRUNCH_POINTS, CRUNCH_SHARE * full_total)
    thin = not crunch and any(r["role"] == "starter" for r in on_bye)
    return {
        "week": week,
        "status": "crunch" if crunch else "thin" if thin else "ok",
        "projected_total": total,
        "full_strength_total": full_total,
        "bye_cost": bye_cost,
        "on_bye": on_bye,
        "starters_on_bye": sum(1 for r in on_bye if r["role"] == "starter"),
        "injured_out": injured,
        "available_by_position": dict(sorted(available.items())),
        "holes": holes,
        "lineup": [
            {"slot": s, "player": p["player"] if p else None,
             "position": p["position"] if p else None,
             "projected_points": round(pts(p), 1) if p else 0.0}
            for s, p in zip(slot_list, lineup, strict=True)
        ],
    }


def _positions_to_add(row: dict, slot_list: list[str]) -> list[str]:
    """Positions whose addition would fill that week's holes or bye losses."""
    wanted: list[str] = []
    for hole in row["holes"]:
        eligible = hole["eligible"]
        # A flex hole: add the eligible position with the fewest bodies that week.
        pos = min(eligible, key=lambda x: row["available_by_position"].get(x, 0))
        wanted.append(pos)
    if not wanted:
        for r in row["on_bye"]:
            if r["role"] == "starter":
                wanted.append(r["position"])
    return list(dict.fromkeys(wanted))


def _suggestion(row: dict, slot_list: list[str], positions: list[str]) -> str:
    w = row["week"]
    names = ", ".join(r["player"] for r in row["on_bye"] if r["role"] == "starter") or "no starters"
    parts = []
    for pos in positions:
        need = sum(1 for s in slot_list if normalize_slot(s) == pos) or 1
        have = row["available_by_position"].get(pos, 0)
        parts.append(f"only {have} {pos} available for {need} {pos} slot(s)" if have < need
                     else f"{pos} depth is thin")
    detail = "; ".join(parts) if parts else "the lineup drops"
    empty = f", {len(row['holes'])} slot(s) empty" if row["holes"] else ""
    add = " / ".join(positions) or "depth"
    return (f"Week {w}: {detail}{empty} — {names} on bye, lineup {row['projected_total']} vs "
            f"{row['full_strength_total']} at full strength (-{row['bye_cost']}). "
            f"Add a {add} before week {w} (waivers after the week-{w - 1} games).")


async def _free_agents(league_id: str, roster_id: int, season: int, week: int,
                       positions: list[str]) -> list[dict]:
    from .waiver_target_tools import get_waiver_targets
    res = await get_waiver_targets(league_id=league_id, roster_id=roster_id, week=week,
                                   season=season, positions=positions or None, limit=3)
    keep = ("name", "position", "team", "opponent", "projected_points", "upgrade_points", "verdict")
    return [{k: t.get(k) for k in keep} | {"recommendation": (t.get("waiver_strategy") or {}).get("recommendation")}
            for t in (res or {}).get("targets") or []]


async def get_bye_week_plan(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    weeks_ahead: int = 6,
    week: int | None = None,
    season: int | None = None,
    include_free_agents: bool = True,
    db=None,
) -> dict:
    """Week-by-week bye/depth plan for one roster (see module docstring)."""
    from . import ros, sleeper_tools
    from .briefing_tools import find_roster
    from .week_context import current_season_week

    db = db if db is not None else NFLDatabase()
    weeks_ahead = max(1, min(int(weeks_ahead or 6), 18))
    if week is None or season is None:
        current = await current_season_week(db)
        week = week or current["week"]
        season = season or current["season"]

    league_resp, rosters_resp = await asyncio.gather(
        sleeper_tools.get_league(league_id), sleeper_tools.get_rosters(league_id))
    league = (league_resp or {}).get("league") or {}
    if not league:
        return create_success_response({"success": False, "error": f"Could not load league {league_id}."})
    rosters = (rosters_resp or {}).get("rosters") or []
    mine, error = find_roster(rosters, league_id, roster_id, user_id)
    if error:
        return create_success_response({"success": False, "error": error})
    roster_id = mine["roster_id"]

    taxi = {str(p) for p in (mine.get("taxi") or [])}
    reserve = {str(p) for p in (mine.get("reserve") or [])}
    ids = [str(p) for p in (mine.get("players") or []) if str(p) not in taxi]
    by_id, meta = await ros.ros_for_ids(ids, league=league, season=season, week=week, db=db)
    players = list(by_id.values())
    slot_list = starting_slot_list(league.get("roster_positions"))
    last_week = meta["windows"]["last_week"]
    weeks = [w for w in range(week, week + weeks_ahead) if w <= last_week]

    core = optimal_lineup(players, slot_list, value=_rate)
    core_ids = {p["player_id"] for p in core if p}

    rows = [_week_row(players, slot_list, w, core_ids) for w in weeks]
    suggestions: list[str] = []
    needs: dict[int, list[str]] = {}
    for row in rows:
        if row["status"] == "crunch":
            positions = _positions_to_add(row, slot_list)
            needs[row["week"]] = positions
            suggestions.append(_suggestion(row, slot_list, positions))
            row["positions_to_add"] = positions
    if not suggestions:
        suggestions.append(f"No bye crunch in weeks {weeks[0]}-{weeks[-1]}: every slot can be filled."
                           if weeks else "No regular-season weeks left to plan.")

    free_agents: dict[int, list[dict]] = {}
    free_agent_note = None
    if include_free_agents and needs:
        worst = sorted(needs, key=lambda w: -next(r["bye_cost"] + 50 * len(r["holes"])
                                                 for r in rows if r["week"] == w))[:FREE_AGENT_WEEKS]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(_free_agents(league_id, roster_id, season, w, needs[w]) for w in worst),
                               return_exceptions=True),
                timeout=FREE_AGENT_TIMEOUT_SECONDS)
            for w, res in zip(worst, results, strict=True):
                if isinstance(res, Exception):
                    logger.debug(f"free agents for week {w} failed: {res}")
                    continue
                free_agents[w] = res
        except TimeoutError:
            free_agent_note = (f"Free-agent lookup timed out after {FREE_AGENT_TIMEOUT_SECONDS:.0f}s — "
                               "call get_waiver_targets(week=<crunch week>) directly.")

    ir_names = [by_id[p]["player"] for p in reserve if p in by_id]
    return create_success_response({
        "league": {"league_id": league_id, "name": league.get("name")},
        "roster_id": roster_id,
        "season": season,
        "week": week,
        "slots": slot_list,
        "weeks": rows,
        "crunch_weeks": [r["week"] for r in rows if r["status"] == "crunch"],
        "thin_weeks": [r["week"] for r in rows if r["status"] == "thin"],
        "suggestions": suggestions,
        "free_agent_options": free_agents,
        "free_agent_note": free_agent_note,
        "core_starters": [p["player"] for p in core if p],
        "reserve_counted": ir_names,
        "trade_deadline": ros.trade_deadline_status(league.get("settings") or {}, week),
        "schedule_unknown_weeks": [w for w in meta.get("schedule_unknown_weeks") or [] if w in weeks],
        "method": (
            "Each week: best legal lineup from the league's slots on that week's "
            "rest-of-season projection (0 on byes and inside expected injury "
            "absences). full_strength_total re-fills the same lineup with the "
            "bye players at their per-game rate; bye_cost is the gap. crunch = an "
            f"empty slot, or bye_cost >= max({CRUNCH_POINTS:.0f}, {CRUNCH_SHARE:.0%} of full strength)."
        ),
    })


__all__ = ["get_bye_week_plan"]
