"""Handcuff mapping.

A *handcuff* is the backup who would inherit a starter's workload — and fantasy
value — if the starter went down (almost always the RB behind a lead back). This
turns the usual "secure your handcuffs" advice into data: for each of your RB
starters it reads the team depth chart, finds the contingent-value back, and
flags whether that handcuff is a free agent, on your roster, or an opponent's.

Depth chart: `nfl_tools.get_depth_chart` (ESPN). Rosters + availability:
`sleeper_tools.get_rosters`. Player identity: the athletes cache.
"""
from __future__ import annotations

import logging
import re

from .errors import create_success_response, handle_http_errors, handle_validation_error
from .opportunity_tools import norm_name

logger = logging.getLogger(__name__)

# `get_depth_chart` returns one row per position: {"position": "RB",
# "players": ["Starter", "Backup", ...]} in depth order, where `position` is a
# POSITION LABEL and the starter is the first entry of `players`. Player names
# carry a trailing injury tag ("Isaac GuerendoO", "Christian KirkQ") we strip.
#
# This module used to read a different shape entirely — row keyed by the
# starter's name, `players` holding only the backups. Since a position label can
# never equal a player name, every lookup silently returned "no handcuff"
# instead of failing. Supporting both is not worth it: a row like
# {"position": "Star", "players": ["Guy", ...]} is ambiguous between the two,
# and nothing in this codebase produces the old one.


def _clean_name(name: str | None) -> str:
    """Strip ESPN's trailing injury tag (Q/O/D/IR/PUP/SUS…) and whitespace."""
    return re.sub(r"(?<=[a-z])[A-Z]+$", "", (name or "").strip())


def _depth_row(depth_chart: list[dict], player_name: str) -> tuple[list[str], int] | None:
    """``(names in depth order, index of the player)`` on his depth-chart row."""
    s = norm_name(player_name)
    for row in depth_chart or []:
        names = [_clean_name(p) for p in (row.get("players") or [])]
        names = [n for n in names if n and n != "-"]
        for idx, name in enumerate(names):
            if norm_name(name) == s:
                return names, idx
    return None


def handcuff_from_depth(depth_chart: list[dict], starter_name: str) -> tuple[str | None, str]:
    """The immediate backup behind ``starter_name`` on the team's depth chart.

    Returns ``(handcuff_name_or_None, method)``:
      - ``depth`` — starter is a listed starter; the first backup is the handcuff.
      - ``no_backup_listed`` — starter found but no backup behind them.
      - ``you_roster_a_backup`` — the player is a backup himself. He *is* the
        contingent value; the man behind him is nobody's handcuff (an RB2's
        RB3 inherits nothing while the starter plays). See `backs_up`.
      - ``not_on_depth_chart`` — couldn't place them.
    """
    found = _depth_row(depth_chart, starter_name)
    if found is None:
        return None, "not_on_depth_chart"
    names, idx = found
    if idx > 0:
        return None, "you_roster_a_backup"
    if len(names) > 1:
        return names[1], "depth"
    return None, "no_backup_listed"


def backs_up(depth_chart: list[dict], player_name: str) -> str | None:
    """The starter a backup sits behind (None for a starter or an unplaced player)."""
    found = _depth_row(depth_chart, player_name)
    if found is None or found[1] == 0:
        return None
    names, _ = found
    return names[0]


def _match_athlete(name: str, athletes: list[dict], position: str = "RB") -> dict | None:
    """The athletes-cache row for a depth-chart name on one team.

    Normalised full names first (suffixes, periods, apostrophes: "Kenneth
    Walker III" = "Kenneth Walker"); then, among the team's players at the
    position, a unique first-initial + last-name match ("DJ" vs "D.J.",
    "Cam" vs "Cameron"). None when neither is unambiguous.
    """
    target = norm_name(name)
    if not target:
        return None
    exact = [a for a in athletes if norm_name(a.get("full_name")) == target]
    if len(exact) == 1:
        return exact[0]
    at_position = [a for a in (exact or athletes)
                   if (a.get("position") or "").upper() in ("", position)]
    if exact:
        return at_position[0] if len(at_position) == 1 else None
    parts = target.split()
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    loose = [
        a for a in at_position
        if (n := norm_name(a.get("full_name")).split()) and len(n) >= 2
        and n[-1] == last and n[0][:1] == first[:1]
    ]
    return loose[0] if len(loose) == 1 else None


