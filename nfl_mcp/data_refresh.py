"""Manual data refresh: the prefetch loop's feeds, on demand.

The background prefetch (``server._prefetch_loop``) keeps injuries, practice
reports, athletes, schedule and snaps current while the host is awake. A
sleeping laptop stops it: after a night the athletes table was 43h old and the
injury reports 10h, with nothing to do about it short of a restart. Running the
fetchers by hand did not help either -- they are gated by
``NFL_MCP_ADVANCED_ENRICH``, which a script without ``.env`` does not have, so
``_fetch_injuries()`` returned 0 rows without a word.

This module is the one code path for those feeds: the prefetch loop calls
:func:`run_scope` per scope on its own schedule (practice not on Sundays ET,
athletes once a day, usage only from week 2), and ``refresh_data`` runs the
same scope functions on demand, regardless of that flag, reporting per-scope
counts, durations and the resulting freshness. Writes are the same either way
(injuries pruned only for completely crawled teams). A scope one caller is
refreshing is skipped by the other (``already_running``). An injury crawl is
~1900 ESPN requests and takes minutes, so a refresh can also run in the
background and be polled by job id.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime

from .health import env_int

logger = logging.getLogger(__name__)

REFRESH_SCOPES = ("injuries", "practice", "athletes", "schedule", "snaps", "usage", "accuracy",
                  "news")
DEFAULT_SCOPES = ("injuries", "practice")

# A feed younger than this is left alone unless ``force``: a second refresh a
# minute after the first only re-crawls the same pages.
MIN_REFRESH_AGE_HOURS = {"injuries": 0.25, "practice": 0.25, "athletes": 6.0, "news": 0.25}
# Scope -> key in NFLDatabase.get_data_freshness().
_FRESHNESS_FEED = {"injuries": "injuries", "practice": "practice_status", "athletes": "athletes",
                   "schedule": "schedule", "snaps": "snaps", "news": "news"}
# Owner name the prefetch loop claims scopes under (see ``run_scope``).
PREFETCH_OWNER = "prefetch"
# NFL regular season, for the schedule look-ahead.
_LAST_REGULAR_WEEK = 18
# Finished background jobs kept for polling.
MAX_KEPT_JOBS = 20

_jobs: dict[str, dict] = {}
# Strong references: a task only referenced by the loop can be collected.
_tasks: dict[str, asyncio.Task] = {}
# scope -> id of the job currently refreshing it.
_scope_owner: dict[str, str] = {}


def _schedule_weeks() -> int:
    return env_int("NFL_MCP_PREFETCH_SCHEDULE_WEEKS", 4)


async def _refresh_injuries(db, season: int, week: int) -> dict:
    from . import sleeper_tools
    injuries, complete = await sleeper_tools._fetch_injuries(with_complete_teams=True, force=True)
    written = 0
    if injuries or complete:
        # As the prefetch: reports a team's complete crawl no longer lists are
        # cleared; partially crawled teams are left alone.
        written = await asyncio.to_thread(
            db.upsert_injuries, injuries, prune_missing=True, complete_teams=complete)
    return {"fetched": len(injuries), "written": written, "complete_teams": len(complete)}


async def _refresh_practice(db, season: int, week: int) -> dict:
    from . import sleeper_tools
    reports = await sleeper_tools._fetch_practice_reports(season, week, db=db, force=True)
    written = await asyncio.to_thread(db.upsert_practice_status, reports) if reports else 0
    return {"fetched": len(reports), "written": written, "week": week}


async def _refresh_athletes(db, season: int, week: int) -> dict:
    from . import athlete_tools
    res = await athlete_tools.fetch_athletes(db)
    if not res.get("success"):
        raise RuntimeError(res.get("error") or "athletes fetch failed")
    return {"fetched": res.get("athletes_count", 0), "written": db.get_athlete_count()}


async def _refresh_schedule(db, season: int, week: int) -> dict:
    from . import sleeper_tools
    weeks = list(range(week, min(week + _schedule_weeks(), _LAST_REGULAR_WEEK + 1)))
    fetched = written = 0
    for wk in weeks:
        rows = await sleeper_tools._fetch_week_schedule(season, wk, force=True)
        fetched += len(rows or [])
        if rows:
            written += await asyncio.to_thread(db.upsert_schedule_games, rows)
    return {"fetched": fetched, "written": written, "weeks": weeks}


async def _refresh_snaps(db, season: int, week: int) -> dict:
    from . import sleeper_tools
    # The current week may not have been played yet; the last one has.
    weeks = [w for w in (week, week - 1) if w >= 1]
    fetched = written = 0
    for wk in weeks:
        rows = await sleeper_tools._fetch_week_player_snaps(season, wk, force=True)
        fetched += len(rows or [])
        if rows:
            written += await asyncio.to_thread(db.upsert_player_week_stats, rows)
    return {"fetched": fetched, "written": written, "weeks": weeks}


async def _refresh_usage(db, season: int, week: int) -> dict:
    from . import sleeper_tools
    # Rolling usage averages read completed weeks: the last one.
    weeks = [week - 1] if week > 1 else []
    fetched = written = 0
    for wk in weeks:
        rows = await sleeper_tools._fetch_weekly_usage_stats(season, wk, force=True)
        fetched += len(rows or [])
        if rows:
            written += await asyncio.to_thread(db.upsert_usage_stats, rows)
    return {"fetched": fetched, "written": written, "weeks": weeks}


async def _refresh_accuracy(db, season: int, week: int) -> dict:
    """Grade finished weeks' logged projections (`projection_accuracy`):
    each week once it is final, and once more after the stat corrections --
    then store each newly graded week's signal review (`signal_review`)."""
    from .projection_accuracy import refresh_accuracy
    out = await refresh_accuracy(db, season)
    return {"fetched": out["fetched"], "written": out["written"], "weeks": out["weeks"],
            "reviewed": out.get("reviewed", [])}


