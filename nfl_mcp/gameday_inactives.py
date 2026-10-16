"""Official gameday inactives, as far as the public feeds publish them.

Teams declare their inactives about 90 minutes before kickoff. Until then the
best available answer is the injury report's designations, which is what
``get_gameday_inactives`` used to return at all times — a severity filter over
the ESPN injury list, labelled as if it were the inactive list.

What the feeds carry, checked on 2026-09-23 against week 2 (and the game
rosters on 2026-10-16 against week 5):

- ESPN's game roster (core API, ``events/<id>/competitions/<id>/competitors/
  <team>/roster``): the team's full game-day roster with ``didNotPlay: true``
  on each declared inactive. It answers 404 until the roster is set and
  carries every inactive, not only the fantasy-relevant ones, so a team with
  it is *published* in full -- and everyone else on it is active. Week 5
  checked against the gameday notes: PIT Dowdle/Howard/Allar/Heidenreich, MIN
  Addison/Beckham, CIN C. Young, ATL Rush, BAL Vorhees all flagged; BAL L.
  Jackson, CHI Monangai among the rest. Primary, polled every few minutes in
  the inactives window by the prefetch (`warm_inactives`). When it first
  showed a team's list is logged and kept (``first_seen``): the feed carries
  no timestamp of its own.
- ESPN's game summary (``/summary?event=<id>``) has *no* inactives section —
  only the recap prose mentions them after the fact (its ``injuries`` block is
  the injury report).
- ESPN's league injury list carries the RotoWire notes posted at inactive time:
  "Goodson (coach's decision) is inactive for Sunday's game against the
  Commanders", "Seumalo (shoulder) is active for Sunday's game". Dated, per
  player, and covering every fantasy-relevant question mark -- but 15-30
  minutes after the list (week 5: the 17:00 UTC games' notes from 15:49 UTC).
  A team with notes only is published *in part*.
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
ESPN_GAME_ROSTER_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{event}"
    "/competitions/{event}/competitors/{team}/roster"
)

# Inactives are due 90 minutes before kickoff; the window opens a little
# earlier so an early list is not missed.
WINDOW_LEAD = timedelta(hours=2)
# How far before kickoff a note may be posted and still be about this game.
NOTE_LEAD = timedelta(hours=3)

SOURCE_ESPN_NOTE = "espn_gameday_note"
SOURCE_SLEEPER = "sleeper_injury_status"
SOURCE_ESPN_ROSTER = "espn_game_roster"
# Sources whose list is the team's complete inactive list.
COMPLETE_SOURCES = (SOURCE_ESPN_ROSTER,)

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
                "espn_id": str(athlete["id"]) if athlete.get("id") else None,
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
    _game_rosters.clear()
    _first_seen.clear()


# ESPN game rosters, per (event id, ESPN team id): (fetched_at, entries), with
# entries None while the feed answers 404 / lists nobody inactive (not set
# yet). A pending team is re-asked after ROSTER_PENDING_TTL; a published list
# after ROSTER_PUBLISHED_TTL (late changes are rare), once the game is under
# way only every ROSTER_SETTLED_TTL. An expired published copy is served,
# flagged stale, while the re-read runs.
ROSTER_TIMEOUT = 6.0
ROSTER_PENDING_TTL = timedelta(minutes=2)
ROSTER_PUBLISHED_TTL = timedelta(minutes=10)
ROSTER_SETTLED_TTL = timedelta(hours=6)
ROSTER_STALE_MAX = timedelta(hours=12)
_game_rosters: dict[tuple[str, str], tuple[datetime, list | None]] = {}
# When a team's list was first seen (the feed has no timestamp of its own).
_first_seen: dict[tuple[str, str], datetime] = {}


def parse_espn_game_roster(payload: dict | None) -> list[dict] | None:
    """``[{espn_id, name, jersey, inactive}]`` from an ESPN game roster, or
    None when it is not published yet (no entries, or nobody flagged
    ``didNotPlay``: every team declares inactives, so an all-active list is
    a roster set before the declaration)."""
    entries = (payload or {}).get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("playerId"):
            continue
        out.append({"espn_id": str(e["playerId"]), "name": (e.get("displayName") or "").strip(),
                    "jersey": e.get("jersey"), "inactive": e.get("didNotPlay") is True})
    return out if any(r["inactive"] for r in out) else None


def _espn_ids(db, season: int, week: int) -> dict[str, dict]:
    """``{team: {event_id, team_id}}`` from the cached schedule (or {})."""
    if db is None or not hasattr(db, "get_week_espn_ids"):
        return {}
    try:
        ids = db.get_week_espn_ids(season, week)
    except Exception as e:
        logger.debug(f"[Inactives] ESPN ids unavailable: {e}")
        return {}
    return {normalize_team(t) or t: v for t, v in ids.items()
            if isinstance(v, dict) and v.get("event_id") and v.get("team_id")} \
        if isinstance(ids, dict) else {}


async def _download_game_roster(client, key: tuple[str, str], team: str,
                                now: datetime) -> list | None:
    from .config import get_http_headers
    url = ESPN_GAME_ROSTER_URL.format(event=key[0], team=key[1])
    resp = await _get(client, url, get_http_headers("nfl_teams"), ROSTER_TIMEOUT)
    if resp.status_code == 404:
        _game_rosters[key] = (now, None)
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"ESPN game roster {team}: HTTP {resp.status_code}")
    entries = parse_espn_game_roster(resp.json())
    _game_rosters[key] = (now, entries)
    if entries is not None and key not in _first_seen:
        _first_seen[key] = now
        logger.info(f"[Inactives] ESPN game roster for {team} published "
                    f"({sum(r['inactive'] for r in entries)} inactive), first seen {now.isoformat()}")
    return entries


async def _game_roster(team: str, ids: dict, client, now: datetime, wait: float,
                       settled: bool) -> tuple[list | None, str]:
    """``(entries, status)`` for one team: ``published`` / ``pending`` (not
    set yet) / ``stale`` (an older published copy while the re-read runs) /
    ``timeout`` / ``failed``. Never raises."""
    key = (str(ids["event_id"]), str(ids["team_id"]))
    cached = _game_rosters.get(key)
    if cached is not None:
        age = now - cached[0]
        ttl = (ROSTER_PENDING_TTL if cached[1] is None
               else ROSTER_SETTLED_TTL if settled else ROSTER_PUBLISHED_TTL)
        if timedelta(0) <= age < ttl:
            return cached[1], "published" if cached[1] is not None else "pending"
    stale = (cached[1] if cached is not None and cached[1] is not None
             and timedelta(0) <= now - cached[0] < ROSTER_STALE_MAX else None)
    if inflight(f"gameday_roster:{key[0]}:{key[1]}"):
        wait = min(wait, INFLIGHT_WAIT_SECONDS)
    try:
        task = single_flight(f"gameday_roster:{key[0]}:{key[1]}",
                             lambda: _download_game_roster(client, key, team, now))
        done, value = await wait_bounded(task, min(wait, STALE_GRACE_SECONDS) if stale else wait)
    except Exception as e:
        logger.debug(f"[Inactives] ESPN game roster {team} failed: {e}")
        return (stale, "stale") if stale is not None else (None, "failed")
    if done:
        return value, "published" if value is not None else "pending"
    if stale is not None:
        return stale, "stale"
    return None, "timeout"


def roster_first_seen(event_id: str, team_id: str) -> datetime | None:
    """When this process first saw the team's game roster published."""
    return _first_seen.get((str(event_id), str(team_id)))


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
        stamps = [t for t in (parse_kickoff(r.get("updated_at")) for r in rows if isinstance(r, dict))
                  if t is not None]
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
    if cached and age is not None and timedelta(0) <= age < ttl:
        return cached[1], cached[0], "fresh"
    stale = cached if cached and age is not None and timedelta(0) <= age < stale_max else None
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


