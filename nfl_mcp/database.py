"""
SQLite database management for NFL athletes and teams data.

This module handles the persistence layer for athlete information fetched from
the Sleeper API and teams information from ESPN API, providing caching and lookup functionality.
Features connection pooling, health checks, optimized indexing and migration support.

The implementation is split by concern: ``nfl_mcp.db.pool`` (connection pool),
``nfl_mcp.db.migrations`` (schema) and one mixin module per data domain
(``snapshots``, ``stats``, ``injuries``, ``news``, ``projections``,
``rankings``, ``athletes``). This module assembles ``NFLDatabase`` from them and
keeps the public import path (``from nfl_mcp.database import ...``) stable.
"""
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

# Re-exported (``X as X``) for ``from nfl_mcp.database import ...`` callers.
from .db._common import _DEFAULT_SEVERITY as _DEFAULT_SEVERITY
from .db._common import CLEARED_REPORT_TEXT as CLEARED_REPORT_TEXT
from .db._common import SEASON_ROLLOVER_MONTH as SEASON_ROLLOVER_MONTH
from .db._common import SEASON_START_MONTH as SEASON_START_MONTH
from .db._common import SIGNAL_HISTORY_DAYS as SIGNAL_HISTORY_DAYS
from .db._common import logger as logger
from .db._common import season_start as season_start
from .db.athletes import AthletesMixin
from .db.injuries import InjuriesMixin
from .db.migrations import MigrationsMixin
from .db.news import NewsMixin
from .db.pool import ConnectionPoolConfig as ConnectionPoolConfig
from .db.pool import DatabaseConnectionPool as DatabaseConnectionPool
from .db.pool import _close_idle as _close_idle
from .db.pool import _live_pools as _live_pools
from .db.pool import _PooledConnection as _PooledConnection
from .db.pool import open_connection_count as open_connection_count
from .db.projections import ProjectionsMixin
from .db.rankings import RankingsMixin
from .db.snapshots import SnapshotsMixin
from .db.stats import StatsMixin


class NFLDatabase(
    MigrationsMixin,
    SnapshotsMixin,
    StatsMixin,
    InjuriesMixin,
    NewsMixin,
    ProjectionsMixin,
    RankingsMixin,
    AthletesMixin,
):
    """SQLite database manager for NFL athlete and teams data with caching and lookup functionality."""

    # Database schema version for migrations
    CURRENT_SCHEMA_VERSION = 21

    def __init__(self, db_path: str | None = None, pool_config: ConnectionPoolConfig | None = None):
        """
        Initialize the NFL database.

        Args:
            db_path: Path to the SQLite database file. When ``None`` (the
                default), falls back to the ``NFL_MCP_DB_PATH`` environment
                variable, or ``nfl_data.db`` if that is unset. Resolving the
                default here means *every* ``NFLDatabase()`` call in the codebase
                honors the configured path, so a persistent volume can be pointed
                at (e.g. ``NFL_MCP_DB_PATH=/data/nfl_data.db``) without code
                changes. An explicit ``db_path`` (e.g. a test's temp file) wins.
            pool_config: Configuration for connection pooling
        """
        if db_path is None:
            db_path = os.getenv("NFL_MCP_DB_PATH", "nfl_data.db")
        self.db_path = Path(db_path)
        self.pool_config = pool_config or ConnectionPoolConfig()
        self._pool = DatabaseConnectionPool(str(self.db_path), self.pool_config)
        self._ensure_database()

    def _ensure_database(self) -> None:
        """Create database and tables if they don't exist, run migrations."""
        with self._pool.get_connection() as conn:
            # Create schema_version table for migration tracking
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)

            # Get current schema version
            cursor = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
            row = cursor.fetchone()
            current_version = row[0] if row else 0

            # Run migrations
            self._vacuum_after_migration = False
            self._run_migrations(conn, current_version)

            conn.commit()
            if self._vacuum_after_migration:
                # Give the pruned snapshot pages back to the filesystem (once).
                try:
                    conn.execute("VACUUM")
                except sqlite3.Error as e:
                    logger.warning(f"VACUUM after migration failed: {e}")

    def health_check(self) -> dict[str, bool | int | str]:
        """Perform a comprehensive health check of the database."""
        pool_health = self._pool.health_check()

        if not pool_health["healthy"]:
            return pool_health

        try:
            with self._pool.get_connection() as conn:
                # Additional database-specific checks
                athlete_count = conn.execute("SELECT COUNT(*) FROM athletes").fetchone()[0]
                team_count = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]

                # Check for recent data
                cursor = conn.execute("SELECT MAX(updated_at) FROM athletes")
                last_athlete_update = cursor.fetchone()[0]

                cursor = conn.execute("SELECT MAX(updated_at) FROM teams")
                last_team_update = cursor.fetchone()[0]

                pool_health.update({
                    "athlete_count": athlete_count,
                    "team_count": team_count,
                    "last_athlete_update": last_athlete_update,
                    "last_team_update": last_team_update,
                    "schema_version": self.CURRENT_SCHEMA_VERSION
                })
            # Per-feed age (injuries, practice, athletes, schedule, snaps), read
            # after the connection above is back in the pool.
            pool_health["data_freshness"] = self.get_data_freshness()
            return pool_health
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            pool_health.update({
                "healthy": False,
                "error": f"Database check failed: {e!s}"
            })
            return pool_health

    def get_connection_stats(self) -> dict[str, int]:
        """Get connection pool statistics."""
        return {
            "active_connections": self._pool._total_connections,
            "max_connections": self._pool.config.max_connections,
            "pool_utilization": (self._pool._total_connections / self._pool.config.max_connections) * 100
        }

    def close(self) -> None:
        """Close the database connection pool."""
        self._pool.close()

    def __enter__(self) -> "NFLDatabase":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_data_freshness(self) -> dict[str, dict]:
        """How old each cached feed is, in hours.

        Advice built on a stale feed looks exactly like advice built on a fresh
        one. Everything else here labels a guess (`is_fallback`, `stale`,
        placeholder warnings) — injury and roster data did not, so a lineup
        recommendation could be made against a day-old injury report with
        nothing in the output to say so.

        Returns ``{feed: {updated_at, age_hours}}``; a feed with no rows at all
        reports ``age_hours: None`` rather than 0, because "never fetched" and
        "just fetched" must not look alike.
        """
        feeds = {
            "injuries": ("player_injuries", "updated_at"),
            "athletes": ("athletes", "updated_at"),
            "practice_status": ("player_practice_status", "updated_at"),
            "schedule": ("schedule_games", "updated_at"),
            "snaps": ("player_week_stats", "updated_at"),
            "news": ("news_fetch_state", "fetched_at"),
        }
        now = datetime.now(UTC)
        out: dict[str, dict] = {}
        for label, (table, column) in feeds.items():
            try:
                with self._pool.get_connection() as conn:
                    newest = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
            except Exception as e:
                logger.debug(f"freshness check failed for {table}: {e}")
                newest = None
            age = None
            if newest:
                try:
                    ts = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    age = round((now - ts).total_seconds() / 3600.0, 1)
                except (TypeError, ValueError):
                    age = None
            out[label] = {"updated_at": newest, "age_hours": age}
        news = self.get_news_health()
        if news.get("sources"):
            out["news"].update(news)
        return out


