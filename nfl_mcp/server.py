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
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastmcp import FastMCP

from . import http_security, tool_registry
from .config_manager import get_config_manager
from .database import NFLDatabase
from .health import env_int as _env_int
from .health import health_check as _health_check
from .log_redaction import install_log_redaction


def _load_dotenv(path: Path | None = None) -> int:
    """Populate ``os.environ`` from a ``.env`` file next to the repo root.

    Secrets such as ``ODDS_API_KEY`` live in a gitignored ``.env`` so a local
    run picks them up without exporting anything by hand. Deliberately
    dependency-free and non-destructive: a variable already present in the real
    environment always wins, so container/CI values are never clobbered.

    Returns the number of variables newly set.
    """
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    try:
        raw = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0

    loaded = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


# Configure logging with INFO level by default. .env is NOT loaded here:
# importing the package (tests, tooling) must not pick up a developer's local
# secrets. ``main()`` loads it and then re-derives the settings below.
LOG_LEVEL = os.getenv("NFL_MCP_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
# httpx logs full request URLs at INFO (the Odds API key rides in the query
# string): quiet it and mask secrets on every root handler.
install_log_redaction()

logger = logging.getLogger(__name__)


def _load_runtime_settings() -> None:
    """(Re-)read the env-driven prefetch/prune settings into module globals.

    Runs at import (defaults for tests and embedders) and again in ``main()``
    once ``.env`` is loaded, so .env-provided values take effect.
    """
    global PREFETCH_ENABLED, PREFETCH_INTERVAL_SECONDS, PREFETCH_SNAPS_TTL_SECONDS
    global PREFETCH_SCHEDULE_WEEKS, PREFETCH_ATHLETES, PREFETCH_ATHLETES_INTERVAL_SECONDS
    global DB_PRUNE_INTERVAL_SECONDS
    global PREFETCH_NEWS, PREFETCH_NEWS_INTERVAL_SECONDS, PREFETCH_NEWS_GAMEDAY_INTERVAL_SECONDS

    # Prefetch config from environment (prefetch is separate from general config)
    PREFETCH_ENABLED = os.getenv("NFL_MCP_PREFETCH") == "1"
    PREFETCH_INTERVAL_SECONDS = _env_int("NFL_MCP_PREFETCH_INTERVAL", 900)
    PREFETCH_SNAPS_TTL_SECONDS = _env_int("NFL_MCP_PREFETCH_SNAPS_TTL", 900)
    PREFETCH_SCHEDULE_WEEKS = _env_int("NFL_MCP_PREFETCH_SCHEDULE_WEEKS", 4)
    # Athletes cache refresh (player names/teams/positions). Enabled by default when
    # prefetch runs; refreshed once at startup and then every ATHLETES_INTERVAL.
    PREFETCH_ATHLETES = os.getenv("NFL_MCP_PREFETCH_ATHLETES", "1") == "1"
    PREFETCH_ATHLETES_INTERVAL_SECONDS = _env_int(
        "NFL_MCP_PREFETCH_ATHLETES_INTERVAL", 86400  # daily
    )
    # DB pruning cadence. Wall-clock rather than a cycle count, which reset on every
    # restart and so never reached its threshold on a server restarted daily.
    DB_PRUNE_INTERVAL_SECONDS = _env_int("NFL_MCP_DB_PRUNE_INTERVAL", 86400)  # daily
    # Player news (`news_sources`): every 45 min, every 15 min in the game-day
    # windows (inactives and late scratches, see `_news_interval`).
    PREFETCH_NEWS = os.getenv("NFL_MCP_PREFETCH_NEWS", "1") == "1"
    PREFETCH_NEWS_INTERVAL_SECONDS = _env_int("NFL_MCP_PREFETCH_NEWS_INTERVAL", 2700)
    PREFETCH_NEWS_GAMEDAY_INTERVAL_SECONDS = _env_int(
        "NFL_MCP_PREFETCH_NEWS_GAMEDAY_INTERVAL", 900)


PREFETCH_ENABLED: bool
PREFETCH_INTERVAL_SECONDS: int
PREFETCH_SNAPS_TTL_SECONDS: int
PREFETCH_SCHEDULE_WEEKS: int
PREFETCH_ATHLETES: bool
PREFETCH_ATHLETES_INTERVAL_SECONDS: int
DB_PRUNE_INTERVAL_SECONDS: int
PREFETCH_NEWS: bool
PREFETCH_NEWS_INTERVAL_SECONDS: int
PREFETCH_NEWS_GAMEDAY_INTERVAL_SECONDS: int
_load_runtime_settings()
_last_prune_at: float | None = None
# Wall clock of the last news poll (the monotonic clock stops while the host
# sleeps; a woken laptop should poll at once).
_last_news_at: datetime | None = None

# How long shutdown waits for an in-flight startup warm-up before cancelling it.
_STARTUP_TASK_SHUTDOWN_GRACE_SECONDS = 10.0

# Global state for prefetch task
_prefetch_task: asyncio.Task | None = None
_shutdown_event: asyncio.Event | None = None


async def _refresh_athletes(nfl_db: NFLDatabase, tag: str = "Prefetch") -> None:
    """Refresh the Sleeper athletes cache (player names, teams, positions).

    Player→team assignments change over the offseason and season (signings,
    trades, releases), so the cache is refreshed periodically to keep enrichment
    — e.g. trending players — current. Best-effort: failures are logged, never
    raised. Gated by ``NFL_MCP_PREFETCH_ATHLETES`` (default on). Runs
    ``data_refresh``'s athletes scope, the same path as ``refresh_data``.
    """
    if not PREFETCH_ATHLETES:
        return
    from . import data_refresh
    res = await data_refresh.run_scope("athletes", nfl_db, None, None)
    if res.get("status") == "ok":
        logger.info(f"[{tag}] Athletes cache refreshed: {res.get('written')} stored "
                    f"({res.get('fetched')} processed)")
    else:
        logger.warning(f"[{tag}] Athletes refresh {res.get('status')}: "
                       f"{res.get('error') or res.get('job_id')}")


async def _prune_db_if_due(nfl_db: NFLDatabase, tag: str = "Prune") -> bool:
    """Prune old rows at startup and then every ``DB_PRUNE_INTERVAL_SECONDS``.

    Runs in a worker thread: a large first delete plus the WAL checkpoint must
    not stall the event loop. Best-effort; returns whether a prune ran.
    """
    global _last_prune_at
    now = time.monotonic()
    if _last_prune_at is not None and now - _last_prune_at < DB_PRUNE_INTERVAL_SECONDS:
        return False
    _last_prune_at = now
    try:
        deleted = await asyncio.to_thread(nfl_db.prune_old_data)
        logger.info(f"[{tag}] DB prune: deleted {sum(deleted.values())} old rows")
    except Exception as e:
        logger.warning(f"[{tag}] DB prune failed: {e}")
    return True


def _athletes_refresh_every_n_cycles() -> int:
    """Number of prefetch cycles between athletes refreshes (always >= 1).

    Derived from the athletes interval vs. the base prefetch interval so the
    cadence tracks ``NFL_MCP_PREFETCH_ATHLETES_INTERVAL`` (default daily)
    regardless of the base loop interval.
    """
    return max(1, round(PREFETCH_ATHLETES_INTERVAL_SECONDS / max(1, PREFETCH_INTERVAL_SECONDS)))


def _athletes_overdue(nfl_db: NFLDatabase) -> bool:
    """Whether the stored athletes are older than the refresh interval.

    The cycle count alone stalls while the host sleeps (the loop's wait runs on
    the monotonic clock, which stops with it): athletes were seen 43h old.
    The table's own timestamp is wall-clock, so it catches up on the first
    cycle after waking.
    """
    try:
        age = (nfl_db.get_data_freshness().get("athletes") or {}).get("age_hours")
        return age is not None and float(age) * 3600 >= PREFETCH_ATHLETES_INTERVAL_SECONDS
    except Exception as e:
        logger.debug(f"athletes freshness unavailable: {e}")
        return False


async def _prefetch_loop(nfl_db: NFLDatabase, shutdown_event: asyncio.Event):
    """Background loop to prefetch weekly schedule and player snaps to warm caches.

    Strategy:
      - Determine season/week via get_nfl_state tool (internal call)
      - Run the cycle's ``data_refresh`` scopes (``_cycle_scopes``): the same
        fetch-and-write code as the ``refresh_data`` tool
      - Prune when due; refresh athletes on their own cadence
      - Sleep until next interval or shutdown
    Controlled by env NFL_MCP_PREFETCH=1.
    """
    if not PREFETCH_ENABLED:
        logger.info("Prefetch loop disabled: NFL_MCP_PREFETCH not set to 1")
        return

    # Import late to avoid circular
    from . import data_refresh
    from .practice_reports import to_eastern
    from .sleeper_tools import advanced_enrich_enabled, get_nfl_state

    if not advanced_enrich_enabled():
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
        tag = f"Prefetch Cycle #{cycle_count}"
        logger.info(f"[{tag}] Starting at {cycle_start.isoformat()}")
        results: dict[str, dict] = {}

        try:
            season, week = _season_week(await get_nfl_state(), tag)
            if season is not None and week is not None:
                news_due = PREFETCH_NEWS and _news_due(cycle_start, _last_news_at)
                for scope in _cycle_scopes(week, to_eastern(cycle_start).weekday(),
                                           news_due=news_due):
                    # The same fetch-and-write as refresh_data (one code path).
                    results[scope] = await data_refresh.run_scope(scope, nfl_db, season, week)
                    _log_scope(tag, scope, results[scope])
                    if scope == "news" and results[scope].get("status") != "already_running":
                        _mark_news_polled(cycle_start)
        except Exception as e:
            logger.error(f"[{tag}] Iteration error: {e}", exc_info=True)

        cycle_duration = (datetime.now(UTC) - cycle_start).total_seconds()
        logger.info(
            f"[{tag}] Completed in {cycle_duration:.2f}s - "
            + ", ".join(f"{s.capitalize()}: {r.get('written', 0)} rows" for s, r in results.items())
        )
        errors = {s: r.get("error") for s, r in results.items() if r.get("status") == "error"}
        if errors:
            logger.warning(f"[{tag}] Errors occurred - {errors}")

        # Prune old snapshots/history once the prune interval has elapsed.
        await _prune_db_if_due(nfl_db, tag=tag)

        # Periodic athletes cache refresh (default daily) so player
        # names/teams/positions stay current as roster moves happen.
        if PREFETCH_ATHLETES and (cycle_count % _athletes_refresh_every_n_cycles() == 0
                                  or _athletes_overdue(nfl_db)):
            await _refresh_athletes(nfl_db, tag=tag)

        logger.info(f"[{tag}] Next cycle in {PREFETCH_INTERVAL_SECONDS}s")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=PREFETCH_INTERVAL_SECONDS)
        except TimeoutError:
            continue


