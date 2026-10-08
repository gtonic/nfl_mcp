"""Per-player news from several sources, for the news flags (`news_signals`).

The injury table keeps one ESPN blurb per player, replaced by the next one:
the role and availability notes in between ("benched after his fumble",
"only an outside chance to play ... could miss multiple games") were
overwritten by a practice note before anything read them. This module polls
three sources and keeps every item in ``player_news`` (schema v19), mapped to
the Sleeper player id:

``espn_fantasy``
    ESPN's fantasy player-news feed (JSON,
    ``site.api.espn.com/apis/fantasy/v2/games/ffl/news/players``): the
    RotoWire blurb (headline + analysis) for each player, queried by ESPN
    athlete id, ``ESPN_BATCH`` ids per request (repeated ``playerId``). The
    ids come from ESPN's 32 team rosters (cached ``ROSTER_TTL_SECONDS``),
    offense, kickers and the injured list; each maps to the Sleeper id through
    Sleeper's ``espn_id`` or, failing that, name + team + position. The
    backbone: every fantasy-relevant player, with history.
``nbc``
    NBC Sports / Rotoworld player news (HTML, ``nbcsports.com/fantasy/football/
    player-news?p=N``): independent writers, absolute timestamps, team and
    position on every item. robots.txt asks for ``Crawl-delay: 10``; the
    ``nbc`` limiter allows 6 requests a minute with no burst, and a poll
    reads only the pages newer than the last one seen (``NBC_MAX_PAGES``).
``cbs``
    CBS Sports fantasy player news (HTML, first page only -- deeper pages and
    the per-position pages answer 406 to non-browser clients): ten RotoWire
    items with team and position. Cheap (one request); its items are mostly
    the ESPN ones again and merge as near-duplicates, but it carries players
    outside the ESPN roster query (IDP, practice squad).

Considered and not used: ESPN's league news (`get_nfl_news`; articles and
videos, not player notes), the per-athlete ``core`` notes endpoint (404),
RotoWire's RSS (five items; the same text as the ESPN feed), FantasyPros (no
news RSS; ``/api/`` disallowed), Sleeper (no player-news endpoint).

Player mapping (`resolve_player`): an exact normalized name on the stated
team (`lineup_tools.name_candidates`), the position breaking ties; a
same-name player at another position is never taken ("Justin Jefferson", the
CLE linebacker, is not the MIN receiver). A player off the stated team is
taken only when the name and position are unique league-wide (a trade the
athlete cache has not seen yet). Unresolved items are counted, not stored.

Items are deduplicated per source on content (`content_hash`: source,
player, normalized text); across sources they are merged at read time
(`merge_timeline`). `ingest_news` is the ``data_refresh`` scope ``news``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta

from .teams import normalize_team

logger = logging.getLogger(__name__)

SOURCES = ("espn_fantasy", "nbc", "cbs")
SOURCE_LABELS = {
    "espn_fantasy": "ESPN fantasy news (RotoWire)",
    "nbc": "NBC Sports / Rotoworld",
    "cbs": "CBS Sports (RotoWire)",
    "injury_report": "ESPN injury report blurb",
}

# How far back an ingest keeps items (and the first poll reaches).
LOOKBACK_DAYS = 14

# --- ESPN ---------------------------------------------------------------
ESPN_NEWS_URL = "https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players"
ESPN_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team}/roster"
# Ids per request. The feed is sorted newest first across the batch, so the
# limit is per batch: ESPN_ITEMS_PER_PLAYER on the first poll (two weeks of
# a busy player's notes), ESPN_ITEMS_PER_PLAYER_INCREMENTAL after.
ESPN_BATCH = 20
ESPN_ITEMS_PER_PLAYER = 15
ESPN_ITEMS_PER_PLAYER_INCREMENTAL = 5
# Concurrent ESPN requests (the `espn` limiter paces them, 120/min).
ESPN_CONCURRENCY = 4
ROSTER_TTL_SECONDS = 12 * 3600
# ESPN roster groups and positions worth news (fantasy-relevant).
ESPN_ROSTER_GROUPS = ("offense", "specialTeam", "injuredReserveOrOut", "suspended")
ESPN_POSITIONS = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE", "PK": "K",
                  "K": "K"}
# Feed item types that are a note about the player (not an article tagged
# with him, e.g. a rankings column that names twenty players).
ESPN_NOTE_TYPES = {"Rotowire"}

# --- NBC ----------------------------------------------------------------
NBC_NEWS_URL = "https://www.nbcsports.com/fantasy/football/player-news"
# Pages per poll (10 items each, ~50 a day in season), and on the first poll.
NBC_MAX_PAGES = 3
NBC_FIRST_PAGES = 12

# --- CBS ----------------------------------------------------------------
CBS_NEWS_URL = "https://www.cbssports.com/fantasy/football/players/news/all/"
CBS_BASE = "https://www.cbssports.com"

# Source position labels -> a group, for "same position" checks.
_POSITION_WORDS = {
    "quarterback": "QB", "running back": "RB", "fullback": "RB", "wide receiver": "WR",
    "tight end": "TE", "kicker": "K", "place kicker": "K", "punter": "P",
    "linebacker": "LB", "inside linebacker": "LB", "outside linebacker": "LB",
    "cornerback": "DB", "safety": "DB", "defensive back": "DB",
    "defensive end": "DL", "defensive tackle": "DL", "defensive lineman": "DL",
    "nose tackle": "DL", "edge": "DL", "tackle": "OL", "guard": "OL", "center": "OL",
    "offensive tackle": "OL", "offensive guard": "OL", "offensive lineman": "OL",
    "long snapper": "LS",
}
_POSITION_GROUP = {
    "QB": "QB", "RB": "RB", "FB": "RB", "HB": "RB", "WR": "WR", "TE": "TE", "K": "K", "PK": "K",
    "P": "P", "LS": "LS", "DEF": "DEF", "DST": "DEF",
    "LB": "LB", "ILB": "LB", "OLB": "LB", "MLB": "LB",
    "DB": "DB", "CB": "DB", "S": "DB", "SS": "DB", "FS": "DB",
    "DL": "DL", "DE": "DL", "DT": "DL", "NT": "DL", "EDGE": "DL",
    "OL": "OL", "OT": "OL", "OG": "OL", "C": "OL", "G": "OL", "T": "OL",
}

_roster_cache: dict = {"at": 0.0, "players": [], "fresh_fetch": False}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default


def enabled_sources() -> tuple[str, ...]:
    """``NFL_MCP_NEWS_SOURCES`` (comma list), else every source."""
    raw = os.getenv("NFL_MCP_NEWS_SOURCES", "")
    wanted = tuple(s.strip().lower() for s in raw.split(",") if s.strip())
    return tuple(s for s in SOURCES if s in wanted) if wanted else SOURCES


def position_group(position: str | None) -> str | None:
    """A source's position (``WR``, ``Wide Receiver``, ``PK``) as a group code."""
    if not position:
        return None
    text = " ".join(str(position).replace("|", " ").split()).strip()
    if text.lower() in _POSITION_WORDS:
        return _POSITION_WORDS[text.lower()]
    return _POSITION_GROUP.get(text.upper())


