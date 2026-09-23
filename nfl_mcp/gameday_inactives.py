"""Official gameday inactives, as far as the public feeds publish them.

Teams declare their inactives about 90 minutes before kickoff. Until then the
best available answer is the injury report's designations, which is what
``get_gameday_inactives`` used to return at all times — a severity filter over
the ESPN injury list, labelled as if it were the inactive list.

What the feeds carry, checked on 2026-09-23 against week 2:

- ESPN's game summary (``/summary?event=<id>``) has *no* inactives section —
  only the recap prose mentions them after the fact.
- ESPN's league injury list carries the RotoWire notes posted at inactive time:
  "Goodson (coach's decision) is inactive for Sunday's game against the
  Commanders", "Seumalo (shoulder) is active for Sunday's game". Dated, per
  player, and covering every fantasy-relevant question mark. Primary.
- Sleeper's player feed: ``injury_status == "Inactive"`` when Sleeper sets it.
  Read too (from the stored athletes, else a once-a-day download of the
  dump), but it was not seen in any live payload so far.

A note only counts for the game it was posted around (from three hours before
kickoff to the end of the game); anything older is last week's.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from .game_clock import GAME_LENGTH, parse_kickoff
from .opportunity_tools import norm_name
from .teams import normalize_team

logger = logging.getLogger(__name__)

ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    "?dates={season}&seasontype=2&week={week}"
)
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

# Inactives are due 90 minutes before kickoff; the window opens a little
# earlier so an early list is not missed.
WINDOW_LEAD = timedelta(hours=2)
# How far before kickoff a note may be posted and still be about this game.
NOTE_LEAD = timedelta(hours=3)

SOURCE_ESPN_NOTE = "espn_gameday_note"
SOURCE_SLEEPER = "sleeper_injury_status"

_INACTIVE_RE = re.compile(r"\b(?:is|are|was|will be|has been (?:ruled|declared))\s+inactive\b", re.I)
_ACTIVE_RE = re.compile(r"\b(?:is|was|will be)\s+active\b", re.I)


def game_phase(kickoff: str | None, now: datetime | None = None) -> str:
    """``upcoming`` / ``inactives_window`` / ``in_progress`` / ``final`` / ``unknown``."""
    start = parse_kickoff(kickoff)
    if start is None:
        return "unknown"
    now = now or datetime.now(UTC)
    if now < start - WINDOW_LEAD:
        return "upcoming"
    if now < start:
        return "inactives_window"
    if now < start + GAME_LENGTH:
        return "in_progress"
    return "final"


def classify_note(comment: str | None) -> str | None:
    """``inactive`` / ``active`` for a gameday note, else None."""
    text = comment or ""
    if _INACTIVE_RE.search(text):
        return "inactive"
    if _ACTIVE_RE.search(text):
        return "active"
    return None


def parse_espn_gameday_notes(payload: dict, kickoffs: dict[str, str],
                             teams: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """``(inactive, active)`` rows from the ESPN league injury payload.

    Only notes posted between ``NOTE_LEAD`` before the team's kickoff and the
    end of its game count. The newest note per player wins.
    """
    latest: dict[tuple[str, str], dict] = {}
    for group in (payload or {}).get("injuries") or []:
        team = normalize_team(group.get("displayName"))
        if not team or (teams and team not in teams):
            continue
        start = parse_kickoff(kickoffs.get(team))
        if start is None:
            continue
        for item in group.get("injuries") or []:
            kind = classify_note(item.get("shortComment"))
            posted = parse_kickoff(item.get("date"))
            if not kind or posted is None:
                continue
            if not (start - NOTE_LEAD <= posted <= start + GAME_LENGTH):
                continue
            athlete = item.get("athlete") or {}
            name = (athlete.get("displayName") or "").strip()
            if not name:
                continue
            key = (norm_name(name), team)
            if key in latest and latest[key]["posted"] >= item.get("date", ""):
                continue
            latest[key] = {
                "player_name": name, "team_id": team,
                "position": (athlete.get("position") or {}).get("abbreviation"),
                "status": "Inactive" if kind == "inactive" else "Active",
                "note": item.get("shortComment"),
                "posted": item.get("date", ""),
                "kickoff": kickoffs.get(team),
                "source": SOURCE_ESPN_NOTE,
                "official": True,
            }
    rows = list(latest.values())
    return ([r for r in rows if r["status"] == "Inactive"],
            [r for r in rows if r["status"] == "Active"])


def parse_sleeper_inactives(players: dict, teams: set[str]) -> list[dict]:
    """Players Sleeper marks ``Inactive`` on the given teams."""
    out = []
    for pid, p in (players or {}).items():
        if not isinstance(p, dict) or (p.get("injury_status") or "").lower() != "inactive":
            continue
        team = normalize_team(p.get("team"))
        if team not in teams:
            continue
        out.append({
            "player_id": str(pid), "player_name": p.get("full_name"),
            "team_id": team, "position": p.get("position"),
            "status": "Inactive", "note": None, "posted": None,
            "source": SOURCE_SLEEPER, "official": True,
        })
    return out


# Process cache of the full Sleeper player dump (~5 MB), for when the stored
# athletes cannot answer: (fetched_at, players).
_SLEEPER_DUMP_TTL = timedelta(days=1)
_sleeper_dump: tuple[datetime, dict] | None = None


def _stored_sleeper_players(db, teams: set[str]) -> dict:
    """``{id: raw Sleeper player}`` for the teams, from the stored athletes.

    The athletes table holds Sleeper's player payload in ``raw``, refreshed by
    the prefetch; reading it replaces downloading the whole dump on every call
    inside an inactives window.
    """
    import json

    if db is None or not hasattr(db, "get_athletes_by_team"):
        return {}
    players: dict = {}
    for team in teams:
        try:
            rows = db.get_athletes_by_team(team)
        except Exception:
            continue
        for row in rows if isinstance(rows, list) else []:
            raw = row.get("raw") if isinstance(row, dict) else None
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (ValueError, TypeError):
                    raw = None
            if isinstance(raw, dict) and row.get("id"):
                # The stored team is canonical; the raw one can be WAS/OAK.
                players[str(row["id"])] = {**raw, "team": row.get("team_id") or raw.get("team")}
    return players


async def _sleeper_players(db, teams: set[str], client, now: datetime) -> dict | None:
    """Sleeper players for the window teams: stored athletes first, else the
    full dump, fetched at most once a day per process. None when neither is
    available."""
    global _sleeper_dump
    stored = _stored_sleeper_players(db, teams)
    if stored:
        return stored
    if _sleeper_dump and timedelta(0) <= now - _sleeper_dump[0] < _SLEEPER_DUMP_TTL:
        return _sleeper_dump[1]
    from .config import get_http_headers
    resp = await client.get(SLEEPER_PLAYERS_URL, headers=get_http_headers("sleeper_players"),
                            timeout=30.0)
    if resp.status_code != 200:
        return None
    players = resp.json()
    if not isinstance(players, dict):
        return None
    if players:
        _sleeper_dump = (now, players)
    return players


async def _week_kickoffs(db, season: int, week: int, client) -> dict[str, str]:
    """``{team: kickoff}`` from the cached schedule, else ESPN's scoreboard."""
    kickoffs: dict[str, str] = {}
    if db is not None and hasattr(db, "get_week_kickoffs"):
        try:
            kickoffs = {normalize_team(t) or t: k for t, k in (db.get_week_kickoffs(season, week) or {}).items()}
        except Exception:
            kickoffs = {}
    if kickoffs:
        return kickoffs
    try:
        resp = await client.get(ESPN_SCOREBOARD_URL.format(season=season, week=week), timeout=15.0)
        if resp.status_code == 200:
            for event in (resp.json() or {}).get("events") or []:
                for comp in event.get("competitions") or []:
                    for side in comp.get("competitors") or []:
                        team = normalize_team((side.get("team") or {}).get("abbreviation"))
                        if team:
                            kickoffs[team] = event.get("date")
    except Exception as e:
        logger.debug(f"[Inactives] scoreboard fetch failed: {e}")
    return kickoffs