# Weekday (US Eastern) with no practice reports: Sunday teams report Wed-Fri,
# Thursday teams Mon-Wed and Monday teams Thu-Sat.
_NO_PRACTICE_WEEKDAY = 6


def _cycle_scopes(week: int, weekday_et: int, news_due: bool = False) -> list[str]:
    """The ``data_refresh`` scopes one prefetch cycle runs, in order.

    Schedule (this week + the look-ahead), snaps (this week and the last) and
    injuries every cycle; practice reports every day but Sunday (US Eastern);
    usage once there is a completed week, and then the accuracy grading (a
    no-op unless a week has just become final or its stat corrections are
    due: `projection_accuracy.weeks_to_grade`); player news when it is due
    (`_news_due`). Athletes run on their own cadence.
    """
    scopes = ["schedule", "snaps", "injuries"]
    if weekday_et != _NO_PRACTICE_WEEKDAY:
        scopes.append("practice")
    if week > 1:
        scopes += ["usage", "accuracy"]
    if news_due:
        scopes.append("news")
    return scopes


# Game-day windows (US Eastern, hours): inactives come out 90 minutes before
# kickoff and late scratches after, so news is polled more often then.
_NEWS_GAMEDAY_WINDOWS = {6: (10.0, 20.5), 0: (17.0, 20.5), 3: (17.0, 20.5), 5: (14.0, 20.5)}
# A cycle runs every PREFETCH_INTERVAL plus its own duration: this much early
# still counts as due, so a 15-minute news interval runs every cycle.
_NEWS_DUE_SLACK_SECONDS = 120


