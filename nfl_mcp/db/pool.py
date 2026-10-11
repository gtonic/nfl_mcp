"""SQLite connection pool used by ``NFLDatabase``."""
import sqlite3
import threading
import time
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue

from ._common import logger


@dataclass
class ConnectionPoolConfig:
    """Configuration for database connection pool."""
    max_connections: int = 5
    connection_timeout: float = 30.0
    health_check_interval: float = 60.0


class _PooledConnection(sqlite3.Connection):
    """A pool's connection, tagged with the pool generation it belongs to (a
    plain ``sqlite3.Connection`` takes no attributes)."""

    pool_generation = 0


# Pools opened in this process and not yet garbage-collected (weak), for
# `open_connection_count` and the test suite's leak check.
_live_pools: "weakref.WeakSet[DatabaseConnectionPool]" = weakref.WeakSet()


def open_connection_count() -> int:
    """Connections currently open across every live pool (idle + borrowed)."""
    return sum(p.open_connections for p in list(_live_pools))


def _close_idle(queue: Queue) -> None:
    """Close every connection waiting in ``queue`` (a pool's finalizer)."""
    while True:
        try:
            conn = queue.get_nowait()
        except Empty:
            return
        try:
            conn.close()
        except Exception:
            pass


class DatabaseConnectionPool:
    """Simple connection pool for SQLite database with thread safety.

    ``close()`` closes the idle connections at once and a borrowed one when it
    is returned (it belongs to the old generation); the pool stays usable and
    opens fresh connections on demand. Before, a connection borrowed during
    ``close()`` went back into the drained pool and was never closed.
    """

    def __init__(self, db_path: str, config: ConnectionPoolConfig):
        self.db_path = db_path
        self.config = config
        self._pool: Queue[sqlite3.Connection] = Queue(maxsize=config.max_connections)
        self._lock = threading.RLock()
        self._total_connections = 0
        self._last_health_check: float = 0
        self._generation = 0
        _live_pools.add(self)
        # A pool dropped without close() still closes its idle connections
        # (borrowed ones keep the pool alive until returned). Before, the
        # garbage collector closed them one by one, each with an "unclosed
        # database" ResourceWarning.
        weakref.finalize(self, _close_idle, self._pool)

        # Pre-populate pool with initial connections
        self._initialize_pool()

    @property
    def open_connections(self) -> int:
        return self._total_connections

    def _initialize_pool(self) -> None:
        """Initialize the connection pool with initial connections."""
        with self._lock:
            for _ in range(min(2, self.config.max_connections)):  # Start with 2 connections
                conn = self._create_connection()
                if conn:
                    self._pool.put(conn)
                    self._total_connections += 1

    def _create_connection(self) -> sqlite3.Connection | None:
        """Create a new database connection with proper settings."""
        conn = None
        try:
            conn = sqlite3.connect(
                self.db_path,
                timeout=self.config.connection_timeout,
                check_same_thread=False,  # Allow connection sharing between threads
                factory=_PooledConnection,
            )
            conn.pool_generation = self._generation
            conn.row_factory = sqlite3.Row  # Enable dict-like access

            # Enable WAL mode for better concurrent access
            conn.execute("PRAGMA journal_mode=WAL")
            # Set reasonable timeout for busy database
            conn.execute("PRAGMA busy_timeout=30000")  # 30 seconds
            # Truncate the WAL back to <=16 MB after checkpoints instead of
            # leaving it at its high-water mark.
            conn.execute("PRAGMA journal_size_limit=16777216")

            return conn
        except Exception as e:
            logger.error(f"Failed to create database connection: {e}")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return None

    @contextmanager
    def get_connection(self) -> Iterator[sqlite3.Connection]:
        """Get a connection from the pool with automatic cleanup."""
        conn = None
        try:
            # An idle connection, else a new one while under the limit, else
            # wait for one to come back. (The idle check used to *block* for
            # 5 s first -- on the event loop's thread -- before opening the
            # connection the limit allowed.)
            try:
                conn = self._pool.get_nowait()
            except Empty:
                with self._lock:
                    if self._total_connections < self.config.max_connections:
                        conn = self._create_connection()
                        if conn:
                            self._total_connections += 1

                if not conn:
                    # Wait for a connection to become available
                    conn = self._pool.get(timeout=self.config.connection_timeout)

            if not conn:
                raise Exception("Failed to obtain database connection")

            # Health check connection if needed
            if self._should_health_check() and not self._test_connection(conn):
                conn.close()
                conn = self._create_connection()
                if not conn:
                    # The dead connection is gone and no replacement exists:
                    # free its slot, or the pool believes it is full forever.
                    with self._lock:
                        self._total_connections -= 1
                    raise Exception("Failed to create healthy database connection")

            yield conn

        finally:
            if conn and self._reset_connection(conn):
                if getattr(conn, "pool_generation", self._generation) != self._generation:
                    # Borrowed before close(): close it rather than pool it.
                    self._discard(conn)
                else:
                    try:
                        # Return connection to pool
                        self._pool.put_nowait(conn)
                    except Full:
                        # Pool is full, close the connection
                        self._discard(conn)

    def _discard(self, conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            self._total_connections = max(0, self._total_connections - 1)

    def _reset_connection(self, conn: sqlite3.Connection) -> bool:
        """Roll back whatever the borrower left open; False if ``conn`` was discarded.

        A writer that raised (or returned without committing) leaves its write
        transaction open, and with it the database's single write lock. Pooled
        as-is, every other writer then waits out ``busy_timeout`` and fails with
        "database is locked" — and the next borrower's ``commit()`` would persist
        the half-written batch. So nothing goes back into the pool mid-transaction.
        """
        try:
            if conn.in_transaction:
                logger.warning("Rolling back uncommitted transaction on pooled connection")
                conn.rollback()
            return True
        except Exception as e:
            # A connection that cannot roll back may still hold the lock: drop it.
            logger.error(f"Discarding pooled connection after failed rollback: {e}")
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._total_connections -= 1
            return False

    def _should_health_check(self) -> bool:
        """Check if it's time for a health check."""
        now = time.time()
        if now - self._last_health_check > self.config.health_check_interval:
            self._last_health_check = now
            return True
        return False

    def _test_connection(self, conn: sqlite3.Connection) -> bool:
        """Test if a connection is healthy."""
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def health_check(self) -> dict[str, bool | int | str]:
        """Perform a comprehensive health check of the connection pool."""
        try:
            with self.get_connection() as conn:
                # Test basic connectivity
                conn.execute("SELECT 1").fetchone()

                # Get database stats
                cursor = conn.execute("PRAGMA database_list")
                cursor.fetchall()

                # Check if database file exists and is accessible
                db_size = 0
                if Path(self.db_path).exists():
                    db_size = Path(self.db_path).stat().st_size

                return {
                    "healthy": True,
                    "pool_size": self._total_connections,
                    "pool_capacity": self.config.max_connections,
                    "database_size_bytes": db_size,
                    "wal_mode": "enabled",
                    "last_check": datetime.now(UTC).isoformat()
                }
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return {
                "healthy": False,
                "error": str(e),
                "pool_size": self._total_connections,
                "last_check": datetime.now(UTC).isoformat()
            }

    def close(self) -> None:
        """Close the idle connections now and the borrowed ones on return."""
        with self._lock:
            self._generation += 1
            while True:
                try:
                    conn = self._pool.get_nowait()
                except Empty:
                    break
                self._discard(conn)
