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


# Sleeper-enrichment trend words -> the optimizer's USAGE_TREND_SCORES keys.
_TREND_WORDS = {"up": "upward", "down": "downward", "flat": "stable"}
# Below this many team targets in the sample the share is noise (a partial ingest).
_MIN_TEAM_TARGETS = 15
_team_targets_cache: dict[tuple, dict[str, float]] = {}


def clear_usage_cache() -> None:
    _team_targets_cache.clear()


def _team_week_targets(db, season: int, week: int) -> dict[str, float]:
    """``{team: targets}`` for one recorded week of ``player_usage_stats``."""
    key = (id(db), int(season), int(week))
    hit = _team_targets_cache.get(key)
    if hit is not None:
        return hit
    try:
        rows = db.get_usage_for_week(int(season), int(week)) or []
        ids = [str(r.get("player_id")) for r in rows if r.get("targets")]
        teams = db.get_athletes_by_ids(ids) if ids else {}
    except Exception as e:
        logger.debug(f"team targets unavailable for {season} week {week}: {e}")
        return {}
    totals: dict[str, float] = {}
    for r in rows:
        if not r.get("targets"):
            continue
        team = normalize_team((teams.get(str(r.get("player_id"))) or {}).get("team_id"))
        if team:
            totals[team] = totals.get(team, 0.0) + float(r["targets"])
    if totals:  # an empty week may simply not be ingested yet
        if len(_team_targets_cache) > 64:
            _team_targets_cache.clear()
        _team_targets_cache[key] = totals
    return totals


def recent_usage(db, player_id: str | None, position: str | None, team: str | None,
                 season: int | None, week: int | None) -> dict:
    """``{target_share, red_zone_opportunities, usage_trend}`` from the last 3 weeks.

    The same ``player_usage_stats`` rows the roster enrichment reads: target
    share is the player's targets over his team's in the weeks he has a row,
    red-zone opportunities the per-game RZ touches, the trend the enrichment's
    targets (else snaps) trend. Only what the data supports is returned — a
    missing key keeps the optimizer's neutral default.
    """
    out: dict = {}
    if (db is None or not player_id or not season or not week
            or (position or "").upper() not in ("RB", "WR", "TE")
            or not hasattr(db, "get_usage_weekly_breakdown")):
        return out
    try:
        weeks = db.get_usage_weekly_breakdown(str(player_id), int(season), int(week), n=3) or []
    except Exception as e:
        logger.debug(f"usage breakdown unavailable for {player_id}: {e}")
        return out
    if not isinstance(weeks, list) or not weeks:
        return out
    rz = [float(r["rz_touches"]) for r in weeks if r.get("rz_touches") is not None]
    if rz:
        out["red_zone_opportunities"] = round(sum(rz) / len(rz))
    from .sleeper_enrichment import _calculate_usage_trend
    trend = _calculate_usage_trend(weeks, "targets") or _calculate_usage_trend(weeks, "snap_share")
    if trend:
        out["usage_trend"] = _TREND_WORDS.get(trend, "stable")
    team = normalize_team(team) if team else None
    if team:
        own = team_total = 0.0
        for r in weeks:
            if r.get("targets") is None or r.get("week") is None:
                continue
            total = _team_week_targets(db, season, r["week"]).get(team)
            if total:
                own += float(r["targets"])
                team_total += total
        if team_total >= _MIN_TEAM_TARGETS:
            out["target_share"] = round(100.0 * own / team_total, 1)
    return out


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
    # Target share / red-zone work / trend: without them the optimizer's
    # target-share bonus never fired and every trend read "stable".
    for key, value in recent_usage(db, out["player_id"], out["position"], out["team"],
                                   season, week).items():
        usage.setdefault(key, value)
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
    # Raw: zipped against the slot list below, so the "0" of an empty slot
    # must keep its place or every later starter shifts one slot.
    starters = ctx.get("starters_raw") or ctx["starters"]
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
    occupied_unknown = []
    for i, slot in enumerate(slots):
        pid = starters[i] if i < len(starters) else None
        p = by_id.get(str(pid)) if pid else None
        if not p:
            if pid and str(pid) != "0":
                # Someone holds the seat, the athlete cache just cannot place
                # him (a new signing, no team). Not an open seat: never
                # suggested for filling, and left out of the grade.
                occupied_unknown.append({"slot": slot, "player_id": str(pid)})
                continue
            # "0" or no entry at all: an open seat the optimizer should fill.
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
        lineup=built, week=week, league_id=league_id, season=season,
        empty_slots=empty_slots)
    if isinstance(result, dict):
        result["league"] = {"league_id": league_id, "name": league.get("name")}
        result["roster_id"] = ctx["roster_id"]
        result["empty_slots"] = empty_slots
        result["occupied_unknown_slots"] = occupied_unknown
        reserve_ids = [str(p) for p in (roster.get("reserve") or [])]
        try:
            rows = db.get_athletes_by_ids(reserve_ids) if db is not None and reserve_ids else {}
        except Exception:
            rows = {}
        rows = rows if isinstance(rows, dict) else {}
        result["reserve"] = [
            {"player_id": pid, "name": (rows.get(pid) or {}).get("full_name") or pid,
             "position": (rows.get(pid) or {}).get("position")}
            for pid in reserve_ids
        ]
        warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
        warnings = list(warnings)
        if occupied_unknown:
            warnings.append(
                f"{len(occupied_unknown)} starting slot(s) hold a player the athlete cache "
                "cannot place (" + ", ".join(f"{o['slot']}: {o['player_id']}" for o in occupied_unknown)
                + ") — occupied, not graded, not offered for filling.")
        if ctx.get("roster_warning"):
            warnings.append(ctx["roster_warning"])
        if warnings:
            result["warnings"] = warnings
        result["stale"] = ctx.get("stale", False)
        result["snapshot_age_seconds"] = ctx.get("snapshot_age_seconds")
    return result


__all__ = ["analyze_lineup", "player_input", "recent_snap_share", "recent_usage", "resolve_player"]
