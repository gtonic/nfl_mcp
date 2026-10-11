"""Shared state for the tool wrappers: the request's database and season/week.

Database access uses a ContextVar for async-safe dependency injection instead
of a mutable global, eliminating race conditions and making the code testable.
Tool modules call ``_context.get_db()`` (attribute lookup at call time), so a
test patches exactly one place: ``nfl_mcp.tools._context.get_db``.
"""
from __future__ import annotations

from contextvars import ContextVar

from ..database import NFLDatabase, set_shared_db

# Async-safe database instance via ContextVar (replaces mutable global get_db())
_db_token: ContextVar[NFLDatabase | None] = ContextVar("nfl_db", default=None)


def initialize_shared(db: NFLDatabase) -> None:
    """Register the shared database instance for tool access.

    Called once at application startup to inject the DB. Also registers it as
    the process-wide instance tool code reaches via ``database.get_shared_db``.
    """
    _db_token.set(db)
    set_shared_db(db)


def get_db() -> NFLDatabase | None:
    """Return the current request's database instance (or None)."""
    return _db_token.get()


async def _current_season_week() -> tuple[int | None, int | None]:
    """Season and week via the canonical ``week_context.current_season_week``."""
    from ..week_context import current_season_week
    try:
        current = await current_season_week(get_db())
    except Exception:
        return None, None
    return current["season"], current["week"]