def _news_interval(now_et: datetime) -> int:
    """Seconds between news polls at this (US Eastern) time."""
    window = _NEWS_GAMEDAY_WINDOWS.get(now_et.weekday())
    hour = now_et.hour + now_et.minute / 60
    if window and window[0] <= hour <= window[1]:
        return PREFETCH_NEWS_GAMEDAY_INTERVAL_SECONDS
    return PREFETCH_NEWS_INTERVAL_SECONDS


def _news_due(now: datetime, last: datetime | None) -> bool:
    """Whether a prefetch cycle at `now` should poll the news sources."""
    from .practice_reports import to_eastern
    if last is None:
        return True
    return ((now - last).total_seconds()
            >= _news_interval(to_eastern(now)) - _NEWS_DUE_SLACK_SECONDS)


def _mark_news_polled(when: datetime) -> None:
    global _last_news_at
    _last_news_at = when


def _season_week(state: dict, tag: str) -> tuple[int | None, int | None]:
    """``(season, week)`` as ints from a ``get_nfl_state`` result, or Nones."""
    if not (state.get("success") and state.get("nfl_state")):
        logger.warning(f"[{tag}] NFL state unavailable or unsuccessful")
        return None, None
    st = state["nfl_state"]
    season_raw = st.get("season") or st.get("league_season")
    week_raw = st.get("week") or st.get("display_week")
    try:
        season, week = int(season_raw), int(week_raw)
    except (ValueError, TypeError) as e:
        logger.warning(f"[{tag}] Could not parse season/week: season_raw={season_raw}, "
                       f"week_raw={week_raw}, error={e}")
        return None, None
    logger.info(f"[{tag}] NFL State: season={season}, week={week}")
    return season, week


