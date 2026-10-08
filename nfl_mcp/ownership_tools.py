"""Who owns a player in a league: ``get_player_ownership``.

An ad-hoc check once answered "Tyreek Hill is a free agent in both leagues":
Hill had no NFL team (the athlete cache's ``team`` was null), the name -> id
lookup was filtered on team, the id came back empty, and an empty id is on no
roster. He was on roster 11 of one of them. Ownership is a question about the
league, not about the NFL, so here

* a name resolves with `lineup_tools.name_candidates(include_free_agents=True)`:
  players without an NFL team count, fantasy positions first, and two
  exact-name fantasy players are reported as ambiguous (with the candidates)
  rather than silently picked;
* rosters come from the shared fresh copy (`sleeper_tools.get_rosters`, which
  bypasses an old Sleeper CDN copy), and a snapshot too old to say who is
  available is an error, not an answer;
* an unrostered player's waiver state is `waiver_rules.waiver_status` -- the
  same game-lock / clear-day logic `get_waiver_targets` uses -- from his
  team's kickoffs and his latest drop in this league. A player with no NFL
  team has no game to lock him, so only a recent drop holds him.

A name that matches nobody is listed under ``unresolved`` -- never reported as
a free agent.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from .database import get_shared_db
from .errors import create_success_response
from .game_clock import parse_kickoff, week_games
from .teams import normalize_team
from .waiver_rules import latest_drops, waiver_rules, waiver_status

logger = logging.getLogger(__name__)

# Most names one call resolves.
MAX_PLAYERS = 25
FREE_AGENT_TEAM = "FA"
ROSTERED = "rostered"
FREE_AGENT = "free_agent"
ON_WAIVERS = "on_waivers"
# Unrostered, but whether he is locked on waivers cannot be told (no cached
# kickoffs for his team): the app decides.
UNROSTERED = "unrostered"


def _now() -> datetime:
    return datetime.now(UTC)


def _owners(users: list[dict]) -> dict[str, dict]:
    return {str(u.get("user_id")): u for u in users or [] if u.get("user_id")}


def _holding(rosters: list[dict]) -> dict[str, tuple[dict, str]]:
    """``{player_id: (roster, slot)}``; slot is starter / bench / ir / taxi."""
    held: dict[str, tuple[dict, str]] = {}
    for r in rosters or []:
        reserve = {str(p) for p in r.get("reserve") or []}
        taxi = {str(p) for p in r.get("taxi") or []}
        starters = {str(p) for p in r.get("starters") or []}
        for pid in {str(p) for p in (r.get("players") or [])} | reserve | taxi:
            if not pid or pid == "0":
                continue
            slot = ("ir" if pid in reserve else "taxi" if pid in taxi
                    else "starter" if pid in starters else "bench")
            held[pid] = (r, slot)
    return held


def resolve_names(db, names: list[str]) -> tuple[list[dict], list[dict]]:
    """``(resolved, unresolved)`` for player names; teamless players count."""
    from .lineup_tools import candidate_summary, name_candidates

    resolved, unresolved = [], []
    for name in names:
        ranked, ambiguous = name_candidates(db, name, include_free_agents=True)
        if not ranked or not ranked[0].get("id"):
            unresolved.append({"name": name, "reason": "no player by that name in the athlete cache"})
            continue
        best = ranked[0]
        entry = {
            "query": name,
            "name": best.get("full_name") or name,
            "player_id": str(best["id"]),
            "position": (best.get("position") or "").upper() or None,
            "team": normalize_team(best.get("team_id")) or FREE_AGENT_TEAM,
        }
        if ambiguous:
            entry["ambiguous"] = True
            entry["candidates"] = candidate_summary(ranked)
        resolved.append(entry)
    return resolved, unresolved


async def get_player_ownership(league_id: str, players: list[str]) -> dict:
    """Whether each named player is rostered in a league (and by whom), or a
    free agent / on waivers. See the module doc."""
    from . import sleeper_tools

    names = [str(n).strip() for n in players or [] if str(n or "").strip()]
    if not names:
        return create_success_response({"success": False,
                                        "error": "players: give at least one player name"})
    if len(names) > MAX_PLAYERS:
        return create_success_response({
            "success": False, "error": f"players: at most {MAX_PLAYERS} names per call"})
    db = get_shared_db()
    resolved, unresolved = resolve_names(db, names)

    state, league_resp, rosters_resp, users_resp = await asyncio.gather(
        sleeper_tools.get_nfl_state(), sleeper_tools.get_league(league_id),
        sleeper_tools.get_rosters(league_id), sleeper_tools.get_league_users(league_id),
        return_exceptions=True)
    rosters_resp = {} if isinstance(rosters_resp, BaseException) else rosters_resp
    freshness = sleeper_tools.roster_freshness(rosters_resp)
    error = sleeper_tools.availability_error(freshness)
    if error:
        return create_success_response({
            "success": False, "error": error, "league_id": league_id,
            "stale": freshness["stale"], "snapshot_age_seconds": freshness["snapshot_age_seconds"],
            "unresolved": unresolved})
    league = {} if isinstance(league_resp, BaseException) else (league_resp or {}).get("league") or {}
    users = [] if isinstance(users_resp, BaseException) else (users_resp or {}).get("users") or []
    nfl_state = {} if isinstance(state, BaseException) else (state or {}).get("nfl_state") or {}
    season = int(nfl_state.get("season") or 0) or None
    week = int(nfl_state.get("week") or 0) or None

    held = _holding(freshness["rosters"])
    owners = _owners(users)
    # An ambiguous name: say where each same-name player is, too.
    for p in resolved:
        for c in p.get("candidates") or []:
            hit = held.get(c.get("player_id") or "")
            c["roster_id"] = hit[0].get("roster_id") if hit else None
    warnings =[freshness["warning"]] if freshness.get("warning") else []

    unrostered = [p for p in resolved if p["player_id"] not in held]
    timing_ctx: dict = {}
    if unrostered:
        timing_ctx = await _timing_context(db, league_id, season, week)
        if timing_ctx.get("warning"):
            warnings.append(timing_ctx["warning"])
    now = _now()

    out = []
    for p in resolved:
        hit = held.get(p["player_id"])
        if hit:
            roster, slot = hit
            owner = owners.get(str(roster.get("owner_id"))) or {}
            out.append({
                **p, "status": ROSTERED, "roster_id": roster.get("roster_id"),
                "owner_id": roster.get("owner_id"),
                "owner": owner.get("display_name"),
                "team_name": (owner.get("metadata") or {}).get("team_name")
                or owner.get("display_name"),
                "slot": slot, "on_ir": slot == "ir", "on_taxi": slot == "taxi",
            })
            continue
        out.append({**p, **_unrostered_status(p, league, timing_ctx, now)})

    rostered = sum(1 for p in out if p["status"] == ROSTERED)
    return create_success_response({
        "league_id": league_id, "league_name": league.get("name"),
        "season": season, "week": week,
        "players": out,
        # Names that matched nobody: unknown, NOT free agents.
        "unresolved": unresolved,
        "summary": {"rostered": rostered, "not_rostered": len(out) - rostered,
                    "unresolved": len(unresolved)},
        "waiver_rules": waiver_rules(league) if league else None,
        "stale": freshness["stale"],
        "snapshot_age_seconds": freshness["snapshot_age_seconds"],
        "warnings": warnings,
        "method": ("names resolved against the athlete cache including players without an NFL "
                   "team; ownership from this league's live rosters (IR and taxi count as "
                   "rostered); waiver timing estimated like get_waiver_targets — the league app "
                   "is the authority"),
    })


async def _timing_context(db, league_id: str, season: int | None, week: int | None) -> dict:
    """Kickoffs (this and last week) and the league's latest drops."""
    from . import sleeper_tools

    games = week_games(db, season, week)
    previous = week_games(db, season, week - 1) if week and week > 1 else {}
    weeks = sorted({w for w in (week, (week or 1) - 1) if w and w > 0})
    drops: dict = {}
    warning = None
    try:
        resps = await asyncio.gather(
            *(sleeper_tools.get_transactions(league_id, week=w) for w in weeks),
            return_exceptions=True)
        for resp in resps:
            if isinstance(resp, BaseException):
                raise resp
            for pid, when in latest_drops((resp or {}).get("transactions")).items():
                if pid not in drops or when > drops[pid]:
                    drops[pid] = when
    except Exception as e:
        logger.debug(f"transactions unavailable for ownership timing: {e}")
        warning = ("League transactions unavailable — a recent drop's waiver period is not "
                   "reflected in the timing.")
    return {"games": games, "previous": previous, "drops": drops, "warning": warning}


