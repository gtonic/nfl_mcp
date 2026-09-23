"""
Health check endpoint for NFL MCP Server.

Provides the /health REST endpoint for monitoring, including
server version, database health, circuit breaker states, rate limiter
status, and prefetch configuration.

Extracted from server.py as part of Fix #3 (extract health endpoint).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.responses import JSONResponse


def _get_version() -> str:
    """Return the server version (single source of truth: ``nfl_mcp._version``)."""
    from ._version import get_version

    return get_version()


# A wedged sqlite (locked file, exhausted pool) must not hang the probe.
_DB_CHECK_TIMEOUT_SECONDS = 3.0


async def _check_database() -> dict[str, Any]:
    """Run the DB health check off the event loop, bounded by a timeout."""
    from .tool_registry import get_db

    nfl_db = get_db()
    if nfl_db is None:
        return {}
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(nfl_db.health_check),  # type: ignore[attr-defined]
            timeout=_DB_CHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return {"healthy": False, "error": f"health check timed out after {_DB_CHECK_TIMEOUT_SECONDS}s"}
    except Exception as e:
        return {"healthy": False, "error": str(e)}


def _overall_status(db_health: dict[str, Any], circuit_breakers: dict[str, Any]) -> tuple[str, int]:
    """``unhealthy``/503 only when the DB is down; an open breaker (an upstream
    API failing) is ``degraded``/200 -- the server still answers from cache."""
    if db_health and not db_health.get("healthy", False):
        return "unhealthy", 503
    if any((cb or {}).get("state") in ("open", "half_open") for cb in circuit_breakers.values()):
        return "degraded", 200
    return "healthy", 200


def _get_prefetch_config() -> dict[str, Any]:
    """Return current prefetch configuration."""
    return {
        "enabled": os.getenv("NFL_MCP_PREFETCH") == "1",
        "interval_seconds": int(os.getenv("NFL_MCP_PREFETCH_INTERVAL", "900")),
        "advanced_enrich_enabled": os.getenv("NFL_MCP_ADVANCED_ENRICH") == "1",
        "athletes_refresh_enabled": os.getenv("NFL_MCP_PREFETCH_ATHLETES", "1") == "1",
        "athletes_refresh_interval_seconds": int(
            os.getenv("NFL_MCP_PREFETCH_ATHLETES_INTERVAL", "86400")
        ),
    }


def _get_tool_profile() -> dict[str, Any]:
    """The registered tool profile and how many tools it exposes."""
    try:
        from .tool_registry import get_all_tools, tool_profile

        profile = tool_profile()
        return {"profile": profile, "count": len(get_all_tools(profile))}
    except Exception as e:
        return {"profile": None, "count": None, "error": str(e)}


async def health_check() -> JSONResponse:
    """Health check endpoint for monitoring server status.

    Status: ``healthy`` (200); ``degraded`` (200) while any upstream circuit
    breaker is open/half-open; ``unhealthy`` (503) only when the database
    check fails -- the one condition a restart can fix.

    Returns detailed status including:
    - Server status and version
    - Database health and stats
    - Circuit breaker states
    - Rate limiter status
    - Prefetch status
    """
    from starlette.responses import JSONResponse

    from .config import get_all_rate_limiter_status
    from .retry_utils import get_all_circuit_breaker_status

    # Get version
    version = _get_version()

    # Get database health (if tool_registry has been initialized): a pool
    # check plus cheap queries, in a worker thread with a timeout.
    db_health: dict[str, Any] = {}
    try:
        db_health = await _check_database()
    except Exception as e:
        db_health = {"healthy": False, "error": str(e)}

    # Get circuit breaker status
    circuit_breakers: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        circuit_breakers = get_all_circuit_breaker_status()

    # Get rate limiter status
    rate_limiters: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        rate_limiters = get_all_rate_limiter_status()

    status, status_code = _overall_status(db_health, circuit_breakers)
    open_breakers = sorted(
        name for name, cb in circuit_breakers.items()
        if (cb or {}).get("state") in ("open", "half_open")
    )

    return JSONResponse(
        {
            "status": status,
            "service": "NFL MCP Server",
            "version": version,
            "database": db_health,
            "circuit_breakers": circuit_breakers,
            "rate_limiters": rate_limiters,
            "open_circuit_breakers": open_breakers,
            "prefetch": _get_prefetch_config(),
            "tools": _get_tool_profile(),
        },
        status_code=status_code,
    )
