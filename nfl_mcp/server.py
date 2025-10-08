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
"""

from fastmcp import FastMCP
from starlette.responses import JSONResponse

from .database import NFLDatabase
from . import tool_registry
import asyncio, os, logging
from datetime import datetime, UTC
from contextlib import asynccontextmanager

# Configure logging with INFO level by default
LOG_LEVEL = os.getenv("NFL_MCP_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)
logger.info(f"Logging initialized at {LOG_LEVEL} level")

PREFETCH_ENABLED = os.getenv("NFL_MCP_PREFETCH") == "1"
PREFETCH_INTERVAL_SECONDS = int(os.getenv("NFL_MCP_PREFETCH_INTERVAL", "900"))  # default 15m
PREFETCH_SNAPS_TTL_SECONDS = int(os.getenv("NFL_MCP_PREFETCH_SNAPS_TTL", "900"))  # refetch snaps after 15m by default

# Global state for prefetch task
_prefetch_task = None
_shutdown_event = None

async def _prefetch_loop(nfl_db: NFLDatabase, shutdown_event: asyncio.Event):
    """Background loop to prefetch weekly schedule and player snaps to warm caches.

    Strategy:
      - Determine season/week via get_nfl_state tool (internal call)
      - Fetch schedule (stores opponents for DEF) if not already cached
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
        _fetch_practice_reports,
        _fetch_weekly_usage_stats,
        ADVANCED_ENRICH_ENABLED
    )
    if not ADVANCED_ENRICH_ENABLED:
        logger.warning("Prefetch loop disabled: NFL_MCP_ADVANCED_ENRICH not set to 1")
        return
    
    logger.info(f"Prefetch loop started (interval={PREFETCH_INTERVAL_SECONDS}s, snaps_ttl={PREFETCH_SNAPS_TTL_SECONDS}s)")
    
    cycle_count = 0
    while not shutdown_event.is_set():
        cycle_count += 1
        cycle_start = datetime.now(UTC)
        logger.info(f"[Prefetch Cycle #{cycle_count}] Starting at {cycle_start.isoformat()}")
        
        stats = {
            "schedule_inserted": 0,
            "snaps_inserted": 0,
            "practice_inserted": 0,
            "usage_inserted": 0,
            "schedule_error": None,
            "snaps_error": None,
            "practice_error": None,
            "usage_error": None
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
                    logger.warning(f"[Prefetch Cycle #{cycle_count}] Could not parse season/week: season_raw={season_raw}, week_raw={week_raw}, error={e}")
                    season = None
                    week = None
                
                logger.info(f"[Prefetch Cycle #{cycle_count}] NFL State: season={season}, week={week}")
                
                if season is not None and week is not None and isinstance(season, int) and isinstance(week, int):
                    # Schedule prefetch
                    try:
                        logger.debug(f"[Prefetch Cycle #{cycle_count}] Fetching schedule for season={season}, week={week}")
                        sched_rows = await _fetch_week_schedule(season, week)
                        if sched_rows:
                            inserted = nfl_db.upsert_schedule_games(sched_rows)
                            stats["schedule_inserted"] = inserted
                            logger.info(f"[Prefetch Cycle #{cycle_count}] Schedule: {inserted} rows inserted (season={season} week={week})")
                        else:
                            logger.info(f"[Prefetch Cycle #{cycle_count}] Schedule: No rows returned")
                    except Exception as e:
                        stats["schedule_error"] = str(e)
                        logger.error(f"[Prefetch Cycle #{cycle_count}] Schedule fetch failed: {e}", exc_info=True)
                    
                    # Snaps prefetch (current week + previous week as fallback)
                    # Current week might not have data yet (games not played)
                    snap_weeks_to_fetch = [week]
                    if week > 1:
                        snap_weeks_to_fetch.append(week - 1)  # Add previous week
                    
                    total_snap_rows_inserted = 0
                    for snap_week in snap_weeks_to_fetch:
                        try:
                            logger.debug(f"[Prefetch Cycle #{cycle_count}] Fetching snaps for season={season}, week={snap_week}")
                            snap_rows = await _fetch_week_player_snaps(season, snap_week)
                            # Take only first 2000 to avoid huge memory churn
                            if snap_rows:
                                subset = snap_rows[:2000]
                                inserted = nfl_db.upsert_player_week_stats(subset)
                                total_snap_rows_inserted += inserted
                                logger.info(f"[Prefetch Cycle #{cycle_count}] Snaps (week {snap_week}): {inserted} rows inserted from {len(snap_rows)} fetched")
                            else:
                                logger.info(f"[Prefetch Cycle #{cycle_count}] Snaps (week {snap_week}): No rows returned")
                        except Exception as e:
                            stats["snaps_error"] = str(e)
                            logger.error(f"[Prefetch Cycle #{cycle_count}] Snaps fetch (week {snap_week}) failed: {e}", exc_info=True)
                    
                    stats["snaps_inserted"] = total_snap_rows_inserted
                    if total_snap_rows_inserted > 0:
                        logger.info(f"[Prefetch Cycle #{cycle_count}] Snaps total: {total_snap_rows_inserted} rows inserted across {len(snap_weeks_to_fetch)} weeks")
                    
                    # Practice reports (Thu-Sat only to capture weekly injury reports)
                    weekday = datetime.now(UTC).weekday()
                    logger.debug(f"[Prefetch Cycle #{cycle_count}] Current weekday: {weekday} ({'Thu' if weekday == 3 else 'Fri' if weekday == 4 else 'Sat' if weekday == 5 else 'Other'})")
                    if weekday in [3, 4, 5]:  # Thu=3, Fri=4, Sat=5
                        try:
                            logger.debug(f"[Prefetch Cycle #{cycle_count}] Fetching practice reports for season={season}, week={week}")
                            practice_reports = await _fetch_practice_reports(season, week)
                            if practice_reports:
                                inserted = nfl_db.upsert_practice_status(practice_reports)
                                stats["practice_inserted"] = inserted
                                logger.info(f"[Prefetch Cycle #{cycle_count}] Practice: {inserted} rows inserted")
                            else:
                                logger.info(f"[Prefetch Cycle #{cycle_count}] Practice: No rows returned (implementation pending)")
                        except Exception as e:
                            stats["practice_error"] = str(e)
                            logger.error(f"[Prefetch Cycle #{cycle_count}] Practice fetch failed: {e}", exc_info=True)
                    else:
                        logger.debug(f"[Prefetch Cycle #{cycle_count}] Practice: Skipped (only runs Thu-Sat)")
                    
                    # Usage stats (fetch previous week for rolling averages)
                    if week > 1:
                        try:
                            logger.debug(f"[Prefetch Cycle #{cycle_count}] Fetching usage stats for season={season}, week={week-1}")
                            usage_stats = await _fetch_weekly_usage_stats(season, week - 1)
                            if usage_stats:
                                inserted = nfl_db.upsert_usage_stats(usage_stats)
                                stats["usage_inserted"] = inserted
                                logger.info(f"[Prefetch Cycle #{cycle_count}] Usage: {inserted} rows inserted (week {week-1})")
                            else:
                                logger.info(f"[Prefetch Cycle #{cycle_count}] Usage: No rows returned")
                        except Exception as e:
                            stats["usage_error"] = str(e)
                            logger.error(f"[Prefetch Cycle #{cycle_count}] Usage fetch failed: {e}", exc_info=True)
                    else:
                        logger.debug(f"[Prefetch Cycle #{cycle_count}] Usage: Skipped (week={week}, need week > 1)")
                else:
                    logger.warning(
                        f"[Prefetch Cycle #{cycle_count}] Invalid season/week: "
                        f"season={season} (type={type(season).__name__}), "
                        f"week={week} (type={type(week).__name__})"
                    )
            else:
                logger.warning(f"[Prefetch Cycle #{cycle_count}] NFL state unavailable or unsuccessful")
        except Exception as e:
            logger.error(f"[Prefetch Cycle #{cycle_count}] Iteration error: {e}", exc_info=True)
        
        cycle_end = datetime.now(UTC)
        cycle_duration = (cycle_end - cycle_start).total_seconds()
        
        logger.info(
            f"[Prefetch Cycle #{cycle_count}] Completed in {cycle_duration:.2f}s - "
            f"Schedule: {stats['schedule_inserted']} rows, "
            f"Snaps: {stats['snaps_inserted']} rows, "
            f"Practice: {stats['practice_inserted']} rows, "
            f"Usage: {stats['usage_inserted']} rows"
        )
        
        if any([stats['schedule_error'], stats['snaps_error'], stats['practice_error'], stats['usage_error']]):
            logger.warning(
                f"[Prefetch Cycle #{cycle_count}] Errors occurred - "
                f"Schedule: {stats['schedule_error'] or 'OK'}, "
                f"Snaps: {stats['snaps_error'] or 'OK'}, "
                f"Practice: {stats['practice_error'] or 'OK'}, "
                f"Usage: {stats['usage_error'] or 'OK'}"
            )
        
        logger.info(f"[Prefetch Cycle #{cycle_count}] Next cycle in {PREFETCH_INTERVAL_SECONDS}s")
        
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=PREFETCH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


def create_app() -> FastMCP:
    """Create and configure the FastMCP server application."""
    
    # Create FastMCP server instance
    mcp = FastMCP(
        name="NFL MCP Server"
    )
    
    # Initialize NFL database
    nfl_db = NFLDatabase()
    
    # Initialize shared resources in tool registry
    tool_registry.initialize_shared(nfl_db)
    
    # Register all tools from the tool registry
    for tool_func in tool_registry.get_all_tools():
        mcp.tool(tool_func)
    
    return mcp


def create_lifespan(nfl_db: NFLDatabase):
    """Factory function to create lifespan with access to nfl_db instance."""
    @asynccontextmanager
    async def app_lifespan(app):
        """Lifespan context manager for background tasks."""
        global _prefetch_task, _shutdown_event
        
        if PREFETCH_ENABLED:
            # Import late to avoid circular
            from .sleeper_tools import ADVANCED_ENRICH_ENABLED
            
            if ADVANCED_ENRICH_ENABLED:
                _shutdown_event = asyncio.Event()
                _prefetch_task = asyncio.create_task(_prefetch_loop(nfl_db, _shutdown_event))
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
    app = create_app()
    
    # Add health endpoint using FastMCP's custom_route
    from starlette.responses import JSONResponse
    
    @app.custom_route("/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint for monitoring server status."""
        return JSONResponse({
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.4.4"
        })
    
    # Get DB instance for lifespan
    nfl_db = tool_registry._nfl_db
    if not nfl_db:
        raise RuntimeError("NFLDatabase not initialized in tool_registry")
    
    # Create lifespan with DB access
    app_lifespan_fn = create_lifespan(nfl_db)
    
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