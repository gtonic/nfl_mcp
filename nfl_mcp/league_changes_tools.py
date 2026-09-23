"""What changed for one roster since it was last checked.

A daily check is a diff, not a report: the useful answer to "anything new?" is
the handful of things that moved since yesterday, most important first — not
the whole injury table, the whole news feed and the whole transaction log
again. Each source already exists as a tool; this reads them against a
per-roster "last checked" time (``league_checks``) and keeps only what is new
and touches this roster or this week's opponent:

- injury status moves on your roster and the opponent's starters, from the
  recorded ``injury_history`` timeline (joined through the name/team index,
  as the briefing does — the timeline is keyed by ESPN ids);
- news items (ESPN, CBS) that name one of those players;
- league transactions processed since then;
- trending adds of a same-position teammate of one of your starters — the
  backup who gets the job if the starter misses time;
- projection moves beyond a threshold for your starters, against the logged
  projection as it stood at the last check.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

from .briefing_tools import (
    _build_player,
    _status_moves,
    find_roster,
    weather_by_team,
)
from .database import get_shared_db
from .errors import create_success_response
from .game_clock import progress_of, week_games
from .injury_match import build_injury_index, find_report, misses_this_week
from .injury_service import status_severity
from .projection_store import log_projections, scoring_key
from .scoring import league_scoring
from .teams import normalize_team
from .week_context import current_season_week, week_opponents

logger = logging.getLogger(__name__)

# First check for a roster: look back this far rather than at everything.
DEFAULT_LOOKBACK_HOURS = 24
# Below this a projection move is inside the week-to-week noise of the inputs.
DEFAULT_PROJECTION_THRESHOLD = 2.0
DEFAULT_LIMIT = 25

# How much each kind of change matters, before its own severity is added.
# Your starters first, then the opponent's, then your bench and the league.
_ROLE_WEIGHT = {"my_starter": 60, "opp_starter": 35, "my_bench": 25}

# Positions whose backup inherits the job outright. Receivers share targets
# across three spots, so a WR teammate counts only on the same depth-chart spot.
_BACKUP_POSITIONS = {"QB", "RB", "TE", "WR"}


def _parse_time(value) -> datetime | None:
    """An aware UTC datetime from ISO text, epoch ms, or "N hours ago"."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, UTC)
    text = str(value).strip()
    rel = re.match(r"^(\d+)\s*(minute|min|hour|hr|day)s?\s+ago$", text, re.I)
    if rel:
        n, unit = int(rel.group(1)), rel.group(2).lower()
        delta = (timedelta(minutes=n) if unit.startswith("min")
                 else timedelta(hours=n) if unit.startswith("h") else timedelta(days=n))
        return datetime.now(UTC) - delta
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _label(pid: str, athletes: dict) -> str:
    row = athletes.get(pid) or {}
    return row.get("full_name") or normalize_team(row.get("team_id")) or pid


def _raw(row: dict | None) -> dict:
    import json
    raw = (row or {}).get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _item(kind: str, importance: float, summary: str, **extra) -> dict:
    return {"kind": kind, "importance": round(importance), "summary": summary,
            **{k: v for k, v in extra.items() if v is not None}}


def injury_items(db, since: str, roles: dict[str, str], athletes: dict) -> list[dict]:
    """Status moves since ``since``, one item per player (first -> latest)."""
    injury_index = build_injury_index(db.get_all_current_injuries())
    report_to_pid: dict[str, str] = {}
    for pid in roles:
        row = athletes.get(pid)
        report = find_report(row, injury_index, (row or {}).get("team_id")) if row else None
        if report and report.get("player_id"):
            report_to_pid[str(report["player_id"])] = pid
    if not report_to_pid:
        return []
    moves = _status_moves(db.get_injury_status_changes(
        since=since, limit=200, player_ids=list(report_to_pid)))
    # Newest first; fold several moves of one player into first -> latest.
    by_player: dict[str, list[dict]] = {}
    for m in moves:
        by_player.setdefault(str(m.get("player_id")), []).append(m)
    items = []
    for report_id, rows in by_player.items():
        pid = report_to_pid.get(report_id)
        if not pid:
            continue
        latest, earliest = rows[0], rows[-1]
        before, after = earliest.get("previous_status"), latest.get("injury_status")
        if before == after:
            continue  # moved and moved back: nothing to act on
        role = roles[pid]
        worse = status_severity(after) > status_severity(before)
        weight = 25 if misses_this_week(after) else 12 if worse else 5
        who = {"my_starter": "your starter", "my_bench": "your bench player",
               "opp_starter": "opponent's starter"}[role]
        items.append(_item(
            "injury", _ROLE_WEIGHT[role] + weight,
            f"{_label(pid, athletes)} ({who}): {before or 'none'} -> {after}",
            player=_label(pid, athletes), role=role, from_status=before,
            to_status=after, injury_type=latest.get("injury_type"),
            at=latest.get("recorded_at"),
        ))
    return items