async def _week_kickoffs(db, season: int, week: int, client,
                         ids: dict[str, dict] | None = None) -> dict[str, str]:
    """``{team: kickoff}`` from the cached schedule, else ESPN's scoreboard
    (which also fills ``ids`` with the ESPN event / team ids)."""
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
                            team_id = (side.get("team") or {}).get("id")
                            if ids is not None and event.get("id") and team_id:
                                ids[team] = {"event_id": str(event["id"]), "team_id": str(team_id)}
    except Exception as e:
        logger.debug(f"[Inactives] scoreboard fetch failed: {e}")
    return kickoffs


async def get_official_inactives(db, season: int, week: int, teams: list[str] | None = None,
                                 now: datetime | None = None, client=None,
                                 budget_seconds: float | None = None) -> dict:
    """Published inactives for games whose inactive window is open.

    Returns ``{games: {team: {kickoff, phase}}, window_teams, inactives,
    confirmed_active, roster_active {team: [{player_id, player_name}]},
    published_by_source {source: [team]}, complete_teams, pending_roster_teams,
    sources_checked, sleeper_freshness, partial, timed_out_sources,
    stale_sources}``. ``complete_teams``: the team's whole list is out (ESPN
    game roster), so anyone of theirs not listed inactive is active. Fetches nothing beyond the schedule
    when no game is inside its window. Waits at most ``budget_seconds``
    (default ``OFFICIAL_BUDGET_SECONDS``) for the feeds; a feed that is not in
    by then is listed in ``timed_out_sources`` (it keeps downloading for the
    next call), one served from an older copy also in ``stale_sources``.
    """
    now = now or datetime.now(UTC)
    budget = Budget(OFFICIAL_BUDGET_SECONDS if budget_seconds is None else budget_seconds)
    wanted = {normalize_team(t) for t in teams or [] if normalize_team(t)}
    result: dict[str, Any] = {"games": {}, "window_teams": [], "inactives": [], "confirmed_active": [],
              "roster_active": {}, "published_by_source": {}, "complete_teams": [],
              "pending_roster_teams": [],
              "sources_checked": [], "sleeper_freshness": None,
              "partial": False, "timed_out_sources": [], "stale_sources": []}
    ids: dict[str, dict] = {}
    try:
        kickoffs = await asyncio.wait_for(_week_kickoffs(db, season, week, client, ids),
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
        ids = {**ids, **_espn_ids(db, season, week)}
        roster_teams = sorted(t for t in window if t in ids)
        (payload, _, notes_status), (players, freshness, sleeper_status), rosters = await asyncio.gather(
            _cached_read("gameday_espn_notes", _espn_notes, ESPN_NOTES_TTL, ESPN_NOTES_STALE_MAX,
                         lambda: _download_espn_notes(client, now), now, wait),
            _sleeper_players(db, window, client, now, wait),
            asyncio.gather(*(_game_roster(t, ids[t], client, now, wait,
                                          result["games"][t]["phase"] != "inactives_window")
                             for t in roster_teams)),
        )
        _apply_rosters(result, db, dict(zip(roster_teams, rosters, strict=True)), kickoffs, ids)
        if payload is not None:
            try:
                inactive, active = parse_espn_gameday_notes(payload, kickoffs, window)
                _merge_notes(result, inactive)
                result["confirmed_active"].extend(active)
                _note_source(result, SOURCE_ESPN_NOTE, notes_status)
                noted = sorted({r["team_id"] for r in inactive})
                if noted:
                    result["published_by_source"][SOURCE_ESPN_NOTE] = noted
            except Exception as e:
                logger.warning(f"[Inactives] ESPN notes failed: {e}")
        elif notes_status == "timeout":
            result["timed_out_sources"].append(SOURCE_ESPN_NOTE)
        result["sleeper_freshness"] = freshness
        if players is not None:
            seen = {(norm_name(r["player_name"]), r["team_id"]) for r in result["inactives"]}
            flagged = set()
            for row in parse_sleeper_inactives(players, window):
                flagged.add(row["team_id"])
                if (norm_name(row["player_name"]), row["team_id"]) not in seen:
                    result["inactives"].append(row)
            if flagged:
                result["published_by_source"][SOURCE_SLEEPER] = sorted(flagged)
            _note_source(result, SOURCE_SLEEPER, sleeper_status)
        elif sleeper_status == "timeout":
            result["timed_out_sources"].append(SOURCE_SLEEPER)
    result["partial"] = bool(result["timed_out_sources"] or result["stale_sources"])
    return result


def _apply_rosters(result: dict, db, rosters: dict[str, tuple[list | None, str]],
                   kickoffs: dict[str, str], ids: dict[str, dict]) -> None:
    """Fold the ESPN game rosters into ``result``: every inactive (named via
    Sleeper's ``espn_id`` where the athlete is known, else ESPN's short
    name), the active players per team, and which teams are complete."""
    if not rosters:
        return
    wanted = [r["espn_id"] for entries, _ in rosters.values() for r in entries or []]
    by_espn: dict = {}
    if wanted and db is not None and hasattr(db, "get_athletes_by_espn_ids"):
        try:
            got = db.get_athletes_by_espn_ids(wanted)
            by_espn = got if isinstance(got, dict) else {}
        except Exception as e:
            logger.debug(f"[Inactives] ESPN id lookup failed: {e}")
    complete, pending, timed_out, stale, fresh = [], [], [], [], False
    for team, (entries, status) in sorted(rosters.items()):
        if status in ("published", "pending"):
            fresh = True
        if status == "pending":
            pending.append(team)
        elif status == "timeout":
            timed_out.append(team)
        elif status == "stale":
            stale.append(team)
        if entries is None:
            continue
        complete.append(team)
        seen_at = roster_first_seen(ids[team]["event_id"], ids[team]["team_id"])
        active = []
        # Sleeper carries an espn_id for about half its players; the rest are
        # matched on the team by jersey number and last name.
        local = _team_index(db, team) if any(r["espn_id"] not in by_espn for r in entries) else None
        for r in entries:
            athlete = by_espn.get(r["espn_id"]) or (_match_local(r, local) if local else None) or {}
            name = athlete.get("full_name") or r["name"]
            if not r["inactive"]:
                active.append({"player_id": athlete.get("id"), "player_name": name})
                continue
            result["inactives"].append({
                "player_id": athlete.get("id"), "player_name": name, "team_id": team,
                "position": athlete.get("position"), "status": "Inactive", "note": None,
                "posted": seen_at.isoformat() if seen_at else None,
                "kickoff": kickoffs.get(team), "source": SOURCE_ESPN_ROSTER, "official": True,
                "espn_id": r["espn_id"],
                **({} if athlete else {"name_partial": True}),
            })
        result["roster_active"][team] = active
    if complete:
        result["published_by_source"][SOURCE_ESPN_ROSTER] = complete
    result["complete_teams"] = complete
    result["pending_roster_teams"] = pending
    if fresh:
        result["sources_checked"].append(SOURCE_ESPN_ROSTER)
    if stale:
        result["stale_sources"].append(SOURCE_ESPN_ROSTER)
    if timed_out or stale:
        result["timed_out_sources"].append(SOURCE_ESPN_ROSTER)


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _last_name(name: str | None) -> str:
    """``beckham`` for "Beckham Jr." / "Odell Beckham Jr."; "" when empty."""
    tokens = [t for t in re.sub(r"[^a-z\s-]", "", (name or "").lower()).split() if t]
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return tokens[-1] if tokens else ""


def _team_index(db, team: str) -> dict:
    """``{"jersey": {number: [row]}, "last": {last name: [row]}}`` of the
    stored athletes on a team (Sleeper's ``number`` / ``last_name``)."""
    import json
    index: dict[str, dict[str, list]] = {"jersey": {}, "last": {}}
    try:
        rows = db.get_athletes_by_team(team) if db is not None and hasattr(db, "get_athletes_by_team") else []
    except Exception:
        rows = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        raw = row.get("raw")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = None
        raw = raw if isinstance(raw, dict) else {}
        number = raw.get("number")
        if number is not None and str(number).strip():
            index["jersey"].setdefault(str(number).strip(), []).append(row)
        last = _last_name(raw.get("last_name") or row.get("full_name"))
        if last:
            index["last"].setdefault(last, []).append(row)
    return index


def _match_local(entry: dict, index: dict) -> dict | None:
    """The team's athlete for an ESPN roster entry: same jersey and last name,
    else the only player with that last name."""
    last = _last_name(entry.get("name"))
    jersey = str(entry.get("jersey") or "").strip()
    same_jersey = [r for r in index["jersey"].get(jersey, [])
                   if not last or _last_name(r.get("full_name")) == last]
    if len(same_jersey) == 1:
        return same_jersey[0]
    by_last = index["last"].get(last, []) if last else []
    return by_last[0] if len(by_last) == 1 else None


def _merge_notes(result: dict, notes: list[dict]) -> None:
    """Gameday notes onto the roster rows (the note text joins the row it is
    about, by ESPN id or name), the rest as rows of their own."""
    by_id = {r.get("espn_id"): r for r in result["inactives"] if r.get("espn_id")}
    by_name = {(norm_name(r["player_name"]), r["team_id"]): r for r in result["inactives"]}
    for note in notes:
        hit = by_id.get(note.get("espn_id")) or by_name.get((norm_name(note["player_name"]),
                                                              note["team_id"]))
        if hit is not None:
            hit["note"] = note.get("note")
            if hit.get("name_partial"):
                hit["player_name"] = note["player_name"]
                hit["position"] = hit.get("position") or note.get("position")
                hit.pop("name_partial", None)
            hit.setdefault("also_in", []).append(SOURCE_ESPN_NOTE)
            continue
        result["inactives"].append(note)


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
    # Everyone on a published game roster who is not inactive is active.
    for team, rows_ in ((official or {}).get("roster_active") or {}).items():
        canon = normalize_team(team)
        for row in rows_ or []:
            name = norm_name(row.get("player_name"))
            if name and canon:
                index[(name, canon)] = {"status": "active", "note": None,
                                        "source": SOURCE_ESPN_ROSTER, "posted": None}
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
    assert season is not None and week is not None  # priced_teams is empty without them
    key = (int(season), int(week))
    hit = _gameday_cache.get(key)
    age = now - hit[0] if hit else None
    if hit and age is not None and timedelta(0) <= age < GAMEDAY_CACHE_TTL:
        official = hit[1]
    else:
        stale = hit[1] if hit and age is not None and timedelta(0) <= age < GAMEDAY_STALE_MAX else None

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


# The prefetch polls the published lists this often while a window is open,
# so the cache is warm when a tool asks (`server._prefetch_loop`).
WARM_POLL_SECONDS = 180
# From this long before a kickoff (the list is due at T-90).
WARM_LEAD = timedelta(minutes=100)


def warm_due(db, season: int | None, week: int | None, now: datetime | None = None) -> bool:
    """Whether any game of the week is between ``WARM_LEAD`` before its
    kickoff and the kickoff (cached schedule only, no network)."""
    if db is None or not season or not week or not hasattr(db, "get_week_kickoffs"):
        return False
    now = now or datetime.now(UTC)
    try:
        kickoffs = db.get_week_kickoffs(int(season), int(week)) or {}
    except Exception:
        return False
    if not isinstance(kickoffs, dict):
        return False
    for k in kickoffs.values():
        start = parse_kickoff(k)
        if start is not None and start - WARM_LEAD <= now < start:
            return True
    return False


def next_warm_at(db, season: int | None, week: int | None,
                 now: datetime | None = None) -> datetime | None:
    """When the next poll window opens (None: no upcoming game cached)."""
    if db is None or not season or not week or not hasattr(db, "get_week_kickoffs"):
        return None
    now = now or datetime.now(UTC)
    try:
        kickoffs = db.get_week_kickoffs(int(season), int(week)) or {}
    except Exception:
        return None
    starts = [s - WARM_LEAD for k in (kickoffs.values() if isinstance(kickoffs, dict) else [])
              if (s := parse_kickoff(k)) is not None and s - WARM_LEAD > now]
    return min(starts) if starts else None


async def warm_inactives(db, season: int, week: int, now: datetime | None = None) -> dict | None:
    """One poll of the published inactives for the prefetch: fills the feed
    caches and the projection path's (`gameday_statuses`) read. None when no
    window is open. Never raises."""
    now = now or datetime.now(UTC)
    if not warm_due(db, season, week, now):
        return None
    try:
        official = await get_official_inactives(db, int(season), int(week), now=now)
    except Exception as e:
        logger.warning(f"[Inactives] warm poll failed: {e}")
        return None
    stamp = now - GAMEDAY_CACHE_TTL + GAMEDAY_PARTIAL_TTL if official.get("partial") else now
    _gameday_cache[(int(season), int(week))] = (stamp, official)
    window = official.get("window_teams") or []
    logger.info(
        f"[Inactives] warm poll: {len(official.get('complete_teams') or [])}/{len(window)} window "
        f"teams with the full list (pending: {', '.join(official.get('pending_roster_teams') or []) or '-'})"
        + (f", timed out: {official['timed_out_sources']}" if official.get("timed_out_sources") else ""))
    return official
