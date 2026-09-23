"""Server-side lookups behind the start/sit and lineup MCP tools.

The start/sit tools used to ask the caller for team, position, target share,
snap share and practice status — data the server has. An assistant that does
not know them guesses, and a guessed snap share moves the decision. Here a
player can be named and the rest is looked up: team/position/player_id from the
athlete cache, last week's snap share from the usage table, injury and practice
by the optimizer itself. ``analyze_lineup`` reads the whole lineup off the
league, so no lineup dict has to be typed in at all.
"""
from __future__ import annotations

import logging

from .teams import normalize_team

logger = logging.getLogger(__name__)

_FANTASY = ("QB", "RB", "WR", "TE", "K", "DEF")


def resolve_player(db, name: str | None, team: str | None = None,
                   position: str | None = None, player_id: str | None = None) -> dict:
    """``{name, position, team, player_id}`` for a named player, best effort.

    Given fields win; missing ones come from the athlete cache (exact name
    first, then the team/position hint, then a fantasy position with a team).
    A bare team code is that team's defense.
    """
    from .opportunity_tools import norm_name

    out = {"name": name, "position": (position or "").upper() or None,
           "team": normalize_team(team) if team else None, "player_id": player_id}
    if db is None:
        return out
    if player_id and not (out["team"] and out["position"]):
        row = (db.get_athletes_by_ids([str(player_id)]) or {}).get(str(player_id)) or {}
        out["team"] = out["team"] or normalize_team(row.get("team_id"))
        out["position"] = out["position"] or (row.get("position") or "").upper() or None
        out["name"] = out["name"] or row.get("full_name")
    if out["team"] and out["position"] and out["player_id"]:
        return out
    if not name:
        return out
    code = normalize_team(name)
    if code and len(name.strip()) <= 4 and (out["position"] in (None, "DEF", "DST")):
        return {"name": code, "position": "DEF", "team": code, "player_id": player_id or code}
    try:
        hits = db.search_athletes_by_name(name, limit=25) or []
    except Exception as e:
        logger.debug(f"athlete search failed for {name!r}: {e}")
        hits = []
    exact = [h for h in hits if norm_name(h.get("full_name")) == norm_name(name)]

    def score(h: dict) -> tuple:
        pos = (h.get("position") or "").upper()
        return (
            bool(out["team"]) and normalize_team(h.get("team_id")) == out["team"],
            bool(out["position"]) and pos == out["position"],
            bool(h.get("team_id")),
            pos in _FANTASY,
        )

    pool = exact or hits
    if pool:
        best = max(pool, key=score)
        out["team"] = out["team"] or normalize_team(best.get("team_id"))
        out["position"] = out["position"] or (best.get("position") or "").upper() or None
        out["player_id"] = out["player_id"] or (str(best["id"]) if best.get("id") else None)
        out["name"] = best.get("full_name") or name
    return out


def recent_snap_share(db, player_id: str | None, season: int | None, week: int | None) -> float | None:
    """The player's offensive snap share in his latest recorded week before `week`."""
    if db is None or not player_id or not season or not week:
        return None
    for w in range(int(week) - 1, max(0, int(week) - 3), -1):
        try:
            rows = db.get_usage_for_week(int(season), w) or []
        except Exception:
            return None
        row = next((r for r in rows if str(r.get("player_id")) == str(player_id)), None)
        if row and row.get("snap_share") is not None:
            return float(row["snap_share"])
    return None


def player_input(db, item, season: int | None, week: int | None) -> dict:
    """A roster-recommendation input from a name or a partial player dict."""
    if isinstance(item, str):
        item = {"name": item}
    item = dict(item or {})
    who = resolve_player(db, item.get("name") or item.get("player_name"), item.get("team"),
                         item.get("position"), item.get("player_id"))
    out = {**item, "name": who["name"] or item.get("name"), "position": who["position"],
           "team": who["team"], "player_id": who["player_id"] or item.get("player_id") or "",
           "opponent": item.get("opponent") or ""}
    usage = dict(item.get("usage") or {})
    if usage.get("snap_percentage") is None:
        snap = recent_snap_share(db, out["player_id"], season, week)
        if snap is not None:
            usage["snap_percentage"] = snap
    if usage:
        out["usage"] = usage
    return out


async def analyze_lineup(
    league_id: str | None = None,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    lineup: dict | None = None,
    db=None,
) -> dict:
    """Grade the lineup set in the league (or a supplied `lineup` dict)."""
    from . import lineup_optimizer_tools, sleeper_tools
    from .errors import create_success_response
    from .lineup_slots import starting_slot_list
    from .roster_context import load_roster_players

    if lineup:
        return await lineup_optimizer_tools.analyze_full_lineup(
            lineup=lineup, week=week, league_id=league_id, season=season)
    if not league_id:
        return create_success_response({"success": False,
                                        "error": "Pass league_id with roster_id or user_id (or a lineup dict)."})
    ctx = await load_roster_players(league_id, roster_id, user_id, db=db, season=season, week=week)
    if ctx["error"]:
        return create_success_response({"success": False, "error": ctx["error"]})
    season, week = ctx["season"], ctx["week"]
    league, roster = ctx["league"], ctx["roster"]

    # This week's set lineup: the matchup's starters when Sleeper has them (they
    # follow the week), else the roster's.
    starters = ctx["starters"]
    try:
        matchups = ((await sleeper_tools.get_matchups(league_id, week)) or {}).get("matchups") or []
        mine = next((m for m in matchups if m.get("roster_id") == ctx["roster_id"]), None)
        if mine and mine.get("starters"):
            starters = [str(p) for p in mine["starters"]]
    except Exception as e:
        logger.debug(f"matchup starters unavailable: {e}")

    by_id = {p["player_id"]: p for p in ctx["players"]}
    slots = starting_slot_list(league.get("roster_positions"))
    built: dict[str, list[dict]] = {}
    started: set[str] = set()
    empty_slots = []
    for slot, pid in zip(slots, starters, strict=False):
        p = by_id.get(str(pid))
        if not p:
            empty_slots.append(slot)
            continue
        started.add(p["player_id"])
        built.setdefault(slot, []).append(player_input(db, {
            "name": p["name"], "position": p["position"], "team": p["team"],
            "player_id": p["player_id"], "opponent": p["opponent"]}, season, week))
    built["BENCH"] = [
        player_input(db, {"name": p["name"], "position": p["position"], "team": p["team"],
                          "player_id": p["player_id"], "opponent": p["opponent"]}, season, week)
        for p in ctx["players"] if p["player_id"] not in started
    ]
    result = await lineup_optimizer_tools.analyze_full_lineup(
        lineup=built, week=week, league_id=league_id, season=season)
    if isinstance(result, dict):
        result["league"] = {"league_id": league_id, "name": league.get("name")}
        result["roster_id"] = ctx["roster_id"]
        result["empty_slots"] = empty_slots
        result["reserve"] = [str(p) for p in (roster.get("reserve") or [])]
    return result


__all__ = ["analyze_lineup", "player_input", "recent_snap_share", "resolve_player"]