def _name_patterns(roles: dict[str, str], athletes: dict) -> list[tuple[str, re.Pattern]]:
    out = []
    for pid in roles:
        row = athletes.get(pid) or {}
        name = row.get("full_name")
        if name and (row.get("position") or "").upper() not in ("DEF", "DST"):
            out.append((pid, re.compile(rf"\b{re.escape(name)}\b", re.I)))
    return out


async def news_items(since_dt: datetime, roles: dict[str, str], athletes: dict,
                     errors: dict) -> list[dict]:
    """ESPN and CBS items published since ``since_dt`` that name a player."""
    from . import cbs_fantasy_tools, nfl_tools
    patterns = _name_patterns(roles, athletes)
    if not patterns:
        return []
    sources = (
        ("ESPN", nfl_tools.get_nfl_news, "articles"),
        ("CBS", cbs_fantasy_tools.get_cbs_player_news, "news"),
    )
    # Both feeds at once; the order they are read in below is unchanged.
    responses = await asyncio.gather(
        *(fetch(limit=50) for _, fetch, _ in sources), return_exceptions=True
    )
    feeds = []
    for (label, _, key), resp in zip(sources, responses, strict=True):
        if isinstance(resp, BaseException):
            errors[f"{label.lower()}_news"] = str(resp)
            continue
        resp = resp or {}
        if resp.get("success") is False:
            errors[f"{label.lower()}_news"] = resp.get("error") or "unavailable"
        feeds += [(label, a) for a in resp.get(key) or []]

    items, seen = [], set()
    for source, article in feeds:
        published = _parse_time(article.get("published"))
        # An undated item cannot be placed before or after the last check, so
        # it is left out rather than repeated every day.
        if published is None or published < since_dt:
            continue
        headline = (article.get("headline") or "").strip()
        text = " ".join(filter(None, [article.get("player"), headline,
                                      article.get("description")]))
        for pid, pattern in patterns:
            key = (pid, headline.lower())
            if key in seen or not pattern.search(text):
                continue
            seen.add(key)
            role = roles[pid]
            items.append(_item(
                "news", _ROLE_WEIGHT[role] - 20,
                f"{_label(pid, athletes)}: {headline or (article.get('description') or '')[:120]}",
                player=_label(pid, athletes), role=role, source=source,
                at=published.isoformat(),
            ))
    return items


