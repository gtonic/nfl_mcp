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
- news items (ESPN, CBS) about one of those players -- attributed to the
  item's subject, so a teammate's note that names him is not his news unless
  it reports the teammate's absence at a related spot (``teammate_news``);
  roundups are left out, and an item's importance follows its signal;
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
from .handcuff_tools import trending_backups
from .injury_match import build_injury_index, find_report, misses_this_week, sleeper_injury_status
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


def _item(kind: str, importance: float, summary: str, **extra) -> dict:
    return {"kind": kind, "importance": round(importance), "summary": summary,
            **{k: v for k, v in extra.items() if v is not None}}


def injury_items(db, since: str, roles: dict[str, str], athletes: dict) -> list[dict]:
    """Status moves since ``since``, one item per player (first -> latest)."""
    injury_index = build_injury_index(db.get_all_current_injuries())
    report_to_pid: dict[str, str] = {}
    for role_pid in roles:
        row = athletes.get(role_pid)
        report = find_report(row, injury_index, (row or {}).get("team_id")) if row else None
        if report and report.get("player_id"):
            report_to_pid[str(report["player_id"])] = role_pid
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


# CBS titles an item "<Team>' <Player>: <blurb>" ("Cardinals' Bam Knight:
# Ruled out for Sunday's game"); RotoWire-style feeds "<Player>: <blurb>".
# Two to four capitalised words for the name ("Michael Pittman Jr.").
_NAME = r"[A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){1,3}"
_TEAM_TITLE_RE = re.compile(rf"^(?P<team>[A-Z0-9][\w.]*(?:\s+[A-Z0-9][\w.]*)?)['’]s?\s+"
                            rf"(?P<name>{_NAME}):\s")
_NAME_TITLE_RE = re.compile(rf"^(?P<name>{_NAME}):\s")
# A roundup is about many players, not the one it happens to name: a preview,
# picks, rankings, a waiver / pickups list, start-sit advice. Only read for an
# item without a parsed subject (a CBS / RotoWire note is about its player).
_ROUNDUP_RE = re.compile(
    r"\b(?:previews?|picks|predictions|pickups|rankings|ranks|start\s*'?em|sit\s*'?em|"
    r"start[/-]sit|sleepers|busts|waiver wire|waiver adds|lead the way|takeaways|"
    r"winners and losers|overreactions|what we learned|things to know|mock draft|"
    r"betting odds|best bets|props|dfs|cheat sheet|lineup advice|must-starts|streamers|"
    r"fantasy tips)\b", re.I)
# What the news_signals classifier does not phrase-match but a note says in a
# few words: an absence ("listed as out", "not practicing", "suspended",
# "doubtful"), and the cues of an injury, a transaction or a stat line.
_ABSENCE_RE = re.compile(
    r"\b(?:(?:listed as|is|was|will be|remains?)\s+out\b|out for (?:sunday|monday|thursday|"
    r"saturday|the game|week \d+)|doubtful|suspen(?:ded|sion)|injured reserve|IR\b|sidelined|"
    r"(?:did not|didn't|not|won't|will not|hasn't|has not|no)\s+(?:practic|participat)\w*|"
    r"(?:absent|missing|missed|sat out|sit out|sitting out)\s+(?:from\s+)?(?:\w+'s\s+)?practice|"
    r"(?:not|wasn't|was not|isn't|is not)\s+(?:present|seen|spotted)\s+(?:at|for)\b|DNP\b)",
    re.I)
_INJURY_RE = re.compile(
    r"\b(?:questionable|limited (?:participant|practice|in)|full participant|practic\w+|"
    r"injur\w+|concussion|hamstring|ankle|knee|groin|calf|shoulder|illness)\b", re.I)
_TRANSACTION_RE = re.compile(
    r"\b(?:signs?|signed|re-signed|released|waived|traded|claimed|acquired|activated|"
    r"promoted|elevated|placed on|designated)\b", re.I)
_PERFORMANCE_RE = re.compile(
    r"\b(?:\d+\s+(?:yards|targets|receptions|catches|carries|touchdowns|tds|snaps|"
    r"attempts|tackles|sacks)|(?:one|two|three|\d+)-catch|caught|rushed|completed|"
    r"touchdowns?|fumbled?|intercept\w*)\b", re.I)
_ABSENCE_FLAGS = {"ruled_out", "unlikely_to_play", "multi_week_absence", "week_to_week"}
_ROLE_FLAGS = {"benched", "committee", "lead_role", "limited_snaps", "snap_share_drop"}
# How much of the role's news weight an item gets, by what it says. A note
# without an injury, role, transaction or stat cue ("makes time for art")
# barely registers; an absence ranks above the rest.
SIGNAL_MULT = {"absence": 1.25, "availability": 1.0, "role": 1.0, "transaction": 1.0,
               "performance": 0.6, "none": 0.15}
