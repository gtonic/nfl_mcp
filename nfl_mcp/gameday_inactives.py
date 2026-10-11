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
  Read too, but it was not seen in any live payload so far. Sleeper sets it
  around kickoff, so a daily copy cannot answer: the stored athletes count only
  when refreshed within ``SLEEPER_FRESH_TTL``, else the dump is downloaded (at
  most once per ``SLEEPER_FRESH_TTL`` per process, only inside a window). When
  neither is fresh, Sleeper is not listed in ``sources_checked``.

Every feed read is bounded (``OFFICIAL_BUDGET_SECONDS`` in total, both feeds
at once): a feed that misses the budget keeps downloading in the background,
an older copy stands in where there is one, and the answer says so
(``partial``, ``timed_out_sources``, ``stale_sources``).

A note only counts for the game it was posted around (from three hours before
kickoff to the end of the game); anything older is last week's.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .game_clock import GAME_LENGTH, parse_kickoff
from .opportunity_tools import norm_name
from .teams import normalize_team
from .upstream import Budget, inflight, single_flight, wait_bounded

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


# How old a Sleeper read may be inside an inactives window. Inactive is set
# ~90 minutes before kickoff; a daily snapshot misses it. Also the dump's
# refetch interval: Sleeper asks for the ~5 MB dump to be pulled sparingly, and
# this only runs inside a window (a few hours a week).
SLEEPER_FRESH_TTL = timedelta(minutes=30)
# Process cache of the full Sleeper player dump: (fetched_at, players).
_SLEEPER_DUMP_TTL = SLEEPER_FRESH_TTL
_sleeper_dump: tuple[datetime, dict] | None = None
# A dump past the TTL is still served while a fresh one downloads (its
# Inactive rows are still right; it can miss a late scratch), up to this age.
# It never counts as Sleeper having been checked.
SLEEPER_STALE_MAX = timedelta(hours=6)
# ESPN's league injury payload (the gameday notes): re-read at most this often,
# and served stale up to ESPN_NOTES_STALE_MAX while a re-read is under way.
ESPN_NOTES_TTL = timedelta(minutes=2)
ESPN_NOTES_STALE_MAX = timedelta(hours=3)
_espn_notes: tuple[datetime, dict] | None = None

# Per-request timeouts (seconds), and the total wait of one read of the
# published inactives. Both sources are read at once; one that misses the
# budget keeps downloading in the background (the next call finds it cached)
# and the answer lists it in `timed_out_sources` with `partial: true`. Before,
# every source ran inline, one after another, with 30 s timeouts each.
ESPN_NOTES_TIMEOUT = 10.0
SLEEPER_DUMP_TIMEOUT = 20.0
SCOREBOARD_TIMEOUT = 8.0
OFFICIAL_BUDGET_SECONDS = 12.0
# With a stale copy in hand: how long to wait for the refresh before serving it.
STALE_GRACE_SECONDS = 1.5
# A download already in flight from an earlier call: wait at most this long.
INFLIGHT_WAIT_SECONDS = 2.0
SOURCE_SCOREBOARD = "espn_scoreboard"


def clear_caches() -> None:
    """Forget the cached feeds (tests)."""
    global _sleeper_dump, _espn_notes
    _sleeper_dump = None
    _espn_notes = None
    _gameday_cache.clear()


def _stored_sleeper_players(db, teams: set[str],
                            fresh_since: datetime | None = None) -> tuple[dict, datetime | None]:
    """``({id: raw Sleeper player}, oldest updated_at)`` for the teams, from
    the stored athletes.

    The athletes table holds Sleeper's player payload in ``raw``, refreshed by
    the prefetch. With ``fresh_since``, a team whose rows are older does not
    count (``({}, None)``): a stale copy would miss a gameday Inactive.
    """
    import json

    if db is None or not hasattr(db, "get_athletes_by_team"):
        return {}, None
    players: dict = {}
    oldest: datetime | None = None
    for team in teams:
        try:
            rows = db.get_athletes_by_team(team)
        except Exception:
            continue
        rows = rows if isinstance(rows, list) else []
        stamps = [parse_kickoff(r.get("updated_at")) for r in rows if isinstance(r, dict)]
        stamps = [t for t in stamps if t is not None]
        if fresh_since is not None and (not stamps or min(stamps) < fresh_since):
            return {}, None
        if stamps:
            oldest = min(stamps) if oldest is None else min(oldest, min(stamps))
        for row in rows:
            raw = row.get("raw") if isinstance(row, dict) else None
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (ValueError, TypeError):
                    raw = None
            if isinstance(raw, dict) and row.get("id"):
                # The stored team is canonical; the raw one can be WAS/OAK.
                players[str(row["id"])] = {**raw, "team": row.get("team_id") or raw.get("team")}
    return players, oldest