async def _refresh_news(db, season: int, week: int) -> dict:
    """Player news from every enabled source (`news_sources.ingest_news`):
    ESPN's fantasy feed, NBC Sports / Rotoworld and CBS, into ``player_news``."""
    from .news_sources import ingest_news
    out = await ingest_news(db)
    warnings = [f"{s}: {r['health']} -- {r.get('detail') or r.get('error') or ''}".rstrip(" -")
                + (f" (backing off until {r['next_attempt_at']})" if r.get("next_attempt_at")
                   else "")
                for s, r in out["sources"].items() if r.get("health") not in (None, "ok")]
    return {"fetched": out["fetched"], "written": out["written"],
            "unresolved": out["unresolved"], "sources": out["sources"],
            **({"warnings": warnings} if warnings else {})}


_REFRESHERS = {
    "injuries": _refresh_injuries,
    "practice": _refresh_practice,
    "athletes": _refresh_athletes,
    "schedule": _refresh_schedule,
    "snaps": _refresh_snaps,
    "usage": _refresh_usage,
    "accuracy": _refresh_accuracy,
    "news": _refresh_news,
}


def _freshness(db) -> dict:
    try:
        return db.get_data_freshness()
    except Exception as e:
        logger.debug(f"freshness unavailable: {e}")
        return {}


async def _run_scope(scope: str, db, season: int, week: int) -> dict:
    started = time.monotonic()
    try:
        out = await _REFRESHERS[scope](db, season, week)
        out["status"] = "ok"
    except Exception as e:
        logger.warning(f"[Refresh] {scope} failed: {e}")
        out = {"status": "error", "error": str(e)}
    out["duration_s"] = round(time.monotonic() - started, 1)
    return out


async def run_scope(scope: str, db, season: int | None, week: int | None,
                    owner: str = PREFETCH_OWNER) -> dict:
    """Refresh one scope now, unless another caller is already refreshing it.

    The prefetch loop's entry point: the same fetch-and-write as
    ``refresh_data`` (so the two never diverge), with ``owner`` holding the
    scope meanwhile so a manual refresh reports ``already_running`` instead
    of crawling the same pages in parallel -- and vice versa. Never raises.
    """
    current = _scope_owner.get(scope)
    if current:
        return {"status": "already_running", "job_id": current}
    _scope_owner[scope] = owner
    try:
        return await _run_scope(scope, db, season, week)
    finally:
        if _scope_owner.get(scope) == owner:
            _scope_owner.pop(scope, None)