def clean_text(text: str | None) -> str:
    """Whitespace collapsed, typographic apostrophes and quotes made plain."""
    text = (text or "").replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"').replace("\xa0", " ")
    return " ".join(text.split())


def normalized(text: str | None) -> str:
    """Lower-case words only: the content key for dedupe."""
    return " ".join(re.findall(r"[a-z0-9]+", clean_text(text).lower()))


def content_hash(source: str, player_id: str, headline: str | None, text: str | None) -> str:
    """The per-source dedupe key: the same note on every poll is one row."""
    body = normalized(f"{headline or ''} {text or ''}")
    return hashlib.sha1(f"{source}\x1f{player_id}\x1f{body}".encode()).hexdigest()


def item_text(item: dict) -> str:
    """Headline and body as one text (the body alone when it repeats the headline)."""
    head = clean_text(item.get("headline"))
    body = clean_text(item.get("text"))
    if not head or normalized(body).startswith(normalized(head)):
        return body or head
    if not body:
        return head
    return f"{head}{'' if head.endswith(('.', '!', '?')) else '.'} {body}"


def _iso(value) -> str | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Parsers (pure; the fixtures in tests are these payloads)
# ---------------------------------------------------------------------------

def parse_espn_roster(payload: dict | None) -> list[dict]:
    """``[{espn_id, name, team, position}]`` for one ESPN team roster."""
    payload = payload or {}
    team = normalize_team((payload.get("team") or {}).get("abbreviation")) or ""
    out = []
    for group in payload.get("athletes") or []:
        if group.get("position") not in ESPN_ROSTER_GROUPS:
            continue
        for a in group.get("items") or []:
            pos = ESPN_POSITIONS.get(((a.get("position") or {}).get("abbreviation") or "").upper())
            if not pos or not a.get("id"):
                continue
            out.append({"espn_id": str(a["id"]), "name": clean_text(a.get("fullName")),
                        "team": team, "position": pos})
    return out


