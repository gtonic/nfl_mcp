"""Constants, helpers and the logger shared by the ``nfl_mcp.db`` modules."""
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .pool import ConnectionPoolConfig, DatabaseConnectionPool

# One logger for the whole persistence layer (kept as ``nfl_mcp.database``).
logger = logging.getLogger("nfl_mcp.database")

# Severity for a status the caller's map does not know, matching what the injury
# service assumes for an unrecognised designation (injury_service.DEFAULT_SEVERITY,
# QUESTIONABLE).
_DEFAULT_SEVERITY = 2

# What a report a complete crawl no longer lists is cleared to (not news).
CLEARED_REPORT_TEXT = "No longer on the injury report"

# Signal history (practice rows, news blurbs) is kept at least this long, and
# never inside the current season.
SIGNAL_HISTORY_DAYS = 365
# The NFL league year turns in March; a season's history starts with training
# camp. Before March a date belongs to the previous calendar year's season.
SEASON_ROLLOVER_MONTH = 3
SEASON_START_MONTH = 7


def season_start(now: datetime) -> datetime:
    """Start (July 1, UTC) of the NFL season ``now`` falls in."""
    year = now.year if now.month >= SEASON_ROLLOVER_MONTH else now.year - 1
    return datetime(year, SEASON_START_MONTH, 1, tzinfo=UTC)


class _DBMixinBase:
    """What every ``NFLDatabase`` mixin can rely on ``self`` to provide."""

    CURRENT_SCHEMA_VERSION: int
    db_path: Path
    pool_config: "ConnectionPoolConfig"
    _pool: "DatabaseConnectionPool"

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        """Get a database connection with proper cleanup. (Legacy method for compatibility)"""
        with self._pool.get_connection() as conn:
            yield conn

    if TYPE_CHECKING:
        # Implemented by InjuriesMixin; also used by the v17 migration.
        @staticmethod
        def _news_history_row(inj: dict, sources: Any, now: str) -> tuple | None: ...