async def _run(job: dict, db, season: int, week: int) -> dict:
    """Refresh the job's scopes concurrently (they hit different upstreams)."""
    started = time.monotonic()
    scopes = job["scopes_to_run"]
    try:
        results = await asyncio.gather(*(_run_scope(s, db, season, week) for s in scopes))
        job["scopes"].update(dict(zip(scopes, results, strict=True)))
    finally:
        for s in scopes:
            if _scope_owner.get(s) == job["job_id"]:
                _scope_owner.pop(s, None)
    job["freshness"] = _freshness(db)
    job["duration_s"] = round(time.monotonic() - started, 1)
    job["finished_at"] = datetime.now(UTC).isoformat()
    job["status"] = ("error" if scopes and all(job["scopes"][s]["status"] == "error" for s in scopes)
                     else "done")
    return job


def _prune_jobs() -> None:
    done = [j for j, v in _jobs.items() if v.get("status") != "running"]
    for job_id in done[:-MAX_KEPT_JOBS] if len(done) > MAX_KEPT_JOBS else []:
        _jobs.pop(job_id, None)
        _tasks.pop(job_id, None)


def _public(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k != "scopes_to_run"}
    out["success"] = job.get("status") != "error"
    return out


async def refresh_data(scope: list[str] | None = None, force: bool = False,
                       background: bool = False, job_id: str | None = None,
                       db=None) -> dict:
    """Run the prefetch loop's refreshes now. See the module docstring."""
    if job_id:
        job = _jobs.get(job_id)
        if job is None:
            return {"success": False, "error": f"Unknown job_id {job_id!r}",
                    "jobs": sorted(_jobs)}
        return _public(job)

    scopes = [str(s).strip().lower() for s in (scope or DEFAULT_SCOPES) if str(s).strip()]
    unknown = sorted(set(scopes) - set(REFRESH_SCOPES))
    if unknown:
        return {"success": False,
                "error": f"Unknown scope(s) {unknown}; valid: {list(REFRESH_SCOPES)}"}
    scopes = list(dict.fromkeys(scopes))
    if db is None:
        from .database import get_shared_db
        db = get_shared_db()

    from .week_context import current_season_week
    state = await current_season_week(db)
    season, week = int(state["season"]), int(state["week"])

    before = _freshness(db)
    job = {
        "job_id": uuid.uuid4().hex[:12],
        "status": "running",
        "season": season,
        "week": week,
        "force": force,
        "started_at": datetime.now(UTC).isoformat(),
        "freshness_before": before,
        "scopes": {},
    }
    to_run = []
    for s in scopes:
        owner = _scope_owner.get(s)
        if owner:
            job["scopes"][s] = {"status": "already_running", "job_id": owner}
            continue
        age = (before.get(_FRESHNESS_FEED.get(s, "")) or {}).get("age_hours")
        floor = MIN_REFRESH_AGE_HOURS.get(s)
        if not force and age is not None and floor is not None and age < floor:
            job["scopes"][s] = {"status": "skipped_fresh", "age_hours": age,
                                "note": f"younger than {floor}h; pass force=true to refresh anyway"}
            continue
        to_run.append(s)
        _scope_owner[s] = job["job_id"]
    job["scopes_to_run"] = to_run
    _jobs[job["job_id"]] = job
    _prune_jobs()

    if background and to_run:
        _tasks[job["job_id"]] = asyncio.create_task(_run(job, db, season, week))
        out = _public(job)
        out["note"] = (f"Running in the background; poll with refresh_data(job_id="
                       f"\"{job['job_id']}\"). An injury crawl takes a few minutes.")
        return out
    return _public(await _run(job, db, season, week))