def parse_espn_news(payload: dict | None) -> list[dict]:
    """Raw items from ESPN's fantasy player-news feed (notes only)."""
    out = []
    for x in (payload or {}).get("feed") or []:
        if x.get("type") not in ESPN_NOTE_TYPES or not x.get("playerId"):
            continue
        links = x.get("links") or {}
        url = ((links.get("web") or {}).get("href")
               or f"https://www.espn.com/nfl/player/_/id/{x['playerId']}")
        out.append({
            "source": "espn_fantasy", "source_id": str(x.get("id") or ""),
            "espn_id": str(x["playerId"]),
            "headline": clean_text(x.get("headline") or x.get("description")),
            "text": clean_text(x.get("story")),
            "url": url, "published_at": _iso(x.get("published") or x.get("lastModified")),
        })
    return out


def parse_nbc(html: str | None) -> list[dict]:
    """Raw items from an NBC Sports / Rotoworld player-news page."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for post in soup.select("li.PlayerNewsModuleList-item div.PlayerNewsPost"):
        first = post.select_one(".PlayerNewsPost-firstName")
        last = post.select_one(".PlayerNewsPost-lastName")
        name = clean_text(" ".join(t.get_text(" ", strip=True) for t in (first, last) if t))
        if not name:
            link = post.select_one(".PlayerNewsPost-name a")
            name = clean_text(link.get_text(" ", strip=True)) if link else ""
        headline = post.select_one(".PlayerNewsPost-headline")
        analysis = post.select_one(".PlayerNewsPost-analysis")
        if analysis is not None:
            for author in analysis.select(".PlayerNewsPost-author"):
                author.decompose()
        date = post.select_one(".PlayerNewsPost-date")
        share = post.select_one("[data-share-url]")
        team = post.select_one(".PlayerNewsPost-team-abbr")
        pos = post.select_one(".PlayerNewsPost-position")
        if not name or headline is None:
            continue
        url = share.get("data-share-url") if share else None
        out.append({
            "source": "nbc", "source_id": (url or "").rstrip("/").rsplit("/", 1)[-1] or None,
            "name": name,
            "team": normalize_team(team.get_text(strip=True)) if team else None,
            "position": pos.get_text(strip=True) if pos else None,
            "headline": clean_text(headline.get_text(" ", strip=True)),
            "text": clean_text(analysis.get_text(" ", strip=True)) if analysis else "",
            "url": url or NBC_NEWS_URL,
            "published_at": _iso(date.get("data-date")) if date else None,
        })
    return out


_RELATIVE_RE = re.compile(r"(\d+)\s*([MHD])\w*\s+ago", re.I)


def _cbs_time(text: str | None, now: datetime) -> str | None:
    """CBS's "7M ago" / "2H ago" / "1D ago" as an ISO time (approximate)."""
    m = _RELATIVE_RE.search(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).upper()
    delta = {"M": timedelta(minutes=n), "H": timedelta(hours=n), "D": timedelta(days=n)}[unit]
    return (now - delta).astimezone(UTC).isoformat(timespec="seconds")