async def get_official_inactives(db, season: int, week: int, teams: list[str] | None = None,
                                 now: datetime | None = None, client=None) -> dict:
    """Published inactives for games whose inactive window is open.

    Returns ``{games: {team: {kickoff, phase}}, window_teams, inactives,
    confirmed_active, sources_checked}``. Fetches nothing beyond the schedule
    when no game is inside its window.
    """
    from .config import create_http_client, get_http_headers

    now = now or datetime.now(UTC)
    wanted = {normalize_team(t) for t in teams or [] if normalize_team(t)}
    own = client is None
    client = client or create_http_client()
    result = {"games": {}, "window_teams": [], "inactives": [], "confirmed_active": [],
              "sources_checked": []}
    try:
        if own:
            await client.__aenter__()
        kickoffs = await _week_kickoffs(db, season, week, client)
        for team, kickoff in kickoffs.items():
            if wanted and team not in wanted:
                continue
            result["games"][team] = {"kickoff": kickoff, "phase": game_phase(kickoff, now)}
        window = {t for t, g in result["games"].items()
                  if g["phase"] in ("inactives_window", "in_progress", "final")}
        result["window_teams"] = sorted(window)
        if not window:
            return result
        headers = get_http_headers("nfl_teams")
        try:
            resp = await client.get(ESPN_INJURIES_URL, headers=headers, timeout=30.0)
            if resp.status_code == 200:
                inactive, active = parse_espn_gameday_notes(resp.json(), kickoffs, window)
                result["inactives"].extend(inactive)
                result["confirmed_active"].extend(active)
                result["sources_checked"].append(SOURCE_ESPN_NOTE)
        except Exception as e:
            logger.warning(f"[Inactives] ESPN notes failed: {e}")
        try:
            players = await _sleeper_players(db, window, client, now)
            if players is not None:
                seen = {(norm_name(r["player_name"]), r["team_id"]) for r in result["inactives"]}
                for row in parse_sleeper_inactives(players, window):
                    if (norm_name(row["player_name"]), row["team_id"]) not in seen:
                        result["inactives"].append(row)
                result["sources_checked"].append(SOURCE_SLEEPER)
        except Exception as e:
            logger.warning(f"[Inactives] Sleeper feed failed: {e}")
    finally:
        if own:
            await client.__aexit__(None, None, None)
    return result