async def _get(client, url: str, headers: dict | None, timeout: float):
    """GET with the caller's client, else a short-lived one of our own (a
    background read outlives the call that started it, so it cannot borrow
    a client that call closes)."""
    if client is not None:
        return await client.get(url, headers=headers, timeout=timeout)
    from .config import create_http_client
    async with create_http_client() as own:
        return await own.get(url, headers=headers, timeout=timeout)


async def _download_sleeper_dump(client, now: datetime) -> dict | None:
    global _sleeper_dump
    from .config import get_http_headers
    resp = await _get(client, SLEEPER_PLAYERS_URL, get_http_headers("sleeper_players"),
                      SLEEPER_DUMP_TIMEOUT)
    if resp.status_code != 200:
        return None
    players = resp.json()
    if not isinstance(players, dict) or not players:
        return None
    _sleeper_dump = (now, players)
    return players


async def _download_espn_notes(client, now: datetime) -> dict | None:
    global _espn_notes
    from .config import get_http_headers
    resp = await _get(client, ESPN_INJURIES_URL, get_http_headers("nfl_teams"), ESPN_NOTES_TIMEOUT)
    if resp.status_code != 200:
        return None
    payload = resp.json()
    if not isinstance(payload, dict):
        return None
    _espn_notes = (now, payload)
    return payload


async def _cached_read(key: str, cached: tuple[datetime, Any] | None, ttl: timedelta,
                       stale_max: timedelta, download, now: datetime,
                       wait: float) -> tuple[Any, datetime | None, str]:
    """``(value, as_of, status)`` from a process cache with stale-while-revalidate.

    ``status``: ``fresh`` (cached within ``ttl`` or just downloaded),
    ``stale`` (the download missed the wait or failed; an older copy within
    ``stale_max`` is served), ``timeout`` (nothing usable in time) or
    ``failed``. A download that misses the wait keeps running. Never raises.
    """
    age = now - cached[0] if cached else None
    if cached and timedelta(0) <= age < ttl:
        return cached[1], cached[0], "fresh"
    stale = cached if cached and timedelta(0) <= age < stale_max else None
    if inflight(key):
        # An earlier call's download is still running and already missed its
        # own budget: do not wait the full budget for it again.
        wait = min(wait, INFLIGHT_WAIT_SECONDS)
    try:
        task = single_flight(key, download)
        done, value = await wait_bounded(task, min(wait, STALE_GRACE_SECONDS) if stale else wait)
    except Exception as e:
        logger.warning(f"[Inactives] {key} read failed: {e}")
        done, value = True, None
    if done and value is not None:
        return value, now, "fresh"
    if stale:
        return stale[1], stale[0], "stale"
    return None, None, "failed" if done else "timeout"


async def _sleeper_players(db, teams: set[str], client, now: datetime,
                           wait: float = OFFICIAL_BUDGET_SECONDS
                           ) -> tuple[dict | None, dict | None, str]:
    """``(players, freshness, status)`` for the window teams: stored athletes
    when refreshed within ``SLEEPER_FRESH_TTL``, else the dump (cached for the
    same TTL; see `_cached_read` for ``status``). ``(None, None, ...)`` when
    neither is usable -- the caller must then not claim Sleeper was checked."""
    stored, as_of = _stored_sleeper_players(db, teams, fresh_since=now - SLEEPER_FRESH_TTL)
    if stored:
        return stored, _freshness("stored_athletes", as_of, now), "fresh"
    players, as_of, status = await _cached_read(
        "gameday_sleeper_dump", _sleeper_dump, _SLEEPER_DUMP_TTL, SLEEPER_STALE_MAX,
        lambda: _download_sleeper_dump(client, now), now, wait)
    if players is None:
        return None, None, status
    freshness = _freshness("sleeper_dump", as_of, now)
    if status == "stale":
        freshness["stale"] = True
    return players, freshness, status


