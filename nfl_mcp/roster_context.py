"""A league roster as the per-player dicts the analysis tools take.

Several tools used to ask the caller for ``players=[{name, team, position,
opponent}]`` — data the server already has. Asking for it invites made-up
inputs (a wrong opponent, a stale team). This loads it instead: the Sleeper
roster, names/teams/positions from the athlete cache, this week's opponent
from the cached schedule.
"""
from __future__ import annotations

import asyncio

from .teams import normalize_team


async def load_roster_players(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    *,
    db,
    season: int | None = None,
    week: int | None = None,
    include_reserve: bool = False,
) -> dict:
    """``{league, roster, roster_id, season, week, players, starters, starters_raw, error}``.

    Each player: ``{player_id, name, position, team, opponent, starter, slot}``;
    ``opponent`` is "BYE" when the week's schedule is complete and has no game
    for the team, "" when the schedule is unknown. Taxi players are always
    excluded, reserve (IR) players unless ``include_reserve``.
    """
    from . import sleeper_tools
    from .lineup_slots import starting_slot_list
    from .sleeper_tools import find_roster
    from .week_context import current_season_week, week_opponents, week_schedule

    if week is None or season is None:
        current = await current_season_week(db)
        week = week or current["week"]
        season = season or current["season"]
    league_resp, roster_state = await asyncio.gather(
        sleeper_tools.get_league(league_id), sleeper_tools.load_rosters(league_id, "lineup"))
    league = (league_resp or {}).get("league") or {}
    rosters = roster_state["rosters"]
    # A cached snapshot still says who is on the roster; it is flagged, not refused.
    base = {"league": league, "season": season, "week": week, "players": [], "starters": [],
            "stale": roster_state["stale"],
            "snapshot_age_seconds": roster_state["snapshot_age_seconds"],
            "roster_warning": roster_state["warning"], "unknown_player_ids": []}
    if not league:
        return {**base, "error": f"Could not load league {league_id}."}
    if roster_state["blocking_error"]:
        return {**base, "error": roster_state["blocking_error"]}
    mine, error = find_roster(rosters, league_id, roster_id, user_id)
    if error:
        return {**base, "error": error}

    taxi = {str(p) for p in (mine.get("taxi") or [])}
    reserve = {str(p) for p in (mine.get("reserve") or [])}
    starters = [str(p) for p in (mine.get("starters") or [])]
    slot_of = dict(zip(starters, starting_slot_list(league.get("roster_positions")), strict=False))
    ids = [str(p) for p in (mine.get("players") or [])
           if str(p) not in taxi and (include_reserve or str(p) not in reserve)]
    rows = db.get_athletes_by_ids(ids) if db is not None and ids else {}
    opponents = week_opponents(db, season, week) if db is not None else {}
    schedule = week_schedule(db, season, week) if db is not None else None

    players = []
    unknown: list[str] = []
    for pid in ids:
        row = rows.get(pid) or {}
        team = normalize_team(row.get("team_id") or row.get("team"))
        position = (row.get("position") or "").upper()
        if position == "DST":
            position = "DEF"
        name = row.get("full_name") or (team if position == "DEF" else None)
        if not name or not team or not position:
            # On the roster but not placeable from the athlete cache (a new
            # signing, a free agent): the seat is taken, just not analysable.
            unknown.append(pid)
            continue
        opponent = opponents.get(team) or ("BYE" if schedule is not None else "")
        players.append({
            "player_id": pid, "name": name, "position": position, "team": team,
            "opponent": normalize_team(opponent) or opponent,
            "starter": pid in slot_of, "slot": slot_of.get(pid, "IR" if pid in reserve else "BN"),
        })
    # `starters_raw` keeps Sleeper's "0" placeholders so it still lines up one
    # to one with the league's slot list; `starters` is the players only.
    return {**base, "roster": mine, "roster_id": mine["roster_id"], "players": players,
            "starters": [p for p in starters if p and p != "0"], "starters_raw": starters,
            "unknown_player_ids": unknown, "error": None}


__all__ = ["load_roster_players"]
