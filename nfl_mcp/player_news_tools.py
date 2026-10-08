"""``get_player_news``: one merged news timeline per player, with its flags.

Reads what the ``news`` refresh stored (`news_sources`: ESPN's fantasy feed,
NBC Sports / Rotoworld, CBS) plus the player's current injury blurb, merges
the same note from several sources into one entry (`news_sources.
merge_timeline`) and reads the flags the projections read
(`news_signals.build_index` over the player's team, so a teammate's note
that names him counts). No network: a stale or empty store is reported, with
the refresh to run.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from . import news_signals
from .database import get_shared_db
from .errors import create_success_response
from .news_sources import SOURCE_LABELS, health_summary, merge_timeline
from .opportunity_tools import norm_name
from .teams import normalize_team

logger = logging.getLogger(__name__)

MAX_PLAYERS = 30
MAX_DAYS = 30
DEFAULT_DAYS = 7
# Timeline entries per player (newest first); a league-wide call keeps fewer.
MAX_ITEMS = 12
LEAGUE_MAX_ITEMS = 4
# Older than this, the store is called stale.
STALE_HOURS = 6.0
_FANTASY = {"QB", "RB", "WR", "TE", "K"}


def _now() -> datetime:
    return datetime.now(UTC)


def _held_ids(rosters: list[dict]) -> set[str]:
    held: set[str] = set()
    for r in rosters or []:
        for key in ("players", "reserve", "taxi"):
            held |= {str(p) for p in r.get(key) or [] if p and str(p) != "0"}
    return held


def _resolve(db, queries: list[str], held: set[str]) -> tuple[list[dict], list[dict]]:
    """``(players, unresolved)``: ids as given, names via the athlete cache
    (a candidate rostered in the league wins a shared name)."""
    from .lineup_tools import candidate_summary, name_candidates
    ids = [q for q in queries if q.isdigit()]
    rows = db.get_athletes_by_ids(ids) if ids else {}
    out, unresolved = [], []
    for q in queries:
        if q.isdigit():
            row = rows.get(q)
            if row:
                out.append({"query": q, "row": row})
            else:
                unresolved.append({"name": q, "reason": "no athlete with that Sleeper id"})
            continue
        ranked, ambiguous = name_candidates(db, q, include_free_agents=True)
        exact = [r for r in ranked if norm_name(r.get("full_name")) == norm_name(q)]
        pick = next((r for r in exact if str(r.get("id")) in held), None)
        if pick is None and ranked:
            pick = ranked[0]
        if not pick or not pick.get("id"):
            unresolved.append({"name": q, "reason": "no player by that name in the athlete cache"})
            continue
        entry = {"query": q, "row": pick}
        if ambiguous and str(pick.get("id")) not in held:
            entry["ambiguous"] = True
            entry["candidates"] = candidate_summary(ranked)
        out.append(entry)
    return out, unresolved


def _freshness(db, now: datetime) -> tuple[dict, list[str]]:
    """``(sources, warnings)``: each source's last fetch and health
    (`news_sources.health_summary`), and a warning for a stale store or a
    degraded / failing source."""
    state = db.get_news_fetch_state() if hasattr(db, "get_news_fetch_state") else {}
    health = health_summary(state, now)
    out, warnings = {}, list(health.get("warnings") or [])
    for source, st in sorted(state.items()):
        age = None
        try:
            age = round((now - datetime.fromisoformat(st["fetched_at"])).total_seconds() / 3600, 1)
        except (TypeError, ValueError, KeyError):
            pass
        h = health["sources"].get(source) or {}
        out[source] = {"label": SOURCE_LABELS.get(source, source), "fetched_at": st.get("fetched_at"),
                       "age_hours": age, "status": st.get("status"),
                       "health": h.get("health"), "last_success_at": h.get("last_success_at"),
                       "newest_item": st.get("newest_published"),
                       **({k: h[k] for k in ("detail", "consecutive_failures", "next_attempt_at")
                           if h.get(k)}),
                       **({"enabled": False} if h and not h.get("enabled") else {}),
                       **({"error": st["error"]} if st.get("error") else {})}
    if not state:
        warnings.append("No news has been fetched yet: run refresh_data(scope=[\"news\"]) "
                        "(the prefetch loop does it every 45 min when enabled).")
    else:
        ages = [v["age_hours"] for v in out.values() if v["age_hours"] is not None]
        if ages and min(ages) > STALE_HOURS:
            warnings.append(f"News is {min(ages)}h old: run refresh_data(scope=[\"news\"]).")
    return out, warnings


def _item_flags(text: str, owner: str, names: dict, owner_last: str) -> list[str]:
    """The flags one entry carries for its player (a conditional one --
    "would be the lead back if Hall can't go" -- as ``lead_role?``)."""
    return sorted({h["flag"] + ("?" if h.get("conditional") else "")
                   for h in news_signals.classify(text, owner, names, owner_last)
                   if h["about"] == owner})


async def get_player_news(players: list[str] | None = None, days: int = DEFAULT_DAYS,
                          league_id: str | None = None) -> dict:
    """See the module doc and the tool docstring (`tool_registry`)."""
    db = get_shared_db()
    now = _now()
    days = max(1, min(MAX_DAYS, int(days or DEFAULT_DAYS)))
    queries = [str(p).strip() for p in players or [] if str(p or "").strip()]
    if len(queries) > MAX_PLAYERS:
        return create_success_response({"success": False,
                                        "error": f"players: at most {MAX_PLAYERS} per call"})
    held: set[str] = set()
    warnings: list[str] = []
    if league_id:
        from . import sleeper_tools
        resp = await sleeper_tools.get_rosters(league_id)
        held = _held_ids((resp or {}).get("rosters") or [])
        if not held:
            warnings.append(f"Could not read the rosters of league {league_id}.")
    if not queries and not held:
        return create_success_response({
            "success": False, "error": "give players (names or Sleeper ids) or a league_id"})
    league_wide = not queries
    if league_wide:
        rows = await asyncio.to_thread(db.get_athletes_by_ids, sorted(held))
        resolved = [{"query": pid, "row": row} for pid, row in sorted(rows.items())
                    if (row.get("position") or "").upper() in _FANTASY]
        unresolved: list[dict] = []
    else:
        resolved, unresolved = await asyncio.to_thread(_resolve, db, queries, held)

    freshness, fresh_warnings = _freshness(db, now)
    warnings += fresh_warnings
    since = (now - timedelta(days=days)).isoformat()
    read_since = (now - timedelta(days=max(days, news_signals.MAX_AGE_DAYS))).isoformat()
    teams = sorted({normalize_team(p["row"].get("team_id")) for p in resolved
                    if normalize_team(p["row"].get("team_id"))})
    ids = [str(p["row"]["id"]) for p in resolved]
    # The players' own items (any team) and their teams' items (attribution).
    stored = await asyncio.to_thread(db.get_player_news, ids, read_since, teams)
    injuries = [r for r in (await asyncio.to_thread(db.get_all_current_injuries) or [])
                if normalize_team(r.get("team_id")) in set(teams)]
    index = news_signals.build_index(injuries, now, news=news_signals.news_rows(stored))
    names_by_team: dict[str, dict] = {}
    for r in [*stored, *injuries]:
        team = normalize_team(r.get("team") or r.get("team_id")) or ""
        name = r.get("player_name")
        if name:
            names = names_by_team.setdefault(team, {})
            last = news_signals._last_name(name)
            names[last] = norm_name(name) if names.get(last, norm_name(name)) == norm_name(name) \
                else None

    out = []
    cap = LEAGUE_MAX_ITEMS if league_wide else MAX_ITEMS
    for p in resolved:
        row = p["row"]
        pid, name = str(row["id"]), row.get("full_name") or p["query"]
        team = normalize_team(row.get("team_id")) or ""
        own = [s for s in stored if s.get("player_id") == pid
               and (s.get("published_at") or s.get("recorded_at") or "") >= since]
        report = next((r for r in injuries if norm_name(r.get("player_name")) == norm_name(name)
                       and normalize_team(r.get("team_id")) == team), None)
        if report and report.get("injury_description") and \
                (report.get("date_reported") or "") >= since[:10]:
            own.append({"source": "injury_report", "player_name": name, "team": team,
                        "headline": report["injury_description"], "text": "",
                        "published_at": report.get("date_reported"), "url": None})
        timeline = merge_timeline(own)
        owner, owner_last = norm_name(name), news_signals._last_name(name)
        for e in timeline:
            e["flags"] = _item_flags(e["text"], owner, names_by_team.get(team) or {}, owner_last)
            if len(e["text"]) > 600:
                e["text"] = e["text"][:597] + "..."
            e.pop("player_name", None)
            e.pop("team", None)
        flags = news_signals.signals_for(index, name, team)
        status = (report or {}).get("injury_status")
        from .injury_status import availability
        entry = {
            "query": p["query"], "name": name, "player_id": pid, "team": team or "FA",
            "position": (row.get("position") or "").upper() or None,
            "injury_status": status,
            "items": len(timeline), "news_flags": flags,
            **({"news_adjustment": news_signals.adjustment(flags, None, availability(status))}
               if flags else {}),
            "timeline": timeline[:cap],
            **({"ambiguous": True, "candidates": p["candidates"]} if p.get("ambiguous") else {}),
        }
        if league_wide and not timeline and not flags:
            continue
        out.append(entry)
    out.sort(key=lambda e: (-len(e["news_flags"]), -e["items"]) if league_wide else 0)
    return create_success_response({
        "players": out, "unresolved": unresolved, "window_days": days,
        "league_id": league_id, "sources": freshness,
        "notes": ["news_flags are what the projections read (recency-weighted, one per flag, "
                  "the newest availability flag only); news_adjustment is their effect on "
                  "our model's share of the blend (Sleeper's projection already saw the news).",
                  "A note carried by several sources is one timeline entry (sources lists "
                  "each copy)."],
        **({"warnings": warnings} if warnings else {}),
    })