def parse_cbs(html: str | None, now: datetime | None = None) -> list[dict]:
    """Raw items from CBS's fantasy player-news page (``ul.player-news-by-sport``)."""
    from bs4 import BeautifulSoup
    now = now or datetime.now(UTC)
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for li in soup.select("ul.player-news-by-sport > li"):
        who = li.select_one(".players-annotated p")
        link = who.find("a") if who else None
        label = who.find("span") if who else None
        desc = li.select_one(".player-news-desc")
        if not link or desc is None:
            continue
        pos, _, team = (label.get_text(" ", strip=True) if label else "").partition("|")
        title = desc.select_one("h4 a") or desc.select_one("h4")
        paragraphs = [clean_text(p.get_text(" ", strip=True))
                      for p in desc.select(".latest-updates p")]
        paragraphs = [p for p in paragraphs if p]
        href = title.get("href") if title is not None and title.name == "a" else None
        out.append({
            "source": "cbs", "source_id": (href or "").strip("/").rsplit("/", 1)[-1] or None,
            "name": clean_text(link.get_text(" ", strip=True)),
            "team": normalize_team(team.strip()) if team.strip() else None,
            "position": pos.strip() or None,
            "title": clean_text(title.get_text(" ", strip=True)) if title is not None else None,
            "headline": paragraphs[0] if paragraphs else
            (clean_text(title.get_text(" ", strip=True)) if title is not None else ""),
            "text": " ".join(paragraphs[1:]),
            "url": (CBS_BASE + href) if href and href.startswith("/") else (href or CBS_NEWS_URL),
            "published_at": _cbs_time((desc.find("time") or li).get_text(" ", strip=True), now),
        })
    return out


# ---------------------------------------------------------------------------
# Player mapping
# ---------------------------------------------------------------------------

_SUFFIX_TAIL_RE = re.compile(r"(?:[\s,]+(?:jr|sr|ii|iii|iv|v)\.?)+$", re.I)


def _exact_candidates(db, name: str, team: str | None) -> list[dict]:
    """Athlete rows whose normalized name is exactly `name`.

    `lineup_tools.name_candidates` on the name without its suffix (Sleeper
    says "Travis Etienne", ESPN "Travis Etienne Jr."), then -- for spellings
    a substring search misses ("DJ Moore" / "D.J. Moore") -- the last name.
    """
    from .lineup_tools import name_candidates
    from .opportunity_tools import norm_name
    wanted = norm_name(name)
    bare = _SUFFIX_TAIL_RE.sub("", name).strip() or name
    try:
        ranked, _ = name_candidates(db, bare, team, None, include_free_agents=True)
    except Exception as e:  # never sink an ingest on one name
        logger.debug(f"news: name lookup failed for {name!r}: {e}")
        ranked = []
    exact = [r for r in ranked if norm_name(r.get("full_name")) == wanted]
    if exact or not hasattr(db, "search_athletes_by_name"):
        return exact
    last = bare.split()[-1] if bare.split() else ""
    if len(last) < 3:
        return []
    try:
        rows = db.search_athletes_by_name(last, limit=400) or []
    except Exception as e:
        logger.debug(f"news: last-name lookup failed for {name!r}: {e}")
        return []
    return [r for r in rows if norm_name(r.get("full_name")) == wanted]


def resolve_player(db, name: str | None, team: str | None = None,
                   position: str | None = None, cache: dict | None = None) -> dict | None:
    """The athlete row a news item is about, or None when unsure.

    Exact normalized name only. With a team: the one exact-name player on it
    (several: the position decides). Off the stated team, or with none: only
    a name + position unique league-wide -- a same-name player at another
    position is never taken (Justin Jefferson the CLE linebacker is not the
    MIN receiver). See the module doc.
    """
    from .opportunity_tools import norm_name
    name = clean_text(name)
    if db is None or not name:
        return None
    team = normalize_team(team) if team else None
    group = position_group(position)
    key = (norm_name(name), team, group)
    if cache is not None and key in cache:
        return cache[key]
    exact = _exact_candidates(db, name, team)

    def fits(r: dict) -> bool:
        return group is None or position_group(r.get("position")) == group

    found = None
    on_team = [r for r in exact if team and normalize_team(r.get("team_id")) == team]
    if len(on_team) == 1:
        found = on_team[0]
    elif len(on_team) > 1:
        fit = [r for r in on_team if fits(r)] if group else []
        found = fit[0] if len(fit) == 1 else None
    elif group is not None:
        anywhere = [r for r in exact if fits(r)]
        found = anywhere[0] if len(anywhere) == 1 else None
    elif not team and len(exact) == 1:
        found = exact[0]
    if cache is not None:
        cache[key] = found
    return found