def _freshness(source: str, as_of: datetime | None, now: datetime) -> dict:
    """How current the Sleeper read was, for the response."""
    return {
        "source": source,
        "as_of": as_of.isoformat() if as_of else None,
        "age_minutes": round((now - as_of).total_seconds() / 60, 1) if as_of else None,
    }


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
        resp = await _get(client, ESPN_SCOREBOARD_URL.format(season=season, week=week), None,
                          SCOREBOARD_TIMEOUT)
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
                                 now: datetime | None = None, client=None,
                                 budget_seconds: float | None = None) -> dict:
    """Published inactives for games whose inactive window is open.

    Returns ``{games: {team: {kickoff, phase}}, window_teams, inactives,
    confirmed_active, sources_checked, sleeper_freshness, partial,
    timed_out_sources, stale_sources}``. Fetches nothing beyond the schedule
    when no game is inside its window. Waits at most ``budget_seconds``
    (default ``OFFICIAL_BUDGET_SECONDS``) for the feeds; a feed that is not in
    by then is listed in ``timed_out_sources`` (it keeps downloading for the
    next call), one served from an older copy also in ``stale_sources``.
    """
    now = now or datetime.now(UTC)
    budget = Budget(OFFICIAL_BUDGET_SECONDS if budget_seconds is None else budget_seconds)
    wanted = {normalize_team(t) for t in teams or [] if normalize_team(t)}
    result = {"games": {}, "window_teams": [], "inactives": [], "confirmed_active": [],
              "sources_checked": [], "sleeper_freshness": None,
              "partial": False, "timed_out_sources": [], "stale_sources": []}
    try:
        kickoffs = await asyncio.wait_for(_week_kickoffs(db, season, week, client),
                                          budget.remaining(cap=SCOREBOARD_TIMEOUT + 1))
    except TimeoutError:
        kickoffs = {}
        result["timed_out_sources"].append(SOURCE_SCOREBOARD)
    for team, kickoff in kickoffs.items():
        if wanted and team not in wanted:
            continue
        result["games"][team] = {"kickoff": kickoff, "phase": game_phase(kickoff, now)}
    window = {t for t, g in result["games"].items()
              if g["phase"] in ("inactives_window", "in_progress", "final")}
    result["window_teams"] = sorted(window)
    if window:
        wait = budget.remaining()
        (payload, _, notes_status), (players, freshness, sleeper_status) = await asyncio.gather(
            _cached_read("gameday_espn_notes", _espn_notes, ESPN_NOTES_TTL, ESPN_NOTES_STALE_MAX,
                         lambda: _download_espn_notes(client, now), now, wait),
            _sleeper_players(db, window, client, now, wait),
        )
        if payload is not None:
            try:
                inactive, active = parse_espn_gameday_notes(payload, kickoffs, window)
                result["inactives"].extend(inactive)
                result["confirmed_active"].extend(active)
                _note_source(result, SOURCE_ESPN_NOTE, notes_status)
            except Exception as e:
                logger.warning(f"[Inactives] ESPN notes failed: {e}")
        elif notes_status == "timeout":
            result["timed_out_sources"].append(SOURCE_ESPN_NOTE)
        result["sleeper_freshness"] = freshness
        if players is not None:
            seen = {(norm_name(r["player_name"]), r["team_id"]) for r in result["inactives"]}
            for row in parse_sleeper_inactives(players, window):
                if (norm_name(row["player_name"]), row["team_id"]) not in seen:
                    result["inactives"].append(row)
            _note_source(result, SOURCE_SLEEPER, sleeper_status)
        elif sleeper_status == "timeout":
            result["timed_out_sources"].append(SOURCE_SLEEPER)
    result["partial"] = bool(result["timed_out_sources"] or result["stale_sources"])
    return result


def _note_source(result: dict, source: str, status: str) -> None:
    """A fresh read counts as checked; a stale copy is listed as such (and the
    refresh it stood in for as timed out)."""
    if status == "fresh":
        result["sources_checked"].append(source)
    else:
        result["stale_sources"].append(source)
        result["timed_out_sources"].append(source)