def _unrostered_status(p: dict, league: dict, ctx: dict, now: datetime) -> dict:
    """``{status, waiver_timing, note?}`` for a player nobody rosters."""
    team = p["team"] if p["team"] != FREE_AGENT_TEAM else None
    dropped = (ctx.get("drops") or {}).get(p["player_id"])
    if team:
        timing = waiver_status(
            league,
            kickoff=parse_kickoff(((ctx.get("games") or {}).get(team) or {}).get("kickoff")),
            previous_kickoff=parse_kickoff(
                ((ctx.get("previous") or {}).get(team) or {}).get("kickoff")),
            dropped_at=dropped, now=now)
        note = None
    else:
        # No NFL team, no game to lock him: only a drop's clear days can hold
        # him. waiver_status reads "no kickoffs" as unknown, which here it is not.
        timing = waiver_status(league, dropped_at=dropped, now=now)
        if timing["on_waivers"] is None:
            timing.update(on_waivers=False, instant_add=True,
                          reason="no NFL team, so no game lock; not dropped recently",
                          claim_processes_at=None, claim_processes_at_local="instant")
        note = ("No NFL team (unsigned / released): he scores nothing until a team signs him; "
                "waiver tools do not rank him.")
    status = (ON_WAIVERS if timing["on_waivers"] else
              FREE_AGENT if timing["on_waivers"] is False else UNROSTERED)
    if dropped is not None:
        timing["dropped_at"] = dropped.isoformat()
    return {"status": status, "waiver_timing": timing, **({"note": note} if note else {})}