# A teammate's absence counts for less than the player's own news; a name
# dropped only in a general article's body for half.
TEAMMATE_MULT = 0.75
MENTION_MULT = 0.5
_ABSENCE_PHRASE = {"ruled_out": "ruled out", "unlikely_to_play": "unlikely to play",
                   "multi_week_absence": "out for multiple weeks",
                   "week_to_week": "week-to-week"}
# A teammate at a related spot inherits the work: the same position, or the
# other pass-catcher (WR / TE share targets).
_RELATED = {"WR": {"WR", "TE"}, "TE": {"TE", "WR"}}


def news_subject(article: dict) -> tuple[str | None, str | None, str | None]:
    """``(name, team, position)`` of the player an item is about, as far as
    the item says: the feed's own player field (CBS), else a "<Team>'
    <Player>:" or "<Player>:" title. ``(None, ...)`` for a general article."""
    headline = (article.get("headline") or "").strip()
    team = normalize_team(article.get("team"))
    position = (article.get("position") or "").upper() or None
    if article.get("player"):
        return str(article["player"]).strip(), team, position
    m = _TEAM_TITLE_RE.match(headline)
    if m and normalize_team(m.group("team")):
        return m.group("name"), team or normalize_team(m.group("team")), position
    m = _NAME_TITLE_RE.match(headline)
    if m:
        return m.group("name"), team, position
    return None, team, position


def news_signal(text: str) -> tuple[str, list[str]]:
    """``(signal, flags)``: what kind of fantasy content a note carries --
    ``absence`` / ``role`` / ``availability`` / ``transaction`` /
    ``performance`` / ``none`` -- from the news_signals classifier's flags
    (every hit taken as the note's subject's) plus a few absence, injury,
    transaction and stat-line cues."""
    from .news_signals import classify
    flags = sorted({h["flag"] for h in classify(text, "subject") if not h.get("conditional")})
    if _ABSENCE_FLAGS & set(flags) or _ABSENCE_RE.search(text):
        return "absence", flags
    if _ROLE_FLAGS & set(flags):
        return "role", flags
    if flags or _INJURY_RE.search(text):
        return "availability", flags
    if _TRANSACTION_RE.search(text):
        return "transaction", flags
    if _PERFORMANCE_RE.search(text):
        return "performance", flags
    return "none", flags


def _absence_phrase(flags: list[str], text: str) -> str:
    """"ruled out", "not practicing", ...: the absence a note reports."""
    for flag, phrase in _ABSENCE_PHRASE.items():
        if flag in flags:
            return phrase
    m = _ABSENCE_RE.search(text)
    cue = m.group(0).lower() if m else ""
    if "suspen" in cue:
        return "suspended"
    if "doubtful" in cue:
        return "doubtful"
    if cue == "ir" or "injured reserve" in cue:
        return "on injured reserve"
    if any(w in cue for w in ("practic", "particip", "present", "seen", "spotted", "dnp")):
        return "not practicing"
    if "sidelined" in cue:
        return "sidelined"
    return "out"


def _related(pos: str | None, subject_pos: str | None) -> bool:
    if not pos or not subject_pos:
        return False  # an unknown spot: not enough to say he inherits the work
    return subject_pos in _RELATED.get(pos, {pos})


def _lookup_position(db, name: str, team: str | None) -> str | None:
    """The position of the one athlete with this name on this team, if the
    item did not say (a general article's "<Team>' <Player>:" title)."""
    if db is None or not team:
        return None
    from .opportunity_tools import norm_name
    try:
        rows = db.search_athletes_by_name(name, limit=10)
    except Exception as e:
        logger.debug(f"athlete lookup for {name!r} failed: {e}")
        return None
    positions = {(r.get("position") or "").upper() for r in rows
                 if norm_name(r.get("full_name")) == norm_name(name)
                 and normalize_team(r.get("team_id")) == team}
    return positions.pop() if len(positions) == 1 else None


def unavailable_players(db, roles: dict[str, str], athletes: dict) -> set[str]:
    """The players among ``roles`` who will not play this week (Out / IR / PUP
    / suspended / Doubtful: `misses_this_week`), by the injury report or
    Sleeper's own status."""
    try:
        injury_index = build_injury_index(db.get_all_current_injuries())
    except Exception as e:
        logger.debug(f"injury index unavailable: {e}")
        injury_index = {}
    out = set()
    for pid in roles:
        row = athletes.get(pid)
        if not row:
            continue
        report = find_report(row, injury_index, row.get("team_id")) if injury_index else None
        statuses = ((report or {}).get("injury_status"), sleeper_injury_status(row))
        if any(misses_this_week(s) for s in statuses):
            out.add(pid)
    return out


