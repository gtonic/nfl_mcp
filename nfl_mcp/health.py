"""
Health check endpoint for NFL MCP Server.

Provides the /health REST endpoint for monitoring, including
server version, database health, circuit breaker states, rate limiter
status, and prefetch configuration.

Extracted from server.py as part of Fix #3 (extract health endpoint).
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.responses import JSONResponse


def _get_version() -> str:
    """Return the server version from pyproject.toml, falling back to a default."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        import tomli as tomllib

    # Walk up to find pyproject.toml from this module's location
    from pathlib import Path

    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            with open(candidate, "rb") as f:
                return tomllib.load(f).get("project", {}).get("version", "unknown")
    return "unknown"


def _get_prefetch_config() -> dict[str, Any]:
    """Return current prefetch configuration."""
    return {
        "enabled": os.getenv("NFL_MCP_PREFETCH") == "1",
        "interval_seconds": int(os.getenv("NFL_MCP_PREFETCH_INTERVAL", "900")),
        "advanced_enrich_enabled": os.getenv("NFL_MCP_ADVANCED_ENRICH") == "1",
    }


async def health_check() -> JSONResponse:
    """Health check endpoint for monitoring server status.

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

    # Get database health (if tool_registry has been initialized)
    db_health: dict[str, Any] = {}
    try:
        from .tool_registry import get_db

        nfl_db = get_db()
        if nfl_db is not None:
            try:
                db_health = nfl_db.health_check()  # type: ignore[attr-defined]
            except Exception as e:
                db_health = {"healthy": False, "error": str(e)}
    except Exception:
        pass

    # Get circuit breaker status
    circuit_breakers: dict[str, Any] = {}
    try:
        circuit_breakers = get_all_circuit_breaker_status()
    except Exception:
        pass

    # Get rate limiter status
    rate_limiters: dict[str, Any] = {}
    try:
        rate_limiters = get_all_rate_limiter_status()
    except Exception:
        pass

    return JSONResponse(
        {
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": version,
            "database": db_health,
            "circuit_breakers": circuit_breakers,
            "rate_limiters": rate_limiters,
            "prefetch": _get_prefetch_config(),
        }
    )
