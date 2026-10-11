"""Athlete cache tools (fetch, lookup, search, by team) and the refresh_data admin tool.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import athlete_tools
from ..config import validate_string_input
from ..metrics import timing_decorator
from . import _context

# =============================================================================
# ATHLETE TOOLS
# =============================================================================

# Minimum age of the athletes table before fetch_athletes re-downloads the
# ~5 MB Sleeper dump (the prefetch loop refreshes it daily on its own).
_ATHLETES_MIN_REFRESH_SECONDS = 6 * 60 * 60


def _seconds_since(iso_ts: str | None) -> float | None:
    from datetime import UTC, datetime

    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(iso_ts))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds()


@timing_decorator("fetch_athletes", tool_type="athlete")
async def fetch_athletes() -> dict:
    """Fetch all NFL players from Sleeper API and store in database.

    Rate-limited: when the stored athletes are younger than 6h the call
    returns the cached state (cached=true, note) without re-downloading.

    Returns: {athletes_count, last_updated, cached?, note?, success, error?}
    Example: fetch_athletes()
    """
    db = _context.get_db()
    try:
        last_updated = db.get_last_updated() if db is not None else None
    except Exception:
        last_updated = None
    age = _seconds_since(last_updated)
    if age is not None and 0 <= age < _ATHLETES_MIN_REFRESH_SECONDS:
        return {
            "athletes_count": db.get_athlete_count(),
            "last_updated": last_updated,
            "cached": True,
            "note": (
                f"Athletes were refreshed {int(age // 60)} min ago; refreshes are "
                f"limited to once per {_ATHLETES_MIN_REFRESH_SECONDS // 3600}h"
            ),
            "success": True,
        }
    return await athlete_tools.fetch_athletes(db)


@timing_decorator("refresh_data", tool_type="admin")
async def refresh_data(
    scope: list[str] | None = None,
    force: bool = False,
    background: bool = False,
    job_id: str | None = None,
) -> dict:
    """Refresh cached feeds NOW — when injuries/athletes/practice data are stale.

    The background prefetch stops while the host sleeps; tools then label their
    data stale (`data_freshness`, `stale` warnings). This runs the same fetchers
    the prefetch uses and writes the same way (injury reports pruned only for
    completely crawled teams), even when NFL_MCP_ADVANCED_ENRICH is off.

    Parameters:
        scope: any of "injuries", "practice", "athletes", "schedule", "snaps",
            "usage", "accuracy" (grade finished weeks' logged projections for
            get_projection_accuracy), "news" (player news: ESPN fantasy feed,
            NBC Sports / Rotoworld, CBS; read by get_player_news and the news
            flags) (default ["injuries", "practice"])
        force: refresh even a feed younger than its minimum age
            (15 min for injuries/practice/news, 6h for athletes)
        background: start the refresh and return a job_id at once — an injury
            crawl is ~1900 ESPN requests and takes a few minutes
        job_id: poll a background refresh (other parameters are ignored)

    Returns: {
        job_id, status (running|done|error), season, week,
        scopes {scope: {status (ok|error|skipped_fresh|already_running),
                fetched, written, duration_s, error?}},
        freshness_before, freshness {feed: {updated_at, age_hours}} for
            injuries, athletes, practice_status, schedule, snaps and news,
        duration_s, success
    }

    Example: refresh_data(scope=["injuries", "athletes"])
    Example: refresh_data(scope=["injuries"], background=True) then
             refresh_data(job_id="...")
    """
    from .. import data_refresh

    if job_id is not None:
        try:
            job_id = validate_string_input(job_id, 'job_id', max_length=32, required=True)
        except ValueError as e:
            return {"success": False, "error": f"Invalid input: {e!s}"}
    if isinstance(scope, str):
        scope = [s for s in scope.replace(",", " ").split() if s]
    return await data_refresh.refresh_data(
        scope=scope, force=bool(force), background=bool(background), job_id=job_id, db=_context.get_db())


@timing_decorator("lookup_athlete", tool_type="athlete")
def lookup_athlete(athlete_id: str) -> dict:
    """Look up an athlete by their ID.

    Parameters:
        athlete_id (str, required): The unique identifier for the athlete.
    Returns: {athlete, found, error?}
    Example: lookup_athlete(athlete_id="4034")
    """
    return athlete_tools.lookup_athlete(_context.get_db(), athlete_id)


@timing_decorator("search_athletes", tool_type="athlete")
def search_athletes(name: str, limit: int | None = 10) -> dict:
    """Search for athletes by name (partial match supported).

    Parameters:
        name (str, required): Name or partial name to search for.
        limit (int, default 10, range 1-50): Maximum number of results.
    Returns: {athletes: [...], count, search_term, error?}
    Example: search_athletes(name="Mahomes", limit=5)
    """
    return athlete_tools.search_athletes(_context.get_db(), name, limit)


@timing_decorator("get_athletes_by_team", tool_type="athlete")
def get_athletes_by_team(team_id: str) -> dict:
    """Get all athletes for a specific team.

    Parameters:
        team_id (str, required): The team identifier (e.g., "SF", "DAL", "NE").
    Returns: {athletes: [...], count, team_id, error?}
    Example: get_athletes_by_team(team_id="KC")
    """
    return athlete_tools.get_athletes_by_team(_context.get_db(), team_id)