async def news_items(since_dt: datetime, roles: dict[str, str], athletes: dict,
                     errors: dict, db=None, unavailable: set[str] | None = None) -> list[dict]:
    """ESPN and CBS items published since ``since_dt``, attributed to the
    player they are about.

    An item whose subject is another player (CBS "Ravens' Rashod Bateman:
    ..." names Zay Flowers in its body) is not that player's news -- unless
    the subject is a same-team teammate at a related position who will miss
    time: that is ``teammate_news`` for the player who inherits the work. A
    roundup (preview, picks, pickups, rankings) is about nobody in particular
    and is left out. Importance follows what the item says (:func:`news_signal`).
    """
    from . import cbs_fantasy_tools, nfl_tools
    from .opportunity_tools import norm_name
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
    for (label, _, feed_key), resp in zip(sources, responses, strict=True):
        if isinstance(resp, BaseException):
            errors[f"{label.lower()}_news"] = str(resp)
            continue
        resp = resp or {}
        if resp.get("success") is False:
            errors[f"{label.lower()}_news"] = resp.get("error") or "unavailable"
        feeds += [(label, a) for a in resp.get(feed_key) or []]

    items, seen = [], set()
    for source, article in feeds:
        published = _parse_time(article.get("published"))
        # An undated item cannot be placed before or after the last check, so
        # it is left out rather than repeated every day.
        if published is None or published < since_dt:
            continue
        headline = (article.get("headline") or "").strip()
        description = article.get("description") or ""
        text = " ".join(filter(None, [article.get("player"), headline, description]))
        subject, subject_team, subject_pos = news_subject(article)
        if subject is None and _ROUNDUP_RE.search(headline):
            continue  # a preview / pickups list names players; it is not about them
        signal_text = " ".join(filter(None, [headline, description]))
        signal: tuple[str, list[str]] | None = None
        for pid, pattern in patterns:
            key = (pid, headline.lower())
            if key in seen or not pattern.search(text):
                continue
            row = athletes.get(pid) or {}
            label = _label(pid, athletes)
            role = roles[pid]
            base = _ROLE_WEIGHT[role] - 20
            signal = signal or news_signal(signal_text)
            if subject is not None and norm_name(subject) != norm_name(row.get("full_name")):
                # About someone else: only a same-team teammate's absence at a
                # related spot is kept (the work it frees up) -- and only for a
                # player who plays this week himself.
                if pid in (unavailable or ()):
                    continue
                team = normalize_team(row.get("team_id") or row.get("team"))
                pos = (row.get("position") or "").upper() or None
                if subject_pos is None and team and subject_team == team:
                    subject_pos = _lookup_position(db, subject, subject_team) or ""
                if (not team or subject_team != team or not _related(pos, subject_pos)
                        or signal[0] != "absence"):
                    continue
                seen.add(key)
                items.append(_item(
                    "teammate_news", base * TEAMMATE_MULT,
                    f"{label}: teammate {subject} {_absence_phrase(signal[1], signal_text)}"
                    f" ({headline or description[:120]})",
                    player=label, role=role, source=source, relation="teammate",
                    subject_player=subject, signal=signal[0], flags=signal[1] or None,
                    headline=headline or None, at=published.isoformat(),
                ))
                continue
            seen.add(key)
            # A general article that names the player only in its body: a mention.
            mention = subject is None and not pattern.search(headline)
            importance = base * SIGNAL_MULT[signal[0]] * (MENTION_MULT if mention else 1.0)
            items.append(_item(
                "news", max(1.0, importance),
                f"{label}: {headline or description[:120]}",
                player=label, role=role, source=source,
                relation="mention" if mention else "self", signal=signal[0],
                flags=signal[1] or None, at=published.isoformat(),
            ))
    return items


async def transaction_items(league_id: str, weeks: list[int], since_dt: datetime,
                            my_rid: int, opp_rid: int | None, owners: dict[int, str | None],
                            db, errors: dict) -> list[dict]:
    """Processed adds, drops and trades since ``since_dt``."""
    from . import sleeper_tools
    txs: list[dict] = []
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

    items = []
    for b in trending_backups(trending, rows, starters, athletes):
        tid, pid, count = b["player_id"], b["starter_id"], b["adds"]
        owner = rostered.get(tid)
        status = ("free_agent" if owner is None else "yours" if owner == my_rid
                  else "rostered")
        items.append(_item(
            "trending_backup",
            30 + (15 if status == "free_agent" else 0) + min(15, count / 2000),
            f"{_label(tid, rows)} ({b['position']}, {b['team']}) trending +{count} adds — "
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
    assert mine is not None  # find_roster returns a roster whenever there is no error
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
    starters = [s for s in sleeper_tools.set_starters(mine, my_matchup)[0] if s and s != "0"]
    opp_starters = [s for s in (sleeper_tools.set_starters(opp_roster, opp_matchup)[0] if opp_roster
                                else (opp_matchup or {}).get("starters") or [])
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

    owners: dict[int, str | None] = {}
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
        news_items(since_dt, roles, athletes, errors, db,
                   unavailable_players(db, roles, athletes)),
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
