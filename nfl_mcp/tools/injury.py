"""Injury report, injury trends (practice + timeline) and game-day inactives.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from .. import sleeper_tools
from ..metrics import timing_decorator
from ..teams import normalize_team
from . import _context
from ._context import _current_season_week

if TYPE_CHECKING:
    from ..database import NFLDatabase

logger = logging.getLogger(__name__)


# =============================================================================
# INJURY REPORT TOOLS (ESPN reports, practice reports, status timeline)
# =============================================================================

@timing_decorator("get_injury_report", tool_type="injury")
async def get_injury_report(
    teams: list[str] | None = None,
    player_ids: list[str] | None = None,
    min_confidence: int | None = None,
    severity: int | None = None,
    since: str | None = None,
    include_practice: bool | None = True,
    limit: int | None = None,
    use_cache: bool | None = True,
    team_ids: list[str] | None = None,
    include_healthy: bool | None = False,
) -> dict:
    """Current injury report ("who is hurt"): all teams, some teams, or players.

    Rows come from ESPN's injury API (cached with an adaptive TTL), each with a
    status, severity (1-5) and this week's real practice line. For "what
    CHANGED" use get_injury_trends; for gameday inactives get_gameday_inactives.

    `confidence` is a source-agreement score. Only ESPN is wired in today (the
    CBS injury source is not implemented), so every row is single-source and
    confidence is uniform; min_confidence is kept for when a second source lands.

    Parameters:
        teams: Team abbreviations, any spelling (e.g. ["KC", "PHI"]); default all
        player_ids: ESPN player ids to look up individually (instead of teams)
        min_confidence: Keep rows with confidence >= this (0-100)
        severity: Keep rows with severity >= this (1 minor .. 5 IR/season-ending;
            3+ = likely to miss games)
        since: ISO date/time; keep rows reported on or after it
        include_practice: Attach this week's practice report (default True)
        limit: Max rows (default all)
        use_cache: Use cached reports (default True)
        team_ids: Deprecated alias of `teams`
        include_healthy: Also return healthy rows (Active/Probable/FP,
            severity 1). ESPN's list is every player's latest report, so it is
            ~2600 rows league-wide, most of them recovered; default False.
            Ignored for player_ids lookups, which always return the row.

    Returns: {
        injuries: [{
            player_id, player_name, team_id, position,
            injury_status, injury_type, injury_description,
            game_status, severity (1-5), confidence (0-100),
            sources, date_reported,
            practice_status (DNP/LP/FP/REST or null), practice_pattern
            ("DNP-LP-FP"), practice_trend (improving/worsening/steady/
            single_report), practice_days [{date, day, status, source}],
            practice_source ("nfl.com" official report, "espn_news" dated
            news note, or null = no report published — never inferred)
        }],
        total_injuries, filters, cache_used, practice_week,
        healthy_excluded (rows dropped as healthy), success, error?
    }

    Example: get_injury_report(teams=["KC", "PHI"])
    Example: get_injury_report(severity=3, since="2026-09-20")
    Example: get_injury_report(player_ids=["4428633", "4241479"])
    """
    from ..injury_service import InjuryAggregator, get_injury_reports, get_injury_reports_bounded

    async def _reports(team_arg):
        # A cached read waits for a cold crawl only so long, then answers from
        # the stored reports (the crawl finishes in the background).
        if use_cache_val:
            rows, meta = await get_injury_reports_bounded(teams=team_arg, db=_context.get_db())
            return rows, meta
        return await get_injury_reports(teams=team_arg, db=_context.get_db(), use_cache=False), None

    try:
        use_cache_val = bool(use_cache) if use_cache is not None else True
        team_list = teams or team_ids
        results = []
        crawl_meta = None
        rate_note = None
        if not use_cache_val and not player_ids:
            use_cache_val, rate_note = _allow_uncached_injury_crawl(team_list)

        if player_ids:
            async with InjuryAggregator(db=_context.get_db()) as aggregator:
                for pid in player_ids[:50]:  # Limit to 50 players
                    injury = await aggregator.get_player_injury(str(pid))
                    if injury:
                        results.append(injury.to_dict())
        elif team_list:
            valid_teams = [normalize_team(t) or t.upper() for t in team_list[:32]
                           if isinstance(t, str) and len(t) <= 5]
            if valid_teams:
                results, crawl_meta = await _reports(valid_teams)
        else:
            results, crawl_meta = await _reports(None)

        healthy_excluded = 0
        if not include_healthy and not player_ids:
            before = len(results)
            results = [r for r in results if not _is_healthy_report(r)]
            healthy_excluded = before - len(results)

        filters: dict[str, Any] = {}
        if min_confidence is not None:
            min_conf = max(0, min(100, int(min_confidence)))
            filters["min_confidence"] = min_conf
            results = [r for r in results if (r.get("confidence") or 0) >= min_conf]
        if severity is not None:
            min_sev = max(1, min(5, int(severity)))
            filters["severity"] = min_sev
            results = [r for r in results if (r.get("severity") or 0) >= min_sev]
        if since:
            cutoff = str(since).strip()
            filters["since"] = cutoff
            # ISO strings compare chronologically; a bare date covers the whole day.
            results = [r for r in results if str(r.get("date_reported") or "") >= cutoff]
        if limit is not None:
            results = results[:max(1, int(limit))]

        practice_week = None
        if include_practice is not False and results:
            practice_week = await _attach_practice(results)

        return {
            "injuries": results,
            "total_injuries": len(results),
            "filters": filters,
            "cache_used": use_cache_val,
            "practice_week": practice_week,
            "healthy_excluded": healthy_excluded,
            **({"note": rate_note} if rate_note else {}),
            **({"partial": True, "timed_out_sources": crawl_meta["timed_out_sources"],
                "stale": True} if crawl_meta and crawl_meta.get("partial") else {}),
            "success": True
        }

    except Exception as e:
        return {
            "injuries": [],
            "total_injuries": 0,
            "cache_used": False,
            "success": False,
            "error": str(e)
        }


# use_cache=False re-crawls ESPN (one request per team + one per injury):
# honour it at most once per window per team set, else serve the cache.
_INJURY_UNCACHED_MIN_INTERVAL_SECONDS = 15 * 60
_last_uncached_injury_crawl: dict[str, float] = {}


def _allow_uncached_injury_crawl(team_list) -> tuple[bool, str | None]:
    """(use_cache, note): whether this use_cache=False call must use the cache."""
    import time

    key = ",".join(sorted(str(t).upper() for t in team_list)) if team_list else "*"
    now = time.monotonic()
    last = _last_uncached_injury_crawl.get(key)
    if last is not None and now - last < _INJURY_UNCACHED_MIN_INTERVAL_SECONDS:
        wait_min = int((_INJURY_UNCACHED_MIN_INTERVAL_SECONDS - (now - last)) // 60) + 1
        return True, (
            "use_cache=False ignored: a fresh crawl ran less than "
            f"{_INJURY_UNCACHED_MIN_INTERVAL_SECONDS // 60} min ago; served cached "
            f"reports (fresh crawl allowed again in ~{wait_min} min)"
        )
    _last_uncached_injury_crawl[key] = now
    return False, None


def _is_healthy_report(row: dict) -> bool:
    """An injury-report row that says the player is healthy (severity 1).

    "Unknown" (ESPN sent no status) is kept: it is not a clean bill of health.
    """
    from ..injury_status import is_healthy, normalize
    status = row.get("injury_status")
    if normalize(status) == "Unknown":
        return False
    if is_healthy(status):
        return True
    sev = row.get("severity")
    return isinstance(sev, int | float) and sev <= 1


PRACTICE_REFRESH_WAIT_SECONDS = 6.0


async def _attach_practice(injuries: list[dict]) -> dict:
    """Add this week's reported practice line to each injury row (in place).

    Refreshes the stored reports at most hourly first, so the tool does not
    depend on the prefetch loop. A player without a report gets nulls.
    """
    from ..practice_reports import lookup_practice, practice_fields, refresh_practice_reports
    from ..upstream import single_flight, wait_bounded

    db = _context.get_db()
    season, week = await _current_season_week()
    try:
        # nfl.com + ESPN pages: bounded, the refresh finishes in the background.
        await wait_bounded(single_flight(f"practice_refresh:{season}:{week}",
                                         lambda: refresh_practice_reports(db, season, week)),
                           PRACTICE_REFRESH_WAIT_SECONDS)
    except Exception as e:
        logger.debug(f"practice refresh failed: {e}")
    reported = 0
    for inj in injuries:
        practice = lookup_practice(db, inj.get("player_name"), inj.get("team_id"),
                                   season=season, week=week)
        inj.update(practice_fields(practice))
        inj["practice_days"] = (practice or {}).get("days") or []
        if practice and practice.get("game_status"):
            inj["practice_report_game_status"] = practice["game_status"]
        reported += 1 if practice else 0
    return {"season": season, "week": week, "players_with_report": reported}


@timing_decorator("get_injury_trends", tool_type="injury")
async def get_injury_trends(
    lookback_hours: int | None = 168,
    teams: list[str] | None = None,
    direction: str | None = None,
    limit: int | None = 50,
) -> dict:
    """Get injury status CHANGES over a window - who got worse or recovered.

    Reads the recorded timeline rather than the current snapshot, so it answers
    "what moved since I last looked" instead of "who is hurt". A player's first
    sighting has no previous status and is reported as ``new``.

    Parameters:
        lookback_hours: Window to look back (default 168 = 7 days)
        teams: Team abbreviations to filter
        direction: "worse", "better" or "new" to filter; omit for all
        limit: Max changes to return

    Returns: {
        changes: [{player_name, team_id, position, previous_status,
                   injury_status, direction, severity_delta, recorded_at, ...}],
        total_changes, lookback_hours, success, error?
    }

    Example: get_injury_trends()
    Example: get_injury_trends(lookback_hours=48, direction="worse")
    """
    from datetime import UTC, datetime, timedelta

    from ..injury_service import DEFAULT_SEVERITY, STATUS_SEVERITY, status_severity

    try:
        hours = max(1, min(int(lookback_hours or 168), 24 * 30))
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        teams_list = [normalize_team(t) or t.upper() for t in (teams or [])[:10] if isinstance(t, str)]
        max_rows = max(1, min(int(limit or 50), 500))

        # The direction filter runs in SQL: applying it to an already-truncated
        # page returns nothing whenever the window opens with a bulk backfill.
        rows = cast("NFLDatabase", _context.get_db()).get_injury_status_changes(
            since=since, teams=teams_list or None, limit=max_rows,
            direction=direction, severity_map=STATUS_SEVERITY,
        )

        changes = []
        for row in rows:
            prev = row.get("previous_status")
            new_sev = status_severity(row.get("injury_status")) or int(DEFAULT_SEVERITY)
            if prev is None:
                row_direction, delta = "new", None
            else:
                old_sev = status_severity(prev) or int(DEFAULT_SEVERITY)
                delta = new_sev - old_sev
                # Same severity bucket with a different label (e.g. a changed
                # body part) is a re-report, not a move in either direction.
                row_direction = "worse" if delta > 0 else "better" if delta < 0 else "lateral"
            changes.append({**row, "direction": row_direction, "severity_delta": delta})

        # No post-filter on `direction`: the query already applied it, and doing
        # it again here is what capped the result at "whatever survived the page".

        return {
            "changes": changes,
            "total_changes": len(changes),
            "lookback_hours": hours,
            "success": True,
            "error": None,
        }

    except Exception as e:
        return {
            "changes": [],
            "total_changes": 0,
            "lookback_hours": lookback_hours,
            "success": False,
            "error": f"Failed to get injury trends: {e}",
        }


# get_gameday_inactives answers within about this long (plus the starters
# lookup's floor); feeds that need longer are listed in timed_out_sources.
GAMEDAY_INACTIVES_BUDGET_SECONDS = 20.0
GAMEDAY_INJURY_WAIT_SECONDS = 6.0
GAMEDAY_STARTERS_MIN_SECONDS = 5.0


@timing_decorator("get_gameday_inactives", tool_type="injury")
async def get_gameday_inactives(
    teams: list[str] | None = None,
    severity_threshold: int | None = 3,
    league_id: str | None = None,
    roster_id: int | None = None,
    season: int | None = None,
    week: int | None = None,
) -> dict:
    """Get gameday inactives: the published list once it is out, the injury
    report's likely-outs before that.

    Teams declare inactives ~90 minutes before kickoff. For games starting
    within 2h, in progress or final, the published inactives are read
    (ESPN gameday notes "X is inactive for Sunday's game", plus Sleeper's
    `Inactive` status) and marked `official: true`. For every other team — or
    a team whose list has not been published yet — the old behaviour is the
    fallback: injuries at or above `severity_threshold`, marked
    `official: false` with `basis: "injury_report_severity"`. That is a
    forecast, not the inactive list.

    Severity scale (fallback only):
    - 1: Minor (day-to-day)
    - 2: Questionable (game-time decision)
    - 3: Moderate (expected to miss 1-2 weeks)
    - 4: Significant (multi-week absence)
    - 5: Severe (IR/season-ending)

    Parameters:
        teams: Team abbreviations to filter
        severity_threshold: Min severity for the fallback (3+ = likely out)
        league_id, roster_id (optional): pass both to flag YOUR starters who
            are inactive (or at risk before the list is out)
        season, week (optional): default to the current NFL week

    Returns: {
        inactives: [{player_name, team_id, injury_status, official, basis,
                     source, note?, severity?, confidence?}],
        total_inactives, severity_threshold_used,
        mode: "official" | "mixed" | "fallback_injury_report",
        official_published_teams, pending_teams, games {team: {kickoff, phase}},
        confirmed_active [{player_name, team_id, note}],
        my_starters? {inactive, at_risk, confirmed_active, not_yet_published},
        success, error?
    }

    Example: get_gameday_inactives()
    Example: get_gameday_inactives(league_id="1312017357782155264", roster_id=7)
    Example: get_gameday_inactives(teams=["KC", "SF"], severity_threshold=4)
    """
    from ..gameday_inactives import get_official_inactives
    from ..injury_service import get_injury_reports_bounded
    from ..opportunity_tools import norm_name
    from ..teams import normalize_team
    from ..upstream import Budget

    try:
        budget = Budget(GAMEDAY_INACTIVES_BUDGET_SECONDS)
        threshold = int(severity_threshold) if severity_threshold else 3
        threshold = max(1, min(5, threshold))

        teams_list = [normalize_team(t) or t.upper() for t in (teams or [])[:10] if isinstance(t, str)]
        if not season or not week:
            cur_season, cur_week = await _current_season_week()
            season, week = season or cur_season, week or cur_week

        # The published list and the injury report are independent reads:
        # both at once, each bounded (a slow feed keeps loading in the
        # background and the answer says so). Inline and one after another
        # they ran past the MCP client's 300 s on 2026-09-27.
        empty_official: dict[str, Any] = {"games": {}, "window_teams": [], "inactives": [], "confirmed_active": []}

        async def _official():
            if not (season and week):
                return empty_official
            try:
                return await get_official_inactives(_context.get_db(), int(season), int(week), teams_list or None)
            except Exception as e:
                logger.warning(f"official inactives unavailable: {e}")
                return empty_official

        official, (injuries, injury_meta) = await asyncio.gather(
            _official(),
            get_injury_reports_bounded(teams=teams_list or None, db=_context.get_db(),
                                       wait=budget.remaining(cap=GAMEDAY_INJURY_WAIT_SECONDS)),
        )
        published = sorted({r["team_id"] for r in official["inactives"]})
        pending = sorted(set(official.get("window_teams") or []) - set(published))
        timed_out = list(official.get("timed_out_sources") or []) + injury_meta["timed_out_sources"]

        inactives = []
        for row in official["inactives"]:
            inactives.append({
                "player_id": row.get("player_id"),
                "player_name": row.get("player_name"),
                "team_id": row.get("team_id"),
                "position": row.get("position"),
                "injury_status": "Inactive",
                "official": True,
                "basis": "published_inactives",
                "source": row.get("source"),
                "note": row.get("note"),
                "posted": row.get("posted"),
            })

        # Fallback: severity filter, for teams without a published list.
        for inj in injuries:
            team = normalize_team(inj.get("team_id"))
            if team in published:
                continue
            severity = inj.get("severity")
            if severity and severity >= threshold:
                inactives.append({
                    "player_id": inj.get("player_id"),
                    "player_name": inj.get("player_name"),
                    "team_id": inj.get("team_id"),
                    "position": inj.get("position"),
                    "injury_status": inj.get("injury_status"),
                    "injury_type": inj.get("injury_type"),
                    "game_status": inj.get("game_status"),
                    "severity": severity,
                    "confidence": inj.get("confidence", 50),
                    "official": False,
                    "basis": "injury_report_severity",
                    "source": "injury_report",
                })

        # Official first, then by severity (highest first), then confidence
        inactives.sort(key=lambda x: (not x["official"], -(x.get("severity") or 6),
                                      -(x.get("confidence") or 100)))

        mode = ("official" if published and not any(not r["official"] for r in inactives)
                else "mixed" if published else "fallback_injury_report")
        result = {
            "inactives": inactives,
            "total_inactives": len(inactives),
            "severity_threshold_used": threshold,
            "mode": mode,
            "official_published_teams": published,
            "pending_teams": pending,
            "games": official.get("games") or {},
            # Which feeds answered, and how current the Sleeper read was.
            "sources_checked": official.get("sources_checked") or [],
            "sleeper_freshness": official.get("sleeper_freshness"),
            "confirmed_active": [
                {"player_name": r["player_name"], "team_id": r["team_id"], "note": r.get("note")}
                for r in official.get("confirmed_active") or []
            ],
            "season": season,
            "week": week,
            # A feed that did not answer within the budget: its rows are from
            # an older copy (or missing); the read finishes in the background.
            "partial": bool(timed_out or official.get("partial")),
            "timed_out_sources": timed_out,
            **({"stale_sources": official["stale_sources"]} if official.get("stale_sources") else {}),
            **({"injury_report_stale": True} if injury_meta["stale"] else {}),
            "success": True,
        }
        if mode == "fallback_injury_report":
            result["fallback_note"] = (
                "No inactive list is published yet (they come ~90 minutes before "
                "kickoff). These are injury-report designations at or above the "
                "severity threshold, not official inactives."
            )

        if league_id and roster_id is not None and week:
            try:
                result["my_starters"] = await asyncio.wait_for(_flag_my_starters(
                    str(league_id), int(roster_id), int(week), inactives,
                    official.get("confirmed_active") or [], set(pending), official.get("games") or {},
                    norm_name,
                ), budget.remaining(floor=GAMEDAY_STARTERS_MIN_SECONDS))
            except TimeoutError:
                result["my_starters"] = {"error": "timed out loading your starters"}
                result["partial"] = True
                result["timed_out_sources"].append("sleeper_starters")
        return result

    except Exception as e:
        return {
            "inactives": [],
            "total_inactives": 0,
            "severity_threshold_used": severity_threshold,
            "success": False,
            "error": str(e)
        }


async def _flag_my_starters(league_id, roster_id, week, inactives, confirmed_active,
                            pending, games, norm_name) -> dict:
    """Which of the roster's current starters are inactive / at risk."""
    from ..teams import normalize_team

    starters: list[str] = []
    try:
        matchups = (await sleeper_tools.get_matchups(league_id, week) or {}).get("matchups") or []
        mine = next((m for m in matchups if m.get("roster_id") == roster_id), None)
        state = await sleeper_tools.load_rosters(league_id, "lineup")
        roster, _ = sleeper_tools.find_roster(state["rosters"], league_id, roster_id, None)
        # The matchup's starters only when they agree with the roster: a copy
        # from before a trade still starts the players who left.
        raw = (sleeper_tools.set_starters(roster, mine)[0] if roster
               else [str(p) for p in (mine or {}).get("starters") or []])
        starters = [p for p in raw if p and p != "0"]
    except Exception as e:
        return {"error": f"could not load starters: {e}"}
    athletes = cast("NFLDatabase", _context.get_db()).get_athletes_by_ids(starters) if starters else {}

    def key(name, team):
        return norm_name(name), normalize_team(team)

    official = {key(r["player_name"], r["team_id"]): r for r in inactives if r["official"]}
    at_risk = {key(r["player_name"], r["team_id"]): r for r in inactives if not r["official"]}
    active = {key(r["player_name"], r["team_id"]): r for r in confirmed_active}
    out: dict[str, list] = {"inactive": [], "at_risk": [], "confirmed_active": [], "not_yet_published": []}
    for pid in starters:
        row = athletes.get(pid) or {}
        name, team = row.get("full_name"), normalize_team(row.get("team_id"))
        if not name or not team:
            continue
        k = key(name, team)
        entry = {"player_id": pid, "player": name, "team": team,
                 "kickoff": (games.get(team) or {}).get("kickoff")}
        if k in official:
            out["inactive"].append({**entry, "source": official[k]["source"],
                                    "note": official[k].get("note")})
        elif k in active:
            out["confirmed_active"].append({**entry, "note": active[k].get("note")})
        elif k in at_risk:
            out["at_risk"].append({**entry, "injury_status": at_risk[k]["injury_status"],
                                   "basis": "injury_report_severity"})
        if team in pending and k not in official and k not in active:
            out["not_yet_published"].append(entry)
    return out
