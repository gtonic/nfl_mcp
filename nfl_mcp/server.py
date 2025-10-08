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

logger = logging.getLogger(__name__)

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
        logger.info("Prefetch loop disabled: advanced enrichment not enabled (set NFL_MCP_ADVANCED_ENRICH=1)")
        return
    logger.info("Starting prefetch loop (interval=%ss)" % PREFETCH_INTERVAL_SECONDS)
    while not shutdown_event.is_set():
        try:
            state = await get_nfl_state()
            if state.get("success") and state.get("nfl_state"):
                st = state["nfl_state"]
                season = st.get("season") or st.get("league_season")
                week = st.get("week") or st.get("display_week")
                if isinstance(season, int) and isinstance(week, int):
                    # Schedule prefetch
                    try:
                        sched_rows = await _fetch_week_schedule(season, week)
                        if sched_rows:
                            inserted = nfl_db.upsert_schedule_games(sched_rows)
                            logger.debug(f"Prefetch schedule: {inserted} rows (season={season} week={week})")
                    except Exception as e:
                        logger.debug(f"Prefetch schedule failed: {e}")
                    # Snaps prefetch (subset to reduce load)
                    try:
                        snap_rows = await _fetch_week_player_snaps(season, week)
                        # Take only first 2000 to avoid huge memory churn
                        if snap_rows:
                            subset = snap_rows[:2000]
                            inserted = nfl_db.upsert_player_week_stats(subset)
                            logger.debug(f"Prefetch snaps: {inserted} rows (season={season} week={week})")
                    except Exception as e:
                        logger.debug(f"Prefetch snaps failed: {e}")
                    
                    # Practice reports (Thu-Sat only to capture weekly injury reports)
                    weekday = datetime.now(UTC).weekday()
                    if weekday in [3, 4, 5]:  # Thu=3, Fri=4, Sat=5
                        try:
                            practice_reports = await _fetch_practice_reports(season, week)
                            if practice_reports:
                                inserted = nfl_db.upsert_practice_status(practice_reports)
                                logger.debug(f"Prefetch practice reports: {inserted} rows (season={season} week={week})")
                        except Exception as e:
                            logger.debug(f"Prefetch practice reports failed: {e}")
                    
                    # Usage stats (fetch previous week for rolling averages)
                    if week > 1:
                        try:
                            usage_stats = await _fetch_weekly_usage_stats(season, week - 1)
                            if usage_stats:
                                inserted = nfl_db.upsert_usage_stats(usage_stats)
                                logger.debug(f"Prefetch usage stats: {inserted} rows (season={season} week={week-1})")
                        except Exception as e:
                            logger.debug(f"Prefetch usage stats failed: {e}")
            else:
                logger.debug("Prefetch: nfl_state unavailable this cycle")
        except Exception as e:
            logger.debug(f"Prefetch iteration error: {e}")
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
    
    # Health endpoint (non-MCP, directly exposed REST endpoint)
    @mcp.custom_route(path="/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint for monitoring server status."""
        return JSONResponse({
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.1.0"
        })
    
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
    
    # Get DB instance for lifespan
    nfl_db = tool_registry._nfl_db
    if not nfl_db:
        raise RuntimeError("NFLDatabase not initialized in tool_registry")
    
    # Create lifespan with DB access
    app_lifespan_fn = create_lifespan(nfl_db)
    
    # Combine MCP lifespan with our app lifespan
    @asynccontextmanager
    async def combined_lifespan(app_instance):
        async with app_lifespan_fn(app_instance):
            async with app.lifespan(app_instance):
                yield
    
    # Create HTTP app with combined lifespan
    from starlette.applications import Starlette
    http_app = Starlette(lifespan=combined_lifespan)
    
    # Mount MCP
    mcp_http = app.http_app(path="/mcp")
    http_app.mount("/mcp", mcp_http)
    
    # Run with uvicorn
    import uvicorn
    uvicorn.run(http_app, host="0.0.0.0", port=9000)


if __name__ == "__main__":
    main()