def _stored_row(item: dict, athlete: dict, now: str) -> dict:
    from .opportunity_tools import norm_name
    pid = str(athlete["id"])
    return {
        "content_hash": content_hash(item["source"], pid, item.get("headline"), item.get("text")),
        "source": item["source"], "source_id": item.get("source_id"),
        "player_id": pid, "espn_id": item.get("espn_id"),
        "player_name": athlete.get("full_name") or item.get("name"),
        "name_key": norm_name(athlete.get("full_name") or item.get("name")),
        "team": normalize_team(athlete.get("team_id")) or normalize_team(item.get("team")) or "",
        "position": (athlete.get("position") or "").upper() or None,
        "headline": item.get("headline"), "text": item.get("text") or item.get("headline") or "",
        "url": item.get("url"), "published_at": item.get("published_at"), "recorded_at": now,
    }


def map_items(db, items: list[dict], roster: dict[str, dict] | None = None,
              now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """``(rows to store, unresolved items)``.

    ESPN items map by Sleeper's ``espn_id`` first, then by the ESPN roster's
    name / team / position; the others by name / team / position.
    """
    stamp = (now or datetime.now(UTC)).isoformat()
    roster = roster or {}
    espn_ids = sorted({i["espn_id"] for i in items if i.get("espn_id")})
    by_espn = {}
    if espn_ids and hasattr(db, "get_athletes_by_espn_ids"):
        try:
            by_espn = db.get_athletes_by_espn_ids(espn_ids) or {}
        except Exception as e:
            logger.debug(f"news: espn id join failed: {e}")
    cache: dict = {}
    rows, unresolved = [], []
    for it in items:
        athlete = None
        if it.get("espn_id"):
            athlete = by_espn.get(it["espn_id"])
            info = roster.get(it["espn_id"]) or {}
            if athlete is None and info:
                athlete = resolve_player(db, info.get("name"), info.get("team"),
                                         info.get("position"), cache)
            it = {**it, "name": it.get("name") or info.get("name"),
                  "team": it.get("team") or info.get("team")}
        else:
            athlete = resolve_player(db, it.get("name"), it.get("team"), it.get("position"), cache)
        if not athlete or not athlete.get("id") or not (it.get("headline") or it.get("text")):
            unresolved.append(it)
            continue
        rows.append(_stored_row(it, athlete, stamp))
    return rows, unresolved


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def _client():
    from .config import LONG_TIMEOUT, create_http_client
    return create_http_client(timeout=LONG_TIMEOUT)


def _headers() -> dict:
    from .config import get_http_headers
    return get_http_headers("player_news")


async def espn_roster(client, force: bool = False) -> list[dict]:
    """Every team's fantasy-relevant ESPN athletes (cached ``ROSTER_TTL_SECONDS``)."""
    from .teams import CANONICAL_TEAMS
    if (not force and _roster_cache["players"]
            and time.monotonic() - _roster_cache["at"] < ROSTER_TTL_SECONDS):
        _roster_cache["fresh_fetch"] = False
        return _roster_cache["players"]
    sem = asyncio.Semaphore(ESPN_CONCURRENCY)

    async def one(team: str) -> list[dict]:
        async with sem:
            try:
                r = await client.get(ESPN_ROSTER_URL.format(team=team), headers=_headers())
                r.raise_for_status()
                return parse_espn_roster(r.json())
            except Exception as e:
                logger.debug(f"news: ESPN roster {team} failed: {e}")
                return []

    players = [p for rows in await asyncio.gather(*(one(t) for t in sorted(CANONICAL_TEAMS)))
               for p in rows]
    if players:
        _roster_cache.update(at=time.monotonic(), players=players)
    _roster_cache["fresh_fetch"] = True
    return players


def free_agents_with_news(db, since: datetime) -> list[dict]:
    """``[{espn_id, name, team: None, position}]``: fantasy players without an
    NFL team whose Sleeper ``news_updated`` is after `since` (a released
    veteran still rostered in leagues -- Tyreek Hill). ESPN's team rosters do
    not list them."""
    import json
    if db is None or not hasattr(db, "get_athletes_by_positions"):
        return []
    cutoff_ms = since.timestamp() * 1000
    out = []
    try:
        rows = db.get_athletes_by_positions(["QB", "RB", "WR", "TE", "K"])
    except Exception as e:
        logger.debug(f"news: free-agent lookup failed: {e}")
        return []
    for r in rows:
        if r.get("team_id"):
            continue
        try:
            raw = json.loads(r.get("raw") or "{}") if isinstance(r.get("raw"), str) else (r.get("raw") or {})
            if raw.get("espn_id") and float(raw.get("news_updated") or 0) >= cutoff_ms:
                out.append({"espn_id": str(raw["espn_id"]), "name": r.get("full_name"),
                            "team": None, "position": r.get("position")})
        except (TypeError, ValueError):
            continue
    return out


async def fetch_espn(client, db, first: bool, since: datetime) -> tuple[list[dict], dict]:
    """ESPN fantasy notes for every roster player (and free agents with
    recent news), newer than `since`."""
    from .teams import CANONICAL_TEAMS
    roster = {p["espn_id"]: p for p in await espn_roster(client)}
    free_agents = {p["espn_id"]: p for p in free_agents_with_news(db, since)
                   if p["espn_id"] not in roster}
    roster.update(free_agents)
    ids = sorted(roster)
    per = ESPN_ITEMS_PER_PLAYER if first else ESPN_ITEMS_PER_PLAYER_INCREMENTAL
    sem = asyncio.Semaphore(ESPN_CONCURRENCY)
    errors: list[str] = []

    async def batch(chunk: list[str]) -> list[dict]:
        params = [("limit", str(len(chunk) * per))] + [("playerId", i) for i in chunk]
        async with sem:
            try:
                r = await client.get(ESPN_NEWS_URL, params=params, headers=_headers())
                r.raise_for_status()
                return parse_espn_news(r.json())
            except Exception as e:
                errors.append(str(e))
                return []

    chunks = [ids[i:i + ESPN_BATCH] for i in range(0, len(ids), ESPN_BATCH)]
    items = [it for got in await asyncio.gather(*(batch(c) for c in chunks)) for it in got]
    cutoff = since.isoformat()
    items = [it for it in items if (it.get("published_at") or cutoff) >= cutoff]
    if chunks and len(errors) == len(chunks):
        raise RuntimeError(f"every ESPN news request failed: {errors[0]}")
    fetched_roster = bool(_roster_cache.get("fresh_fetch"))
    return items, {"roster_players": len(ids) - len(free_agents),
                   "free_agents": len(free_agents),
                   "requests": len(chunks) + (len(CANONICAL_TEAMS) if fetched_roster else 0),
                   "failed_requests": len(errors)}


async def fetch_nbc(client, newest_seen: str | None, since: datetime,
                    max_pages: int | None = None) -> tuple[list[dict], dict]:
    """NBC pages, newest first, until a page reaches `newest_seen` / `since`."""
    pages = max_pages or (_env_int("NFL_MCP_NEWS_NBC_PAGES", NBC_MAX_PAGES) if newest_seen
                          else _env_int("NFL_MCP_NEWS_NBC_FIRST_PAGES", NBC_FIRST_PAGES))
    floor = max(newest_seen or "", since.isoformat())
    items: list[dict] = []
    read = 0
    for page in range(1, pages + 1):
        url = NBC_NEWS_URL if page == 1 else f"{NBC_NEWS_URL}?p={page}"
        r = await client.get(url, headers=_headers())
        if page > 1 and r.status_code >= 400:
            break
        r.raise_for_status()
        read += 1
        got = parse_nbc(r.text)
        items += got
        dated = [i["published_at"] for i in got if i.get("published_at")]
        if not got or (dated and min(dated) < floor):
            break
    cutoff = since.isoformat()
    return ([i for i in items if (i.get("published_at") or cutoff) >= cutoff],
            {"pages": read, "requests": read})


async def fetch_cbs(client, now: datetime) -> tuple[list[dict], dict]:
    """CBS's first page (see the module doc)."""
    r = await client.get(CBS_NEWS_URL, headers=_headers())
    r.raise_for_status()
    return parse_cbs(r.text, now), {"pages": 1, "requests": 1}


def _newest(rows: list[dict]) -> str | None:
    return max((r["published_at"] for r in rows if r.get("published_at")), default=None)


async def ingest_news(db, sources: list[str] | tuple[str, ...] | None = None,
                      days: int = LOOKBACK_DAYS, now: datetime | None = None,
                      client=None) -> dict:
    """Poll the sources, map the items to players, store the new ones.

    ``{fetched, written, unresolved, sources: {source: {fetched, resolved,
    unresolved, written, newest_published, duration_s, status, error?}}}``.
    A failing source is reported and recorded; the others still run.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    wanted = [s for s in (sources or enabled_sources()) if s in SOURCES]
    state = db.get_news_fetch_state() if hasattr(db, "get_news_fetch_state") else {}
    own = client is None
    client = client or _client()
    try:
        async def run(source: str) -> tuple[str, dict]:
            started = time.monotonic()
            seen = (state.get(source) or {}).get("newest_published")
            try:
                if source == "espn_fantasy":
                    items, info = await fetch_espn(client, db, not seen, since)
                    roster = {p["espn_id"]: p for p in [*_roster_cache["players"],
                                                        *free_agents_with_news(db, since)]}
                elif source == "nbc":
                    items, info = await fetch_nbc(client, seen, since)
                    roster = None
                else:
                    items, info = await fetch_cbs(client, now)
                    roster = None
                rows, unresolved = await asyncio.to_thread(map_items, db, items, roster, now)
                written = await asyncio.to_thread(db.upsert_player_news, rows)
                newest = _newest(rows)
                await asyncio.to_thread(db.record_news_fetch, source, len(items), written, newest)
                return source, {"status": "ok", "fetched": len(items), "resolved": len(rows),
                                "unresolved": len(unresolved), "written": written,
                                "newest_published": newest or seen, **info,
                                "duration_s": round(time.monotonic() - started, 1)}
            except Exception as e:
                logger.warning(f"[News] {source} failed: {e}")
                await asyncio.to_thread(db.record_news_fetch, source, 0, 0, None, "error",
                                        str(e)[:300])
                return source, {"status": "error", "error": str(e)[:300],
                                "duration_s": round(time.monotonic() - started, 1)}

        results = dict(await asyncio.gather(*(run(s) for s in wanted)))
    finally:
        if own:
            await client.aclose()
    ok = [r for r in results.values() if r["status"] == "ok"]
    if wanted and not ok:
        raise RuntimeError("; ".join(f"{s}: {r.get('error')}" for s, r in results.items()))
    return {"fetched": sum(r.get("fetched", 0) for r in ok),
            "written": sum(r.get("written", 0) for r in ok),
            "unresolved": sum(r.get("unresolved", 0) for r in ok),
            "sources": results}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

# Two items about the same player within this window whose word sets overlap
# at least NEAR_DUPLICATE_JACCARD are one item from several sources.
NEAR_DUPLICATE_HOURS = 48
NEAR_DUPLICATE_JACCARD = 0.7


def _words(text: str) -> set[str]:
    return set(normalized(text).split())


def _when(row: dict) -> datetime | None:
    value = row.get("published_at") or row.get("date_reported") or row.get("recorded_at")
    iso = _iso(value)
    return datetime.fromisoformat(iso) if iso else None


def _same_story(a: dict, b: dict) -> bool:
    ta, tb = a["_words_head"], b["_words_head"]
    if ta and tb and len(ta & tb) / len(ta | tb) >= NEAR_DUPLICATE_JACCARD:
        return True
    wa, wb = a["_words"], b["_words"]
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= NEAR_DUPLICATE_JACCARD


def merge_timeline(rows: list[dict]) -> list[dict]:
    """One player's items, newest first, near-duplicates across sources merged.

    Each entry: ``{published_at, headline, text, source, url, sources:
    [{source, url}]}``; the first (newest) copy keeps its text, the others add
    their source and link.
    """
    entries: list[dict] = []
    ordered = sorted(rows, key=lambda r: (_when(r) or datetime.min.replace(tzinfo=UTC)),
                     reverse=True)
    for r in ordered:
        text = item_text(r)
        cand = {"_words": _words(text), "_words_head": _words(r.get("headline") or ""),
                "_when": _when(r)}
        twin = None
        for e in entries:
            close = (e["_when"] is None or cand["_when"] is None
                     or abs((e["_when"] - cand["_when"]).total_seconds())
                     <= NEAR_DUPLICATE_HOURS * 3600)
            if close and _same_story(e, cand):
                twin = e
                break
        link = {"source": r.get("source"), "url": r.get("url")}
        if twin is not None:
            if link not in twin["sources"]:
                twin["sources"].append(link)
            continue
        entries.append({**cand, "published_at": r.get("published_at") or r.get("date_reported"),
                        "headline": r.get("headline"), "text": text,
                        "source": r.get("source"), "url": r.get("url"),
                        "player_name": r.get("player_name"), "team": r.get("team"),
                        "sources": [link]})
    return [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries]