# For backward compatibility
AthleteDatabase = NFLDatabase


_shared_db: NFLDatabase | None = None
# Whether ``_shared_db`` was registered by ``set_shared_db`` (the server's
# instance) rather than built lazily here from ``NFL_MCP_DB_PATH``.
_shared_db_injected = False
_shared_db_lock = threading.Lock()


def _default_db_path() -> Path:
    return Path(os.getenv("NFL_MCP_DB_PATH", "nfl_data.db"))


def set_shared_db(db: NFLDatabase | None) -> None:
    """Register ``db`` as the process-wide instance returned by ``get_shared_db``."""
    global _shared_db, _shared_db_injected
    with _shared_db_lock:
        _shared_db = db
        _shared_db_injected = db is not None


def get_shared_db() -> NFLDatabase:
    """Return the process-wide NFLDatabase, creating it on first use.

    Constructing an ``NFLDatabase`` per call opens a fresh connection pool and
    re-runs the schema check every time; tool code shares one instance instead.
    An instance registered with ``set_shared_db`` always wins. A lazily built
    one is rebuilt if ``NFL_MCP_DB_PATH`` now points elsewhere (tests point it
    at a temp file per test), and the old one is closed rather than leaked.
    """
    global _shared_db
    path = _default_db_path()
    stale = None
    with _shared_db_lock:
        db = _shared_db
        if db is not None and _shared_db_injected:
            return db
        if db is None or Path(getattr(db, "db_path", path)) != path:
            stale, db = db, NFLDatabase(str(path))
            _shared_db = db
    if stale is not None:
        try:
            stale.close()
        except Exception:
            pass
    return db


def reset_shared_db() -> None:
    """Close and forget the shared instance (used by the test suite)."""
    global _shared_db, _shared_db_injected
    with _shared_db_lock:
        db, _shared_db = _shared_db, None
        _shared_db_injected = False
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def get_nfl_database() -> "NFLDatabase":
    """Return the shared NFLDatabase instance (see ``get_shared_db``)."""
    return get_shared_db()
