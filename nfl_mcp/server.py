#!/usr/bin/env python3
"""
NFL MCP Server - Simplified Architecture

A FastMCP server that provides:
- Health endpoint (non-MCP REST endpoint)
- URL crawling tool (MCP tool for web content extraction)
- NFL news tool (MCP tool for fetching latest NFL news from ESPN)
- NFL teams tool (MCP tool for fetching all NFL teams from ESPN)
- Athlete tools (MCP tools for fetching and looking up NFL athletes from Sleeper API)
- Sleeper API tools (MCP tools for comprehensive fantasy league management):
  - League information, rosters, users, matchups, playoffs
  - Transactions, traded picks, NFL state, trending players
- Waiver wire analysis tools (MCP tools for advanced fantasy football waiver management)
- ConfigManager integration for centralized configuration (Fix #1)
- ContextVar-based DI for database access (Fix #2)
- Extracted health endpoint (Fix #3)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, UTC

from fastmcp import FastMCP

from .config import get_all_rate_limiter_status as _get_all_rate_limiter_status
from .health import health_check as _health_check
from .config_manager import get_config_manager
from .database import NFLDatabase
from . import tool_registry

# Configure logging with INFO level by default
LOG_LEVEL = os.getenv("NFL_MCP_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

logger = logging.getLogger(__name__)

# Load prefetch config once from environment (prefetch is separate from general config)
PREFETCH_ENABLED = os.getenv("NFL_MCP_PREFETCH") == "1"
PREFETCH_INTERVAL_SECONDS = int(os.getenv("NFL_MCP_PREFETCH_INTERVAL", "900"))
PREFETCH_SNAPS_TTL_SECONDS = int(os.getenv("NFL_MCP_PREFETCH_SNAPS_TTL", "900"))
PREFETCH_SCHEDULE_WEEKS = int(os.getenv("NFL_MCP_PREFETCH_SCHEDULE_WEEKS", "4"))

# Global state for prefetch task
_prefetch_task: asyncio.Task | None = None
_shutdown_event: asyncio.Event | None = None


async def _prefetch_loop(nfl_db: NFLDatabase, shutdown_event: asyncio.Event):
    """Background loop to prefetch weekly schedule and player snaps to warm caches.

    Strategy:
      - Determine season/week via get_nfl_state tool (internal call)
      - Fetch schedule for current week + upcoming weeks (stores opponents for all positions)
      - Fetch player snaps (stores usage) with capped volume
      - Sleep until next interval or shutdown
    Controlled by env NFL_MCP_PREFETCH=1.
    """
    if not PREFETCH_ENABLED:
        logger.info("Prefetch loop disabled: NFL_MCP_PREFETCH not set to 1")
        return

    # Import late to avoid circular
    from .sleeper_tools import (
        get_nfl_state,
        _fetch_week_schedule,
        _fetch_week_player_snaps,
        _fetch_injuries,
        _fetch_practice_reports,
        _fetch_weekly_usage_stats,
        ADVANCED_ENRICH_ENABLED,
    )

    if not ADVANCED_ENRICH_ENABLED:
        logger.warning("Prefetch loop disabled: NFL_MCP_ADVANCED_ENRICH not set to 1")
        return

    logger.info(
        f"Prefetch loop started (interval={PREFETCH_INTERVAL_SECONDS}s, "
        f"snaps_ttl={PREFETCH_SNAPS_TTL_SECONDS}s, schedule_weeks={PREFETCH_SCHEDULE_WEEKS})"
    )

    cycle_count = 0
    while not shutdown_event.is_set():
        cycle_count += 1
        cycle_start = datetime.now(UTC)
        logger.info(f"[Prefetch Cycle #{cycle_count}] Starting at {cycle_start.isoformat()}")

        stats = {
            "schedule_inserted": 0,
            "snaps_inserted": 0,
            "injuries_inserted": 0,
            "practice_inserted": 0,
            "usage_inserted": 0,
            "schedule_error": None,
            "snaps_error": None,
            "injuries_error": None,
            "practice_error": None,
            "usage_error": None,
        }

        try:
            state = await get_nfl_state()
            if state.get("success") and state.get("nfl_state"):
                st = state["nfl_state"]
                season_raw = st.get("season") or st.get("league_season")
                week_raw = st.get("week") or st.get("display_week")

                # Convert to int if they're strings
                try:
                    season = int(season_raw) if season_raw is not None else None
                    week = int(week_raw) if week_raw is not None else None
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"[Prefetch Cycle #{cycle_count}] Could not parse season/week: "
                        f"season_raw={season_raw}, week_raw={week_raw}, error={e}"
                    )
                    season = None
                    week = None

                logger.info(f"[Prefetch Cycle #{cycle_count}] NFL State: season={season}, week={week}")

                if season is not None and week is not None and isinstance(season, int) and isinstance(
                    week, int
                ):
                    # Schedule prefetch (current week + upcoming weeks for opponent data)
                    schedule_weeks_to_fetch = list(
                        range(week, min(week + PREFETCH_SCHEDULE_WEEKS, 19))
                    )  # NFL regular season is 18 weeks
                    total_schedule_rows_inserted = 0

                    for schedule_week in schedule_weeks_to_fetch:
                        try:
                            logger.debug(
                                f"[Prefetch Cycle #{cycle_count}] Fetching schedule for "
                                f"season={season}, week={schedule_week}"
                            )
                            sched_rows = await _fetch_week_schedule(season, schedule_week)
                            if sched_rows:
                                inserted = nfl_db.upsert_schedule_games(sched_rows)
                                total_schedule_rows_inserted += inserted
                                logger.info(
                                    f"[Prefetch Cycle #{cycle_count}] Schedule (week {schedule_week}): "
                                    f"{inserted} rows inserted"
                                )
                            else:
                                logger.info(
                                    f"[Prefetch Cycle #{cycle_count}] Schedule (week {schedule_week}): "
                                    "No rows returned"
                                )
                        except Exception as e:
                            stats["schedule_error"] = str(e)
                            logger.error(
                                f"[Prefetch Cycle #{cycle_count}] Schedule fetch (week {schedule_week}) "
                                f"failed: {e}",
                                exc_info=True,
                            )

                    stats["schedule_inserted"] = total_schedule_rows_inserted
                    if total_schedule_rows_inserted > 0:
                        logger.info(
                            f"[Prefetch Cycle #{cycle_count}] Schedule total: "
                            f"{total_schedule_rows_inserted} rows inserted across "
                            f"{len(schedule_weeks_to_fetch)} weeks"
                        )

                    # Snaps prefetch (current week + previous week as fallback)
                    # Current week might not have data yet (games not played)
                    snap_weeks_to_fetch = [week]
                    if week > 1:
                        snap_weeks_to_fetch.append(week - 1)  # Add previous week

                    total_snap_rows_inserted = 0
                    for snap_week in snap_weeks_to_fetch:
                        try:
                            logger.debug(
                                f"[Prefetch Cycle #{cycle_count}] Fetching snaps for "
                                f"season={season}, week={snap_week}"
                            )
                            snap_rows = await _fetch_week_player_snaps(season, snap_week)
                            # Take only first 2000 to avoid huge memory churn
                            if snap_rows:
                                subset = snap_rows[:2000]
                                inserted = nfl_db.upsert_player_week_stats(subset)
                                total_snap_rows_inserted += inserted
                                logger.info(
                                    f"[Prefetch Cycle #{cycle_count}] Snaps (week {snap_week}): "
                                    f"{inserted} rows inserted from {len(snap_rows)} fetched"
                                )
                            else:
                                logger.info(
                                    f"[Prefetch Cycle #{cycle_count}] Snaps (week {snap_week}): "
                                    "No rows returned"
                                )
                        except Exception as e:
                            stats["snaps_error"] = str(e)
                            logger.error(
                                f"[Prefetch Cycle #{cycle_count}] Snaps fetch (week {snap_week}) "
                                f"failed: {e}",
                                exc_info=True,
                            )

                    stats["snaps_inserted"] = total_snap_rows_inserted
                    if total_snap_rows_inserted > 0:
                        logger.info(
                            f"[Prefetch Cycle #{cycle_count}] Snaps total: "
                            f"{total_snap_rows_inserted} rows inserted across "
                            f"{len(snap_weeks_to_fetch)} weeks"
                        )

                    # Injuries prefetch (once per cycle, covers all teams)
                    try:
                        logger.debug(
                            f"[Prefetch Cycle #{cycle_count}] Fetching injury reports for all teams"
                        )
                        injuries = await _fetch_injuries()
                        if injuries:
                            inserted = nfl_db.upsert_injuries(injuries)
                            stats["injuries_inserted"] = inserted
                            logger.info(
                                f"[Prefetch Cycle #{cycle_count}] Injuries: "
                                f"{inserted} rows inserted from {len(injuries)} fetched"
                            )
                        else:
                            logger.info(f"[Prefetch Cycle #{cycle_count}] Injuries: No rows returned")
                    except Exception as e:
                        stats["injuries_error"] = str(e)
                        logger.error(
                            f"[Prefetch Cycle #{cycle_count}] Injuries fetch failed: {e}",
                            exc_info=True,
                        )

                    # Practice reports (Thu-Sat only to capture weekly injury reports)
                    weekday = datetime.now(UTC).weekday()
                    logger.debug(
                        f"[Prefetch Cycle #{cycle_count}] Current weekday: "
                        f"{weekday} ({'Thu' if weekday == 3 else 'Fri' if weekday == 4 else 'Sat' if weekday == 5 else 'Other'})"
                    )
                    if weekday in [3, 4, 5]:  # Thu=3, Fri=4, Sat=5
                        try:
                            logger.debug(
                                f"[Prefetch Cycle #{cycle_count}] Fetching practice reports "
                                f"for season={season}, week={week}"
                            )
                            practice_reports = await _fetch_practice_reports(season, week)
                            if practice_reports:
                                inserted = nfl_db.upsert_practice_status(practice_reports)
                                stats["practice_inserted"] = inserted
                                logger.info(
                                    f"[Prefetch Cycle #{cycle_count}] Practice: "
                                    f"{inserted} rows inserted"
                                )
                            else:
                                logger.info(
                                    f"[Prefetch Cycle #{cycle_count}] Practice: No rows returned "
                                    f"(implementation pending)"
                                )
                        except Exception as e:
                            stats["practice_error"] = str(e)
                            logger.error(
                                f"[Prefetch Cycle #{cycle_count}] Practice fetch failed: {e}",
                                exc_info=True,
                            )
                    else:
                        logger.debug(
                            f"[Prefetch Cycle #{cycle_count}] Practice: Skipped (only runs Thu-Sat)"
                        )

                    # Usage stats (fetch previous week for rolling averages)
                    if week > 1:
                        try:
                            logger.debug(
                                f"[Prefetch Cycle #{cycle_count}] Fetching usage stats for "
                                f"season={season}, week={week-1}"
                            )
                            usage_stats = await _fetch_weekly_usage_stats(season, week - 1)
                            if usage_stats:
                                inserted = nfl_db.upsert_usage_stats(usage_stats)
                                stats["usage_inserted"] = inserted
                                logger.info(
                                    f"[Prefetch Cycle #{cycle_count}] Usage: "
                                    f"{inserted} rows inserted (week {week-1})"
                                )
                            else:
                                logger.info(
                                    f"[Prefetch Cycle #{cycle_count}] Usage: No rows returned"
                                )
                        except Exception as e:
                            stats["usage_error"] = str(e)
                            logger.error(
                                f"[Prefetch Cycle #{cycle_count}] Usage fetch failed: {e}",
                                exc_info=True,
                            )
                    else:
                        logger.debug(
                            f"[Prefetch Cycle #{cycle_count}] Usage: Skipped (week={week}, need week > 1)"
                        )
                else:
                    logger.warning(
                        f"[Prefetch Cycle #{cycle_count}] Invalid season/week: "
                        f"season={season} (type={type(season).__name__}), "
                        f"week={week} (type={type(week).__name__})"
                    )
            else:
                logger.warning(f"[Prefetch Cycle #{cycle_count}] NFL state unavailable or unsuccessful")
        except Exception as e:
            logger.error(
                f"[Prefetch Cycle #{cycle_count}] Iteration error: {e}", exc_info=True
            )

        cycle_end = datetime.now(UTC)
        cycle_duration = (cycle_end - cycle_start).total_seconds()

        logger.info(
            f"[Prefetch Cycle #{cycle_count}] Completed in {cycle_duration:.2f}s - "
            f"Schedule: {stats['schedule_inserted']} rows, "
            f"Snaps: {stats['snaps_inserted']} rows, "
            f"Injuries: {stats['injuries_inserted']} rows, "
            f"Practice: {stats['practice_inserted']} rows, "
            f"Usage: {stats['usage_inserted']} rows"
        )

        if any(
            [
                stats["schedule_error"],
                stats["snaps_error"],
                stats["injuries_error"],
                stats["practice_error"],
                stats["usage_error"],
            ]
        ):
            logger.warning(
                f"[Prefetch Cycle #{cycle_count}] Errors occurred - "
                f"Schedule: {stats['schedule_error'] or 'OK'}, "
                f"Snaps: {stats['snaps_error'] or 'OK'}, "
                f"Injuries: {stats['injuries_error'] or 'OK'}, "
                f"Practice: {stats['practice_error'] or 'OK'}, "
                f"Usage: {stats['usage_error'] or 'OK'}"
            )

        # Run snapshot cleanup once per day (every 96 cycles at 15min interval)
        if cycle_count % 96 == 0:
            try:
                deleted = nfl_db.cleanup_old_snapshots(max_age_days=7)
                total_deleted = sum(deleted.values())
                if total_deleted > 0:
                    logger.info(f"[Prefetch Cycle #{cycle_count}] Cleanup: Deleted {total_deleted} old snapshots")
            except Exception as e:
                logger.warning(f"[Prefetch Cycle #{cycle_count}] Cleanup failed: {e}")

        logger.info(f"[Prefetch Cycle #{cycle_count}] Next cycle in {PREFETCH_INTERVAL_SECONDS}s")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=PREFETCH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


def _get_config() -> dict:
    """Centralized config access (Fix #1).

    Uses ConfigManager as the single source of truth instead of scattering
    os.getenv() calls across multiple modules.
    """
    try:
        cm = get_config_manager()
        return {
            "timeout_total": cm.config.timeout.total,
            "timeout_connect": cm.config.timeout.connect,
            "long_timeout_total": cm.config.long_timeout.total,
            "long_timeout_connect": cm.config.long_timeout.connect,
            "nfl_news_max": cm.config.limits.nfl_news_max,
            "nfl_news_min": cm.config.limits.nfl_news_min,
            "athletes_search_max": cm.config.limits.athletes_search_max,
            "athletes_search_min": cm.config.limits.athletes_search_min,
            "athletes_search_default": cm.config.limits.athletes_search_default,
            "week_min": cm.config.limits.week_min,
            "week_max": cm.config.limits.week_max,
            "round_min": cm.config.limits.round_min,
            "round_max": cm.config.limits.round_max,
            "trending_lookback_min": cm.config.limits.trending_lookback_min,
            "trending_lookback_max": cm.config.limits.trending_lookback_max,
            "trending_limit_min": cm.config.limits.trending_limit_min,
            "trending_limit_max": cm.config.limits.trending_limit_max,
        }
    except Exception:
        return {}


def create_app() -> FastMCP:
    """Create and configure the FastMCP server application.

    - Initializes ConfigManager (Fix #1)
    - Creates NFLDatabase and injects it via ContextVar (Fix #2)
    - Registers all tools from the tool registry
    - Mounts the extracted health endpoint router (Fix #3)
    """
    # --- Fix #1: Initialize ConfigManager (single source of truth) ---
    try:
        _get_config()  # Triggers lazy init of the global ConfigManager
        logger.info("ConfigManager initialized successfully")
    except Exception:
        logger.warning("ConfigManager init failed; using defaults from config.py", exc_info=True)

    # --- Create FastMCP server instance ---
    mcp = FastMCP(name="NFL MCP Server")

    # --- Initialize NFL database ---
    nfl_db = NFLDatabase()

    # --- Fix #2: Inject DB into ContextVar (eliminates global mutable state) ---
    tool_registry.initialize_shared(nfl_db)

    # --- Register all tools from the tool registry ---
    for tool_func in tool_registry.get_all_tools():
        mcp.tool(tool_func)

    # --- Fix #3: Mount extracted health endpoint (separate module) ---
    @mcp.custom_route("/health", methods=["GET"])
    async def _health_endpoint(request):  # type: ignore[assignment]
        return await _health_check()

    return mcp


def _create_prefetch_lifespan(nfl_db: NFLDatabase):
    """Factory function to create lifespan with access to nfl_db instance.

    Handles background prefetch loop startup and graceful shutdown.
    """

    @asynccontextmanager
    async def app_lifespan(app):
        """Lifespan context manager for background prefetch task."""
        global _prefetch_task, _shutdown_event

        if PREFETCH_ENABLED:
            # Import late to avoid circular
            from .sleeper_tools import (
                ADVANCED_ENRICH_ENABLED,
                _fetch_all_team_schedules,
                get_nfl_state,
            )

            if ADVANCED_ENRICH_ENABLED:
                # Run initial startup prefetch (schedules for all 32 teams)
                logger.info("[Startup Prefetch] Running initial cache warm-up...")
                try:
                    # Get current season
                    state = await get_nfl_state()
                    season = 2026  # Default
                    if state.get("success") and state.get("nfl_state"):
                        season_raw = state["nfl_state"].get(
                            "season"
                        ) or state["nfl_state"].get("league_season")
                        try:
                            season = int(season_raw) if season_raw is not None else 2026
                        except (ValueError, TypeError):
                            season = 2026

                    logger.info(
                        f"[Startup Prefetch] Fetching schedules for all 32 teams (season={season})..."
                    )
                    schedules = await _fetch_all_team_schedules(season)

                    if schedules:
                        inserted = nfl_db.upsert_schedule_games(schedules)
                        logger.info(
                            f"[Startup Prefetch] Inserted {inserted} schedule records "
                            f"for {season} season"
                        )
                    else:
                        logger.warning(
                            f"[Startup Prefetch] No schedule data fetched for season {season}"
                        )

                except Exception as e:
                    logger.error(
                        f"[Startup Prefetch] Failed to fetch team schedules: {e}", exc_info=True
                    )

                # Start background prefetch loop
                _shutdown_event = asyncio.Event()
                _prefetch_task = asyncio.create_task(
                    _prefetch_loop(nfl_db, _shutdown_event)
                )
                logger.info("Background prefetch task started")
            else:
                logger.info("Prefetch disabled: NFL_MCP_ADVANCED_ENRICH not enabled")

        yield  # Server running

        # Shutdown
        if _prefetch_task and _shutdown_event:
            logger.info("Stopping prefetch task...")
            _shutdown_event.set()
            await _prefetch_task
            logger.info("Prefetch task stopped")

    return app_lifespan


def main():
    """Main entry point for the server."""
    # --- Fix #1: Explicitly initialize ConfigManager before anything else ---
    try:
        # Determine config file path from environment or defaults
        config_path = os.getenv("NFL_MCP_CONFIG_FILE")
        if config_path:
            from .config_manager import set_config_manager

            cm = get_config_manager()
            set_config_manager(
                type(cm)
                (
                    config_path,
                    enable_hot_reload=os.getenv("NFL_MCP_CONFIG_HOT_RELOAD", "0") == "1",
                )
            )
            logger.info(f"ConfigManager initialized with file: {config_path}")
    except Exception:
        logger.warning("Failed to initialize ConfigManager; using defaults", exc_info=True)

    # Create the application
    app = create_app()

    # Get DB instance for lifespan (injected via ContextVar)
    nfl_db = tool_registry.get_db()
    if nfl_db is None:
        raise RuntimeError("NFLDatabase not initialized in tool_registry")

    # Create lifespan with DB access
    app_lifespan_fn = _create_prefetch_lifespan(nfl_db)

    # Get MCP HTTP app with /mcp path prefix
    mcp_http = app.http_app(path="/mcp")

    # Save original MCP lifespan BEFORE replacing it
    original_mcp_lifespan = mcp_http.router.lifespan_context

    # Combine lifespans
    @asynccontextmanager
    async def combined_lifespan(app_instance):
        # Start our custom lifespan (prefetch, etc.)
        async with app_lifespan_fn(app_instance):
            # Start MCP's ORIGINAL lifespan
            async with original_mcp_lifespan(app_instance):
                yield

    # Replace the lifespan with combined version
    mcp_http.router.lifespan_context = combined_lifespan

    # Run with uvicorn
    import uvicorn

    uvicorn.run(mcp_http, host="0.0.0.0", port=9000)


if __name__ == "__main__":
    main()