async def transaction_items(league_id: str, weeks: list[int], since_dt: datetime,
                            my_rid: int, opp_rid: int | None, owners: dict[int, str],
                            db, errors: dict) -> list[dict]:
    """Processed adds, drops and trades since ``since_dt``."""
    from . import sleeper_tools
    txs = []
    # Every week at once: an empty week is retried with a back-off inside
    # get_transactions, which should not hold up the other week.
    responses = await asyncio.gather(
        *(sleeper_tools.get_transactions(league_id, week=wk) for wk in weeks),
        return_exceptions=True,
    )
    for wk, resp in zip(weeks, responses, strict=True):
        if isinstance(resp, BaseException):
            errors[f"transactions_week_{wk}"] = str(resp)
            continue
        resp = resp or {}
        if resp.get("success") is False:
            errors[f"transactions_week_{wk}"] = resp.get("error") or "unavailable"
        txs += resp.get("transactions") or []
    fresh = []
    seen = set()
    for t in txs:
        if (t.get("status") or "complete") != "complete":
            continue
        when = _parse_time(t.get("status_updated") or t.get("created"))
        tid = t.get("transaction_id") or id(t)
        if when is None or when < since_dt or tid in seen:
            continue
        seen.add(tid)
        fresh.append((when, t))
    ids = set()
    for _, t in fresh:
        ids |= set((t.get("adds") or {}).keys()) | set((t.get("drops") or {}).keys())
    athletes = db.get_athletes_by_ids(list(ids)) if ids else {}

    items = []
    for when, t in fresh:
        adds, drops = t.get("adds") or {}, t.get("drops") or {}
        rids = set(t.get("roster_ids") or []) | set(adds.values()) | set(drops.values())
        kind = t.get("type") or "transaction"
        parts = []
        for rid in sorted(rids, key=lambda r: (r != my_rid, r)):
            got = [_label(p, athletes) for p, r in adds.items() if r == rid]
            lost = [_label(p, athletes) for p, r in drops.items() if r == rid]
            team = "you" if rid == my_rid else owners.get(rid) or f"roster {rid}"
            seg = []
            if got:
                seg.append("+" + ", +".join(got))
            if lost:
                seg.append("-" + ", -".join(lost))
            if seg:
                parts.append(f"{team}: {' '.join(seg)}")
        if my_rid in rids:
            importance = 70 if kind == "trade" else 50
        elif opp_rid is not None and opp_rid in rids:
            importance = 40
        else:
            importance = 30 if kind == "trade" else 15
        items.append(_item("transaction", importance,
                           f"{kind}: " + "; ".join(parts) if parts else kind,
                           type=kind, at=when.isoformat()))
    return items


