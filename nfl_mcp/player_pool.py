"""Which athlete rows are real, playing options — for free-agent pools.

The athletes table is Sleeper's full player list, and that list keeps a team
on players who will not play for it: a practice-squad or international-pathway
kicker (GB carried Lenny Krieg next to its starter Trey Smack, and both came
back from `get_waiver_targets` projected at 9.5), and players retired for years
whose rows were never cleared (Ben Roethlisberger, PIT, "Active"). A kicker's
projection is priced off the team's total, so every kicker a team lists got
the starter's number.

Sleeper's own `depth_chart_order` is the signal: the starter has one, the
extra kicker and the ghost rows do not. A team whose kickers all lack it (the
feed not filled in yet) keeps them all rather than losing its kicker.
"""
from __future__ import annotations

import json
import time

from .teams import normalize_team

# One team, one kicker. The other positions carry real depth (a backup RB is a
# legitimate claim), so only K is cut to the depth-chart starter.
STARTER_ONLY_POSITIONS = frozenset({"K"})

# No depth-chart spot and no news in this long: a row Sleeper stopped
# maintaining, not a player on the roster.
_GHOST_AFTER_DAYS = 365


def raw_of(row: dict | None) -> dict:
    """The stored Sleeper payload of an athlete row, as a dict."""
    raw = (row or {}).get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def depth_order(row: dict | None) -> int | None:
    """Sleeper's `depth_chart_order` for the row, or None."""
    value = raw_of(row).get("depth_chart_order")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_ghost(row: dict, now: float | None = None) -> bool:
    """A row with a team that is not a player on it: Sleeper marks him not
    active, or he has no depth-chart spot and no news for a year or more."""
    position = (row.get("position") or "").upper()
    if position in ("DEF", "DST"):
        return False
    raw = raw_of(row)
    if raw.get("active") is False:
        return True
    # Sleeper's payload always carries the depth key (null when unset); a row
    # without it is not a Sleeper record and there is nothing to judge by.
    if "depth_chart_order" not in raw or depth_order(row) is not None:
        return False
    news_ms = raw.get("news_updated")
    if not news_ms:
        return True  # never any news, no depth spot
    now = time.time() if now is None else now
    try:
        return now - float(news_ms) / 1000 > _GHOST_AFTER_DAYS * 86400
    except (TypeError, ValueError):
        return False


def starters_by_team(rows: list[dict], position: str) -> dict[str, set[str]]:
    """``{team: {athlete ids}}`` of the depth-chart starter(s) at ``position``.

    ``rows`` must be every athlete at the position — rostered ones included —
    since the starter being on someone's roster is exactly when the free
    backup looks like a claim. Teams where nobody has a depth order are left
    out, which callers read as "unknown, keep them all".
    """
    by_team: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        if (row.get("position") or "").upper() != position:
            continue
        team = normalize_team(row.get("team_id") or row.get("team"))
        order = depth_order(row)
        if team and order is not None:
            by_team.setdefault(team, []).append((order, str(row.get("id"))))
    return {
        team: {pid for order, pid in entries if order == min(o for o, _ in entries)}
        for team, entries in by_team.items()
    }


def playing_options(rows: list[dict], reference: list[dict] | None = None) -> list[dict]:
    """``rows`` minus ghosts and non-starting kickers.

    ``reference`` is every athlete at the positions involved (defaults to
    ``rows``), used to tell who the starter is.
    """
    reference = rows if reference is None else reference
    starters = {pos: starters_by_team(reference, pos) for pos in STARTER_ONLY_POSITIONS}
    now = time.time()
    kept = []
    for row in rows:
        if is_ghost(row, now):
            continue
        position = (row.get("position") or "").upper()
        if position in starters:
            team = normalize_team(row.get("team_id") or row.get("team"))
            team_starters = starters[position].get(team)
            if team_starters is not None and str(row.get("id")) not in team_starters:
                continue
        kept.append(row)
    return kept