# The projection path's read of the published inactives (`gameday_statuses`):
# cached per (season, week) so a tool that projects a whole league (waivers,
# the briefing) asks the feeds once, not once per player. Short: the list fills
# in team by team as each window opens.
GAMEDAY_CACHE_TTL = timedelta(minutes=5)
# An expired read is still served (while a refresh runs) up to this age; a
# partial one (a feed timed out) is refreshed after GAMEDAY_PARTIAL_TTL.
GAMEDAY_STALE_MAX = timedelta(minutes=30)
GAMEDAY_PARTIAL_TTL = timedelta(seconds=30)
# How long a projection waits for a cold read before going without it.
GAMEDAY_PROJECTION_WAIT = 3.0
# Phases in which a published decision prices the projection: from the
# inactives window to the end of the game (a locked lineup still feeds the live
# win probability). Upcoming and final games cost nothing to ask about.
PRICED_PHASES = ("inactives_window", "in_progress")
_gameday_cache: dict[tuple[int, int], tuple[datetime, dict]] = {}


def gameday_index(official: dict | None) -> dict[tuple[str, str], dict]:
    """``{(normalized name, team): {status, note, source, posted}}`` with
    ``status`` ``"active"`` / ``"inactive"`` from a `get_official_inactives`
    result. A player both confirmed active and listed inactive (two feeds that
    disagree) is inactive: the worse reading wins, as for injury reports."""
    index: dict[tuple[str, str], dict] = {}
    for key, rows in (("active", "confirmed_active"), ("inactive", "inactives")):
        for row in (official or {}).get(rows) or []:
            name, team = norm_name(row.get("player_name")), normalize_team(row.get("team_id"))
            if name and team:
                index[(name, team)] = {"status": key, "note": row.get("note"),
                                       "source": row.get("source"), "posted": row.get("posted")}
    return index


def priced_teams(db, season: int | None, week: int | None,
                 now: datetime | None = None) -> set[str]:
    """Teams whose game is in a `PRICED_PHASES` phase, from the *cached*
    schedule only (no network): outside a gameday window nothing is fetched."""
    if db is None or not season or not week or not hasattr(db, "get_week_kickoffs"):
        return set()
    try:
        kickoffs = db.get_week_kickoffs(int(season), int(week)) or {}
        if not isinstance(kickoffs, dict):
            return set()
        now = now or datetime.now(UTC)
        return {normalize_team(t) or t for t, k in kickoffs.items()
                if game_phase(k, now) in PRICED_PHASES}
    except Exception as e:  # context only; never sink a projection
        logger.debug(f"[Inactives] kickoff read failed: {e}")
        return set()


async def gameday_statuses(db, season: int | None, week: int | None,
                           now: datetime | None = None,
                           client=None) -> dict[tuple[str, str], dict]:
    """`gameday_index` of the published inactives for the games now in a
    `PRICED_PHASES` phase; ``{}`` when none is (the usual case: one cached
    schedule read, no network). Never raises, and never holds a projection up
    for long: a cold read waits ``GAMEDAY_PROJECTION_WAIT`` seconds, an
    expired one ``STALE_GRACE_SECONDS`` before serving the older copy; the
    read itself carries on in the background either way."""
    now = now or datetime.now(UTC)
    teams = priced_teams(db, season, week, now)
    if not teams:
        return {}
    key = (int(season), int(week))
    hit = _gameday_cache.get(key)
    age = now - hit[0] if hit else None
    if hit and timedelta(0) <= age < GAMEDAY_CACHE_TTL:
        official = hit[1]
    else:
        stale = hit[1] if hit and timedelta(0) <= age < GAMEDAY_STALE_MAX else None

        async def _refresh() -> dict:
            off = await get_official_inactives(db, key[0], key[1], now=now, client=client)
            # A partial read is kept briefly: the feeds still downloading land
            # in their own caches and the next refresh picks them up.
            stamp = (now - GAMEDAY_CACHE_TTL + GAMEDAY_PARTIAL_TTL
                     if (off or {}).get("partial") else now)
            _gameday_cache[key] = (stamp, off)
            return off

        try:
            task = single_flight(f"gameday_statuses:{key[0]}:{key[1]}", _refresh)
            done, official = await wait_bounded(
                task, STALE_GRACE_SECONDS if stale is not None else GAMEDAY_PROJECTION_WAIT)
        except Exception as e:
            logger.warning(f"[Inactives] gameday read for the projection failed: {e}")
            done, official = False, None
        if not done or official is None:
            if stale is None:
                return {}
            official = stale
    return {k: v for k, v in gameday_index(official).items() if k[1] in teams}