def _log_scope(tag: str, scope: str, result: dict) -> None:
    status = result.get("status")
    if status == "ok":
        logger.info(f"[{tag}] {scope.capitalize()}: {result.get('written', 0)} rows written "
                    f"from {result.get('fetched', 0)} fetched ({result.get('duration_s')}s)")
    elif status == "already_running":
        logger.info(f"[{tag}] {scope.capitalize()}: skipped, a manual refresh is running "
                    f"(job {result.get('job_id')})")
    else:
        logger.error(f"[{tag}] {scope.capitalize()} failed: {result.get('error')}")


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

    # --- Initialize NFL database ---
    nfl_db = NFLDatabase()

    # --- Fix #2: Inject DB into ContextVar (eliminates global mutable state) ---
    tool_registry.initialize_shared(nfl_db)

    # --- Create FastMCP server instance (FastMCP 4) ---
    # The background prefetch/shutdown work is registered on the server via the
    # public ``lifespan=`` constructor argument. FastMCP composes it with the
    # transport's own (session-manager) lifespan, so main() no longer needs to
    # monkey-patch the ASGI app's internal ``router.lifespan_context``.
    # Optional shared-secret bearer auth (NFL_MCP_AUTH_TOKEN): FastMCP enforces
    # it on /mcp only; /health and /metrics decide for themselves below.
    auth = http_security.build_auth()
    mcp = FastMCP(
        name="NFL MCP Server", lifespan=_create_prefetch_lifespan(nfl_db), auth=auth,
    )
    if auth is not None:
        logger.info("Bearer auth enabled for /mcp (NFL_MCP_AUTH_TOKEN)")

    # --- Register all tools from the tool registry ---
    profile = tool_registry.tool_profile()
    tools = tool_registry.get_all_tools(profile)
    for tool_func in tools:
        mcp.tool(tool_func)
    logger.info(f"Tool profile {profile!r}: {len(tools)} tools registered "
                f"(set NFL_MCP_TOOL_PROFILE=season|full|offseason)")

    # --- Fix #3: Mount extracted health endpoint (separate module) ---
    @mcp.custom_route("/health", methods=["GET"])
    async def _health_endpoint(request):  # type: ignore[assignment]
        # Without a configured token everything is local and the full report
        # is returned; with one, only a token holder sees the details.
        detailed = auth is None or http_security.request_is_authenticated(request)
        return await _health_check(detailed=detailed)

    # Prometheus text exposition of the per-tool counters, opt-in
    # (NFL_MCP_METRICS=1); requires the bearer token when one is configured.
    if os.getenv("NFL_MCP_METRICS", "0").strip().lower() in ("1", "true", "yes", "on"):
        @mcp.custom_route("/metrics", methods=["GET"])
        async def _metrics_endpoint(request):  # type: ignore[assignment]
            from starlette.responses import PlainTextResponse

            from .metrics import get_metrics_collector

            if auth is not None and not http_security.request_is_authenticated(request):
                return PlainTextResponse("Unauthorized", status_code=401)

            return PlainTextResponse(
                get_metrics_collector().get_prometheus_metrics(),
                media_type="text/plain; version=0.0.4",
            )

    return mcp