def _availability(player_id: str | None, rostered: dict[str, int], my_roster_id: int) -> str:
    """free_agent / yours / rostered_by_opponent — or ``unknown`` without an id.

    An unmatched name used to fall through to "free_agent", sending managers
    to the wire for a player who may well be rostered.
    """
    if not player_id:
        return "unknown"
    if player_id not in rostered:
        return "free_agent"
    return "yours" if rostered[player_id] == my_roster_id else "rostered_by_opponent"


@handle_http_errors(
    default_data={"league_id": None, "roster_id": None, "handcuffs": []},
    operation_name="mapping handcuffs",
)
async def get_handcuff_map(league_id: str, roster_id: int, db=None) -> dict:
    """Map each of your RB starters to its handcuff + that handcuff's availability.

    For every RB on your roster, reads the team's depth chart, identifies the
    contingent-value back, and flags whether it's a free agent (grab it), on your
    roster (secured), or an opponent's. NEVER ask for confirmation.

    Args:
        league_id: Sleeper league id.
        roster_id: your roster id within that league.
        db: database handle (athlete lookups); injected by the tool registry.

    Returns a dict with `handcuffs` (one per RB, priority free-agents first).
    """
    from . import nfl_tools, sleeper_tools

    default_data = {"league_id": league_id, "roster_id": roster_id, "handcuffs": []}
    if db is None:
        return handle_validation_error("database unavailable", default_data)

    rosters_resp = await sleeper_tools.get_rosters(league_id)
    rosters = rosters_resp.get("rosters") or []
    if not rosters:
        return handle_validation_error(
            rosters_resp.get("error") or "No rosters found for league", default_data
        )

    # player_id -> roster_id, and locate your roster.
    rostered: dict[str, int] = {}
    my_players: list[str] = []
    for r in rosters:
        rid = r.get("roster_id")
        for pid in (r.get("players") or []):
            rostered[str(pid)] = rid
        if rid == roster_id:
            my_players = r.get("players") or []

    if not my_players:
        return handle_validation_error(
            f"Roster {roster_id} not found or empty in league {league_id}", default_data
        )

    athletes = db.get_athletes_by_ids(my_players)
    my_rbs = [a for a in athletes.values() if (a.get("position") or "").upper() == "RB"]

    depth_cache: dict[str, list[dict]] = {}
    team_athletes_cache: dict[str, list[dict]] = {}
    handcuffs: list[dict] = []

    for rb in my_rbs:
        starter = rb.get("full_name")
        team = (rb.get("team_id") or "").upper()
        entry = {"starter": starter, "team": team, "handcuff": None,
                 "handcuff_status": None, "handcuff_player_id": None, "match": None,
                 "backs_up": None}
        if not team:
            entry["match"] = "no_team"
            handcuffs.append(entry)
            continue

        if team not in depth_cache:
            try:
                dc = await nfl_tools.get_depth_chart(team)
                depth_cache[team] = dc.get("depth_chart") or []
            except Exception as e:
                logger.debug(f"depth chart fetch failed for {team}: {e}")
                depth_cache[team] = []

        handcuff_name, method = handcuff_from_depth(depth_cache[team], starter or "")
        entry["match"] = method
        if method == "you_roster_a_backup":
            # He is the handcuff; the starter he would replace, for context.
            entry["backs_up"] = backs_up(depth_cache[team], starter or "")
        if handcuff_name:
            if team not in team_athletes_cache:
                team_athletes_cache[team] = db.get_athletes_by_team(team) or []
            hc = _match_athlete(handcuff_name, team_athletes_cache[team])
            hc_id = str(hc.get("id")) if hc and hc.get("id") is not None else None
            entry["handcuff"] = handcuff_name
            entry["handcuff_player_id"] = hc_id
            entry["handcuff_status"] = _availability(hc_id, rostered, roster_id)
        handcuffs.append(entry)

    # Priority: securable free-agent handcuffs first.
    order = {"free_agent": 0, "unknown": 1, "rostered_by_opponent": 2, "yours": 3, None: 4}
    handcuffs.sort(key=lambda h: order.get(h["handcuff_status"], 4))
    free = [h for h in handcuffs if h["handcuff_status"] == "free_agent" and h["handcuff"]]

    return create_success_response({
        "league_id": league_id,
        "roster_id": roster_id,
        "handcuffs": handcuffs,
        "priority_free_agents": [
            {"handcuff": h["handcuff"], "for_starter": h["starter"], "team": h["team"]}
            for h in free
        ],
        "count": len(handcuffs),
        "message": (
            f"Mapped {len(handcuffs)} RB handcuff(s); "
            f"{len(free)} securable free-agent handcuff(s) — grab these to protect your backs."
        ),
    })