async def trending_backup_items(db, since_dt: datetime, starters: list[str],
                                athletes: dict, rostered: dict[str, int], my_rid: int,
                                errors: dict) -> list[dict]:
    """Trending adds who back up one of your starters (same team and position)."""
    from . import sleeper_tools
    hours = max(1, min(168, int((datetime.now(UTC) - since_dt).total_seconds() // 3600) or 1))
    try:
        resp = await sleeper_tools.get_trending_players(
            nfl_db=db, trend_type="add", lookback_hours=hours, limit=50)
    except Exception as e:
        errors["trending"] = str(e)
        return []
    if (resp or {}).get("success") is False:
        errors["trending"] = resp.get("error") or "unavailable"
    trending = (resp or {}).get("trending_players") or []
    rows = db.get_athletes_by_ids([str(t.get("player_id")) for t in trending if t.get("player_id")])

    mine = []
    for pid in starters:
        row = athletes.get(pid) or {}
        pos = (row.get("position") or "").upper()
        if pos in _BACKUP_POSITIONS and normalize_team(row.get("team_id")):
            mine.append((pid, pos, normalize_team(row.get("team_id")),
                         _raw(row).get("depth_chart_position")))
    items = []
    for t in trending:
        tid = str(t.get("player_id") or "")
        row = rows.get(tid) or {}
        pos = (row.get("position") or t.get("position") or "").upper()
        team = normalize_team(row.get("team_id") or t.get("team"))
        spot = _raw(row).get("depth_chart_position")
        for pid, s_pos, s_team, s_spot in mine:
            if tid == pid or pos != s_pos or team != s_team:
                continue
            if s_pos == "WR" and (not spot or spot != s_spot):
                continue
            owner = rostered.get(tid)
            status = ("free_agent" if owner is None else "yours" if owner == my_rid
                      else "rostered")
            count = int(t.get("count") or 0)
            items.append(_item(
                "trending_backup",
                30 + (15 if status == "free_agent" else 0) + min(15, count / 2000),
                f"{_label(tid, rows)} ({s_pos}, {team}) trending +{count} adds — "
                f"behind your starter {_label(pid, athletes)}; {status.replace('_', ' ')}",
                player=_label(tid, rows), starter=_label(pid, athletes),
                adds=count, availability=status,
            ))
    return items


async def projection_items(db, league: dict, league_id: str, season: int, week: int,
                           starters: list[str], athletes: dict, since_iso: str,
                           threshold: float) -> list[dict]:
    """Starters whose projection moved by more than ``threshold`` since the
    last check, measured against the logged projection as it stood then."""
    from .projections import project_players
    scoring = league_scoring(league)
    key = scoring_key(scoring)
    games = week_games(db, season, week)
    open_ids = [pid for pid in starters
                if progress_of(games.get(normalize_team(
                    (athletes.get(pid) or {}).get("team_id")) or "")) <= 0.0]
    if not open_ids:
        return []
    baseline = db.get_logged_projections(season, week, key, player_ids=open_ids,
                                         as_of=since_iso, which="latest")
    # A projection first logged after the last check (a briefing ran in
    # between) is still a baseline: it is what the user last saw.
    first = db.get_logged_projections(season, week, key, player_ids=open_ids, which="first")
    baseline = {**first, **baseline}
    if not baseline:
        return []

    opponents = week_opponents(db, season, week)
    usage = {row["player_id"]: row for row in db.get_usage_for_week(season, max(1, week - 1))}
    injury_index = build_injury_index(db.get_all_current_injuries())
    weather = await weather_by_team(season, week)
    inputs = [p for p in (_build_player(pid, athletes, opponents, weather, usage, injury_index)
                          for pid in open_ids) if p]
    if not inputs:
        return []
    result = await project_players(
        inputs, scoring=scoring, num_teams=int(league.get("total_rosters") or 12),
        season=season, week=week,
    )
    now_rows = [{**proj, "player_id": inp["player_id"]}
                for inp, proj in zip(inputs, (result or {}).get("projections") or [],
                                     strict=False)]
    items = []
    for row in now_rows:
        pid = row["player_id"]
        before = baseline.get(pid)
        if not before or row.get("projected_points") is None:
            continue
        delta = float(row["projected_points"]) - float(before["projected_points"])
        if abs(delta) < threshold:
            continue
        items.append(_item(
            "projection", min(70, 30 + 4 * abs(delta)),
            f"{row.get('player')} projection {before['projected_points']:.1f} -> "
            f"{row['projected_points']:.1f} ({delta:+.1f})",
            player=row.get("player"), before=round(float(before["projected_points"]), 1),
            after=row["projected_points"], delta=round(delta, 1),
            baseline_at=before.get("recorded_at"),
        ))
    # Logged after comparing, so this check's numbers are the next baseline.
    log_projections(db, season, week, scoring, now_rows, league_id=league_id,
                    source="league_changes", games=games)
    return items


async def get_league_changes(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    since: str | None = None,
    mark_seen: bool = True,
    projection_threshold: float = DEFAULT_PROJECTION_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Everything that moved for one roster since the last check; see module."""
    from . import sleeper_tools

    db = get_shared_db()
    checked_at = datetime.now(UTC)

    roster_state = sleeper_tools.roster_freshness(await sleeper_tools.get_rosters(league_id))
    if roster_state["error"]:
        return create_success_response({"success": False, "error": roster_state["error"]})
    rosters = roster_state["rosters"]
    mine, error = find_roster(rosters, league_id, roster_id, user_id)
    if error:
        return create_success_response({"success": False, "error": error})
    roster_id = mine["roster_id"]

    if since:
        since_dt = _parse_time(since)
        if since_dt is None:
            return create_success_response({
                "success": False,
                "error": f"Could not read since={since!r}; pass an ISO-8601 timestamp.",
            })
        since_source = "caller"
    else:
        stored = db.get_league_last_check(league_id, roster_id)
        since_dt = _parse_time(stored) if stored else None
        since_source = "last_check" if since_dt else f"default_{DEFAULT_LOOKBACK_HOURS}h"
        since_dt = since_dt or checked_at - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    since_iso = since_dt.isoformat()

    league_resp, current = await asyncio.gather(
        sleeper_tools.get_league(league_id), current_season_week(db)
    )
    league = (league_resp or {}).get("league") or {}
    season, week = current["season"], current["week"]

    matchups = (await sleeper_tools.get_matchups(league_id, week) or {}).get("matchups") or []
    my_matchup = next((m for m in matchups if m.get("roster_id") == roster_id), None)
    # A null matchup_id (bye / unscheduled) pairs with nobody — `None == None`
    # used to make every such roster "the opponent".
    opp_matchup = next(
        (m for m in matchups
         if my_matchup and my_matchup.get("matchup_id") is not None
         and m.get("matchup_id") == my_matchup.get("matchup_id")
         and m.get("roster_id") != roster_id),
        None,
    )
    opp_rid = (opp_matchup or {}).get("roster_id")
    opp_roster = next((r for r in rosters if r.get("roster_id") == opp_rid), None)
    starters = [s for s in ((my_matchup or {}).get("starters") or mine.get("starters") or [])
                if s and s != "0"]
    opp_starters = [s for s in ((opp_matchup or {}).get("starters")
                                or (opp_roster or {}).get("starters") or [])
                    if s and s != "0"]

    roles: dict[str, str] = {}
    for pid in opp_starters:
        roles[pid] = "opp_starter"
    for pid in mine.get("players") or []:
        roles[pid] = "my_bench"
    for pid in starters:
        roles[pid] = "my_starter"
    athletes = db.get_athletes_by_ids(list(roles))
    rostered = {pid: r.get("roster_id") for r in rosters for pid in (r.get("players") or [])}

    owners: dict[int, str] = {}
    try:
        users = (await sleeper_tools.get_league_users(league_id) or {}).get("users") or []
        names = {u.get("user_id"): u.get("display_name") for u in users}
        owners = {r.get("roster_id"): names.get(r.get("owner_id")) for r in rosters}
    except Exception as e:
        logger.debug(f"league users unavailable: {e}")

    errors: dict[str, str] = {}
    items: list[dict] = []
    try:
        items += injury_items(db, since_iso, roles, athletes)
    except Exception as e:
        errors["injuries"] = str(e)
    weeks = sorted({w for w in (week - 1, week) if 1 <= w <= 18})

    async def _projections() -> list[dict]:
        try:
            return await projection_items(db, league, league_id, season, week, starters,
                                          athletes, since_iso, projection_threshold)
        except Exception as e:
            errors["projections"] = str(e)
            return []

    # The four sources are independent network reads; fetched together and
    # appended in the same order as before.
    groups = await asyncio.gather(
        news_items(since_dt, roles, athletes, errors),
        transaction_items(league_id, weeks, since_dt, roster_id, opp_rid,
                          owners, db, errors),
        trending_backup_items(db, since_dt, starters, athletes, rostered,
                              roster_id, errors),
        _projections(),
    )
    for group in groups:
        items += group

    # Most important first; among equals, the newest.
    items.sort(key=lambda i: i.get("at") or "", reverse=True)
    items.sort(key=lambda i: -i["importance"])
    counts: dict[str, int] = {}
    for i in items:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    shown = items[:max(1, int(limit))]

    # The windowed sources (news, transactions, injuries) only report what
    # happened since `since`: advancing the mark past a failed fetch loses that
    # window for good. Hold it until they all come back.
    windowed_failed = sorted(k for k in errors
                             if k.startswith("transactions") or k.endswith("_news")
                             or k == "injuries")
    advanced = bool(mark_seen) and not windowed_failed
    if advanced:
        db.set_league_last_check(league_id, roster_id, checked_at.isoformat())

    return sleeper_tools.mark_roster_staleness(create_success_response({
        "league_id": league_id,
        "roster_id": roster_id,
        "opponent_roster_id": opp_rid,
        "season": season,
        "week": week,
        "week_source": current["source"],
        "since": since_iso,
        "since_source": since_source,
        "checked_at": checked_at.isoformat(),
        "marked_seen": advanced,
        # Why the last-check mark was held back (the next call re-reads the window).
        "not_marked_because": windowed_failed if mark_seen and windowed_failed else None,
        "changes": shown,
        "counts": counts,
        "omitted": len(items) - len(shown),
        "errors": errors,
        "message": (f"{len(items)} change(s) since {since_iso[:16]}Z"
                    if items else f"Nothing new since {since_iso[:16]}Z"),
    }), roster_state)