async def _startup_warmup(nfl_db: NFLDatabase, shutdown_event: asyncio.Event) -> None:
    """Startup work that must not delay serving: prune, cache warm-up, loop.

    Runs as a background task so ``/health`` and ``/mcp`` answer immediately
    (the Docker HEALTHCHECK would otherwise race a 32-team schedule fetch).
    """
    # Prune on every start, prefetch or not: tool calls write snapshots too.
    try:
        await _prune_db_if_due(nfl_db, tag="Startup")
    except Exception as e:
        logger.warning(f"[Startup] Prune failed: {e}")

    if not PREFETCH_ENABLED:
        return

    # Import late to avoid circular
    from .sleeper_tools import (
        _fetch_all_team_schedules,
        advanced_enrich_enabled,
    )

    if not advanced_enrich_enabled():
        logger.info("Prefetch disabled: NFL_MCP_ADVANCED_ENRICH not enabled")
        return

    # Run initial startup prefetch (schedules for all 32 teams)
    logger.info("[Startup Prefetch] Running initial cache warm-up...")
    try:
        # Get current season
        from .week_context import current_season_week
        season = (await current_season_week(nfl_db))["season"]

        logger.info(
            f"[Startup Prefetch] Fetching schedules for all 32 teams (season={season})..."
        )
        schedules = await _fetch_all_team_schedules(season)

        if schedules:
            inserted = await asyncio.to_thread(nfl_db.upsert_schedule_games, schedules)
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

    # Initial athletes cache refresh (names/teams/positions) so enrichment
    # is current as early as possible.
    await _refresh_athletes(nfl_db, tag="Startup Prefetch")

    if shutdown_event.is_set():
        return

    # Hand over to the periodic prefetch loop (same task).
    logger.info("Background prefetch loop starting")
    await _prefetch_loop(nfl_db, shutdown_event)


def _create_prefetch_lifespan(nfl_db: NFLDatabase):
    """Factory function to create lifespan with access to nfl_db instance.

    Starts the startup warm-up + prefetch loop in the background (the server
    serves immediately) and shuts it down gracefully.
    """

    @asynccontextmanager
    async def app_lifespan(app):
        """Lifespan context manager for background prefetch task."""
        global _prefetch_task, _shutdown_event

        _shutdown_event = asyncio.Event()
        _prefetch_task = asyncio.create_task(_startup_warmup(nfl_db, _shutdown_event))
        logger.info("Background startup/prefetch task started")

        try:
            yield  # Server running
        finally:
            # Shutdown: the loop exits on the event; a warm-up still in
            # flight gets a short grace period, then is cancelled.
            task, event = _prefetch_task, _shutdown_event
            if task and event:
                logger.info("Stopping prefetch task...")
                event.set()
                done, _ = await asyncio.wait({task}, timeout=_STARTUP_TASK_SHUTDOWN_GRACE_SECONDS)
                if not done:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                elif not task.cancelled() and task.exception() is not None:
                    logger.warning(f"Prefetch task ended with error: {task.exception()}")
                logger.info("Prefetch task stopped")

    return app_lifespan


def _port_in_use(host: str, port: int) -> bool:
    """Check whether ``port`` already has a listener we would collide with."""
    import socket

    # 0.0.0.0 binds every interface, so probe loopback to detect any listener.
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((probe_host, port)) == 0


