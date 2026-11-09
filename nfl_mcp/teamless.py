"""Rostered players with no NFL team.

A player his team releases keeps his fantasy roster spot until the manager
drops him, but he has no game to score in. The projection tools skip him
(no opponent, nothing to project), so before this he simply vanished from
the drop lists and the briefing named him nowhere but `not_projected`:
Kendre Miller, released by the Saints on 2026-11-07, sat on a VLBG bench
through Sunday and Monday with no warning. Pure; the callers read the rows.
"""
from __future__ import annotations

from .player_pool import raw_of
from .teams import normalize_team

# Team defenses carry their team in the id, not in `team_id`.
_UNIT_POSITIONS = {"DEF", "DST"}

REASON = "no NFL team (released or unsigned): he cannot score until a team signs him"
ACTION = "drop him now: a player without a game is never locked"


def has_no_team(row: dict | None) -> bool:
    """Whether an athlete row is a player (not a defense) with no NFL team."""
    if not row:
        return False
    if (row.get("position") or "").upper() in _UNIT_POSITIONS:
        return False
    return not normalize_team(row.get("team_id") or raw_of(row).get("team"))


def teamless_rostered(player_ids, athletes: dict, reserve=None) -> list[dict]:
    """``[{player_id, name, position, in_reserve, reason, action}]`` for the
    rostered ``player_ids`` whose athlete row (``athletes``: id -> row) has no
    NFL team. A player missing from ``athletes`` is skipped (unknown, not
    teamless)."""
    reserve_ids = {str(p) for p in (reserve or [])}
    out = []
    for pid in player_ids or []:
        row = athletes.get(str(pid)) or athletes.get(pid)
        if not has_no_team(row):
            continue
        assert row is not None
        out.append({
            "player_id": str(pid),
            "name": row.get("full_name") or str(pid),
            "position": (row.get("position") or "").upper() or None,
            "in_reserve": str(pid) in reserve_ids,
            "reason": REASON,
            "action": ACTION,
        })
    return out


def as_drop(entry: dict) -> dict:
    """A teamless player in the shape `get_waiver_targets` gives a drop."""
    return {"player_id": entry["player_id"], "name": entry["name"],
            "position": entry["position"], "projected_points": 0.0, "value": None,
            "ros_total": 0.0, "role_security": None, "kickoff": None, "kickoff_local": None,
            "teamless": True}