def build_http_app(app: FastMCP):
    """The Streamable HTTP ASGI app for ``app``, with Host/Origin checks on.

    FastMCP 4 serves the sessionless ``2026-07-28`` protocol out of the box via
    mode negotiation. We additionally enable ``stateless_http`` so the
    Streamable HTTP transport keeps NO server-side session state at all: every
    request is self-contained, so the deployment can scale horizontally behind
    a plain round-robin load balancer with no sticky sessions and no shared
    session store. Set ``NFL_MCP_STATELESS_HTTP=0`` to fall back to the
    session-based transport (e.g. for older, handshake-era clients).

    ``host_origin_protection=True`` (strict) rejects any Host header outside
    localhost/127.0.0.1/[::1] + ``NFL_MCP_ALLOWED_HOSTS`` (421) and any
    ``Origin`` that is neither loopback, same-origin nor in
    ``NFL_MCP_ALLOWED_ORIGINS`` (403) -- the DNS-rebinding defence. Clients
    that send no Origin (Claude Code, curl, the Docker healthcheck) pass.
    """
    stateless_http = os.getenv("NFL_MCP_STATELESS_HTTP", "1") == "1"
    return app.http_app(
        path="/mcp",
        stateless_http=stateless_http,
        host_origin_protection=True,
        allowed_hosts=http_security.allowed_hosts(),
        allowed_origins=http_security.allowed_origins(),
    )


def main():
    """Main entry point for the server."""
    # Secrets such as ODDS_API_KEY live in a gitignored .env; load it here
    # (never on import), then re-derive everything read from the environment.
    loaded = _load_dotenv()
    if loaded:
        logger.info(f"Loaded {loaded} variable(s) from .env")
    logging.getLogger().setLevel(
        getattr(logging, os.getenv("NFL_MCP_LOG_LEVEL", "INFO").upper(), logging.INFO)
    )
    install_log_redaction()  # re-read NFL_MCP_HTTPX_LOG_LEVEL after .env
    _load_runtime_settings()

    # --- Fix #1: Explicitly initialize ConfigManager before anything else ---
    # Installing (or reloading) the manager re-derives nfl_mcp.config's
    # module-level values (limits, timeouts, User-Agent) before the tools are
    # registered and serve their first request.
    try:
        from .config_manager import ConfigManager, set_config_manager

        cm = get_config_manager()
        config_path = os.getenv("NFL_MCP_CONFIG_FILE")
        wanted = Path(config_path).resolve() if config_path else None
        if wanted is not None and cm.config_file_path != wanted:
            set_config_manager(
                ConfigManager(
                    wanted,
                    enable_hot_reload=os.getenv("NFL_MCP_CONFIG_HOT_RELOAD", "0") == "1",
                )
            )
            logger.info(f"ConfigManager initialized with file: {wanted}")
        else:
            # Same file (or none): reload so .env-provided overrides apply.
            cm.reload_configuration()
    except Exception:
        logger.warning("Failed to initialize ConfigManager; using defaults", exc_info=True)

    # Create the application (the prefetch lifespan is registered on the server
    # itself via FastMCP's ``lifespan=`` constructor argument, see create_app).
    app = create_app()

    # Build the MCP HTTP app under the ``/mcp`` path prefix (see build_http_app).
    mcp_http = build_http_app(app)

    # Run with uvicorn. Host/port are configurable so a local run can coexist
    # with a containerised instance instead of silently losing the bind race:
    # uvicorn logs the "address already in use" error and exits, which is easy
    # to miss when the process is backgrounded — so we check the port up front
    # and fail with an actionable message naming the occupied address.
    import uvicorn

    # Loopback by default: the server has no auth unless NFL_MCP_AUTH_TOKEN is
    # set, so it must not be reachable from the network by accident. The
    # Docker image sets NFL_MCP_HOST=0.0.0.0 (publish it as 127.0.0.1:9000).
    host = os.getenv("NFL_MCP_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("NFL_MCP_PORT", "9000"))
    except ValueError:
        logger.warning("Invalid NFL_MCP_PORT; falling back to 9000")
        port = 9000

    if _port_in_use(host, port):
        raise SystemExit(
            f"Port {port} on {host} is already in use — another NFL MCP instance "
            f"(e.g. a Docker container) is likely serving it. Stop it, or set "
            f"NFL_MCP_PORT to a free port."
        )

    logger.info(f"Starting NFL MCP Server on {host}:{port}")
    uvicorn.run(mcp_http, host=host, port=port)


if __name__ == "__main__":
    main()
