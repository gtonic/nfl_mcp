"""
SQLite database management for NFL athletes and teams data.

This module handles the persistence layer for athlete information fetched from
the Sleeper API and teams information from ESPN API, providing caching and lookup functionality.
Features connection pooling, health checks, optimized indexing, migration support, and async operations.
"""

import sqlite3
import json
import logging
import threading
import time
import asyncio
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, List, Optional, Union, AsyncContextManager
from contextlib import contextmanager, asynccontextmanager
from queue import Queue, Empty
from dataclasses import dataclass

try:
    import aiosqlite
    ASYNC_SUPPORT = True
except ImportError:
    aiosqlite = None
    ASYNC_SUPPORT = False

logger = logging.getLogger(__name__)


@dataclass
class ConnectionPoolConfig:
    """Configuration for database connection pool."""
    max_connections: int = 5
    connection_timeout: float = 30.0
    health_check_interval: float = 60.0


class DatabaseConnectionPool:
    """Simple connection pool for SQLite database with thread safety."""
    
    def __init__(self, db_path: str, config: ConnectionPoolConfig):
        self.db_path = db_path
        self.config = config
        self._pool = Queue(maxsize=config.max_connections)
        self._lock = threading.RLock()
        self._total_connections = 0
        self._last_health_check = 0
        
        # Pre-populate pool with initial connections
        self._initialize_pool()
    
    def _initialize_pool(self):
        """Initialize the connection pool with initial connections."""
        with self._lock:
            for _ in range(min(2, self.config.max_connections)):  # Start with 2 connections
                conn = self._create_connection()
                if conn:
                    self._pool.put(conn)
                    self._total_connections += 1
    
    def _create_connection(self) -> Optional[sqlite3.Connection]:
        """Create a new database connection with proper settings."""
        try:
            conn = sqlite3.connect(
                self.db_path,
                timeout=self.config.connection_timeout,
                check_same_thread=False  # Allow connection sharing between threads
            )
            conn.row_factory = sqlite3.Row  # Enable dict-like access
            
            # Enable WAL mode for better concurrent access
            conn.execute("PRAGMA journal_mode=WAL")
            # Set reasonable timeout for busy database
            conn.execute("PRAGMA busy_timeout=30000")  # 30 seconds
            
            return conn
        except Exception as e:
            logger.error(f"Failed to create database connection: {e}")
            return None
    
    @contextmanager
    def get_connection(self):
        """Get a connection from the pool with automatic cleanup."""
        conn = None
        try:
            # Try to get connection from pool
            try:
                conn = self._pool.get(timeout=5.0)
            except Empty:
                # Pool is empty, try to create new connection if under limit
                with self._lock:
                    if self._total_connections < self.config.max_connections:
                        conn = self._create_connection()
                        if conn:
                            self._total_connections += 1
                    
                if not conn:
                    # Wait longer for a connection to become available
                    conn = self._pool.get(timeout=self.config.connection_timeout)
            
            if not conn:
                raise Exception("Failed to obtain database connection")
            
            # Health check connection if needed
            if self._should_health_check():
                if not self._test_connection(conn):
                    conn.close()
                    conn = self._create_connection()
                    if not conn:
                        raise Exception("Failed to create healthy database connection")
            
            yield conn
            
        finally:
            if conn:
                try:
                    # Return connection to pool
                    self._pool.put_nowait(conn)
                except:
                    # Pool is full, close the connection
                    conn.close()
                    with self._lock:
                        self._total_connections -= 1
    
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
    
    def health_check(self) -> Dict[str, Union[bool, int, str]]:
        """Perform a comprehensive health check of the connection pool."""
        try:
            with self.get_connection() as conn:
                # Test basic connectivity
                conn.execute("SELECT 1").fetchone()
                
                # Get database stats
                stats = {}
                cursor = conn.execute("PRAGMA database_list")
                db_info = cursor.fetchall()
                
                # Check if database file exists and is accessible
                db_size = 0
                if Path(self.db_path).exists():
                    db_size = Path(self.db_path).stat().st_size
                
                return {
                    "healthy": True,
                    "pool_size": self._total_connections,
                    "pool_capacity": self.config.max_connections,
                    "database_size_bytes": db_size,
                    "database_path": str(self.db_path),
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
    
    def close(self):
        """Close all connections in the pool."""
        with self._lock:
            while not self._pool.empty():
                try:
                    conn = self._pool.get_nowait()
                    conn.close()
                except Empty:
                    break
            self._total_connections = 0


class NFLDatabase:
    """SQLite database manager for NFL athlete and teams data with caching and lookup functionality."""
    
    # Database schema version for migrations
    CURRENT_SCHEMA_VERSION = 7
    
    def __init__(self, db_path: str = "nfl_data.db", pool_config: Optional[ConnectionPoolConfig] = None):
        """
        Initialize the NFL database.
        
        Args:
            db_path: Path to the SQLite database file
            pool_config: Configuration for connection pooling
        """
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
            self._run_migrations(conn, current_version)
            
            conn.commit()
    
    def _run_migrations(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Run database migrations from the current version to the latest."""
        migrations = {
            1: self._migration_v1_initial_schema,
            2: self._migration_v2_optimized_indexes,
            3: self._migration_v3_roster_snapshots,
            4: self._migration_v4_transaction_snapshots,
            5: self._migration_v5_matchup_snapshots,
            6: self._migration_v6_player_week_stats,
            7: self._migration_v7_schedule_games,
        }
        
        for version in range(from_version + 1, self.CURRENT_SCHEMA_VERSION + 1):
            if version in migrations:
                logger.info(f"Running migration to version {version}")
                migrations[version](conn)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat())
                )
    
    def _migration_v1_initial_schema(self, conn: sqlite3.Connection) -> None:
        """Migration v1: Create initial database schema."""
        # Create athletes table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS athletes (
                id TEXT PRIMARY KEY,
                full_name TEXT,
                first_name TEXT,
                last_name TEXT,
                team_id TEXT,
                position TEXT,
                status TEXT,
                updated_at TEXT NOT NULL,
                raw JSON
            )
        """)
        
        # Create teams table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY,
                abbreviation TEXT,
                name TEXT,
                display_name TEXT,
                short_display_name TEXT,
                location TEXT,
                color TEXT,
                alternate_color TEXT,
                logo TEXT,
                updated_at TEXT NOT NULL,
                raw JSON
            )
        """)
        
        # Create basic indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_team ON athletes(team_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_name ON athletes(full_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_abbreviation ON teams(abbreviation)")
    
    def _migration_v2_optimized_indexes(self, conn: sqlite3.Connection) -> None:
        """Migration v2: Add optimized indexes for better query performance."""
        # Compound indexes for common query patterns
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_team_position ON athletes(team_id, position)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_position_status ON athletes(position, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_name_search ON athletes(full_name COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_updated ON athletes(updated_at)")
        
        # Team search optimizations
        conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_name_search ON teams(name COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_updated ON teams(updated_at)")
        
        # Covering indexes for frequent lookups
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_athletes_lookup 
            ON athletes(id, full_name, team_id, position, status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_teams_lookup 
            ON teams(id, abbreviation, name, display_name)
        """)

    def _migration_v3_roster_snapshots(self, conn: sqlite3.Connection) -> None:
        """Migration v3: Add roster_snapshots table for robust roster fallback."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS roster_snapshots (
                id INTEGER PRIMARY KEY,
                league_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_roster_snapshots_league_time
            ON roster_snapshots (league_id, fetched_at DESC)
            """
        )

    def _migration_v4_transaction_snapshots(self, conn: sqlite3.Connection) -> None:
        """Migration v4: Add transaction_snapshots table for robust transactions fallback."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transaction_snapshots (
                id INTEGER PRIMARY KEY,
                league_id TEXT NOT NULL,
                week INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_transaction_snapshots_league_week_time
            ON transaction_snapshots (league_id, week, fetched_at DESC)
            """
        )

    def _migration_v5_matchup_snapshots(self, conn: sqlite3.Connection) -> None:
        """Migration v5: Add matchup_snapshots table for robust matchup fallback."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS matchup_snapshots (
                id INTEGER PRIMARY KEY,
                league_id TEXT NOT NULL,
                week INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_matchup_snapshots_league_week_time
            ON matchup_snapshots (league_id, week, fetched_at DESC)
            """
        )

    def _migration_v6_player_week_stats(self, conn: sqlite3.Connection) -> None:
        """Migration v6: Table for caching per-player weekly snap statistics."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_week_stats (
                player_id TEXT NOT NULL,
                season INTEGER NOT NULL,
                week INTEGER NOT NULL,
                snaps_offense INTEGER,
                snaps_team_offense INTEGER,
                snap_pct REAL,
                updated_at TEXT NOT NULL,
                raw JSON,
                PRIMARY KEY (player_id, season, week)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_pws_lookup ON player_week_stats (player_id, season, week)"""
        )

    def _migration_v7_schedule_games(self, conn: sqlite3.Connection) -> None:
        """Migration v7: Table for caching schedule to derive opponent for a team/DEF."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_games (
                season INTEGER NOT NULL,
                week INTEGER NOT NULL,
                team TEXT NOT NULL,
                opponent TEXT NOT NULL,
                is_home INTEGER NOT NULL,
                kickoff TEXT,
                raw JSON,
                PRIMARY KEY (season, week, team)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_sched_week ON schedule_games (season, week)"""
        )
    
    @contextmanager
    def _get_connection(self):
        """Get a database connection with proper cleanup. (Legacy method for compatibility)"""
        with self._pool.get_connection() as conn:
            yield conn
    
    def health_check(self) -> Dict[str, Union[bool, int, str]]:
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
                
                return pool_health
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            pool_health.update({
                "healthy": False,
                "error": f"Database check failed: {str(e)}"
            })
            return pool_health
    
    def get_connection_stats(self) -> Dict[str, int]:
        """Get connection pool statistics."""
        return {
            "active_connections": self._pool._total_connections,
            "max_connections": self._pool.config.max_connections,
            "pool_utilization": (self._pool._total_connections / self._pool.config.max_connections) * 100
        }
    
    def close(self) -> None:
        """Close the database connection pool."""
        self._pool.close()

    # ------------------------------------------------------------------
    # Roster snapshot helpers
    # ------------------------------------------------------------------
    def save_roster_snapshot(self, league_id: str, rosters) -> None:
        """Persist latest roster payload snapshot (JSON serialized)."""
        try:
            import json, datetime
            with self._pool.get_connection() as conn:
                conn.execute(
                    "INSERT INTO roster_snapshots (league_id, payload_json, fetched_at) VALUES (?,?,?)",
                    (league_id, json.dumps(rosters), datetime.datetime.utcnow().isoformat())
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"save_roster_snapshot failed: {e}")

    def load_roster_snapshot(self, league_id: str, ttl_minutes: int = 15):
        """Load most recent roster snapshot; mark stale if beyond TTL."""
        try:
            import json, datetime
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT payload_json, fetched_at FROM roster_snapshots WHERE league_id=? ORDER BY fetched_at DESC LIMIT 1",
                    (league_id,)
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload_json, fetched_at = row
                dt = datetime.datetime.fromisoformat(fetched_at)
                age_seconds = (datetime.datetime.utcnow() - dt).total_seconds()
                stale = age_seconds > ttl_minutes * 60
                return {"rosters": json.loads(payload_json), "stale": stale, "fetched_at": fetched_at, "age_seconds": age_seconds}
        except Exception as e:
            logger.debug(f"load_roster_snapshot failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Transaction snapshot helpers
    # ------------------------------------------------------------------
    def save_transaction_snapshot(self, league_id: str, week: int, transactions) -> None:
        """Persist latest transactions payload snapshot keyed by league/week."""
        try:
            import json, datetime
            with self._pool.get_connection() as conn:
                conn.execute(
                    "INSERT INTO transaction_snapshots (league_id, week, payload_json, fetched_at) VALUES (?,?,?,?)",
                    (league_id, week, json.dumps(transactions), datetime.datetime.utcnow().isoformat())
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"save_transaction_snapshot failed: {e}")

    def load_transaction_snapshot(self, league_id: str, week: Optional[int] = None, ttl_minutes: int = 15):
        """Load most recent transactions snapshot for league (and week if provided)."""
        try:
            import json, datetime
            with self._pool.get_connection() as conn:
                if week is not None:
                    cur = conn.execute(
                        "SELECT week, payload_json, fetched_at FROM transaction_snapshots WHERE league_id=? AND week=? ORDER BY fetched_at DESC LIMIT 1",
                        (league_id, week)
                    )
                else:
                    cur = conn.execute(
                        "SELECT week, payload_json, fetched_at FROM transaction_snapshots WHERE league_id=? ORDER BY fetched_at DESC LIMIT 1",
                        (league_id,)
                    )
                row = cur.fetchone()
                if not row:
                    return None
                snap_week, payload_json, fetched_at = row
                dt = datetime.datetime.fromisoformat(fetched_at)
                age_seconds = (datetime.datetime.utcnow() - dt).total_seconds()
                stale = age_seconds > ttl_minutes * 60
                return {"transactions": json.loads(payload_json), "week": snap_week, "stale": stale, "fetched_at": fetched_at, "age_seconds": age_seconds}
        except Exception as e:
            logger.debug(f"load_transaction_snapshot failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Matchup snapshot helpers
    # ------------------------------------------------------------------
    def save_matchup_snapshot(self, league_id: str, week: int, matchups) -> None:
        try:
            import json, datetime
            with self._pool.get_connection() as conn:
                conn.execute(
                    "INSERT INTO matchup_snapshots (league_id, week, payload_json, fetched_at) VALUES (?,?,?,?)",
                    (league_id, week, json.dumps(matchups), datetime.datetime.utcnow().isoformat())
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"save_matchup_snapshot failed: {e}")

    def load_matchup_snapshot(self, league_id: str, week: int, ttl_minutes: int = 15):
        try:
            import json, datetime
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT payload_json, fetched_at FROM matchup_snapshots WHERE league_id=? AND week=? ORDER BY fetched_at DESC LIMIT 1",
                    (league_id, week)
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload_json, fetched_at = row
                dt = datetime.datetime.fromisoformat(fetched_at)
                age_seconds = (datetime.datetime.utcnow() - dt).total_seconds()
                stale = age_seconds > ttl_minutes * 60
                return {"matchups": json.loads(payload_json), "stale": stale, "fetched_at": fetched_at, "age_seconds": age_seconds}
        except Exception as e:
            logger.debug(f"load_matchup_snapshot failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Player weekly snap stats helpers
    # ------------------------------------------------------------------
    def upsert_player_week_stats(self, stats: List[Dict]) -> int:
        """Insert or update player weekly snap stats.

        Expected dict keys per item: player_id, season, week, snaps_offense, snaps_team_offense, snap_pct (optional), raw (optional)
        If snap_pct is missing but snaps_offense and snaps_team_offense are present and >0, it's computed.
        """
        if not stats:
            return 0
        now = datetime.now(UTC).isoformat()
        processed = 0
        with self._pool.get_connection() as conn:
            try:
                for s in stats:
                    player_id = s.get("player_id")
                    season = s.get("season")
                    week = s.get("week")
                    if player_id is None or season is None or week is None:
                        continue  # skip invalid rows silently
                    snaps_off = s.get("snaps_offense")
                    snaps_team = s.get("snaps_team_offense")
                    snap_pct = s.get("snap_pct")
                    if snap_pct is None and snaps_off is not None and snaps_team not in (None, 0):
                        try:
                            snap_pct = round((snaps_off / snaps_team) * 100, 1)
                        except Exception:
                            snap_pct = None
                    raw = s.get("raw", {})
                    conn.execute(
                        """
                        INSERT INTO player_week_stats(
                            player_id, season, week, snaps_offense, snaps_team_offense, snap_pct, updated_at, raw
                        ) VALUES(?,?,?,?,?,?,?, json(?))
                        ON CONFLICT(player_id, season, week) DO UPDATE SET
                            snaps_offense=excluded.snaps_offense,
                            snaps_team_offense=excluded.snaps_team_offense,
                            snap_pct=excluded.snap_pct,
                            updated_at=excluded.updated_at,
                            raw=excluded.raw
                        """,
                        (
                            player_id,
                            season,
                            week,
                            snaps_off,
                            snaps_team,
                            snap_pct,
                            now,
                            json.dumps(raw),
                        ),
                    )
                    processed += 1
                conn.commit()
                return processed
            except Exception as e:
                logger.error(f"upsert_player_week_stats failed: {e}")
                conn.rollback()
                return processed

    def get_player_snap_pct(self, player_id: str, season: int, week: int) -> Optional[Dict]:
        """Fetch cached snap percentage info for a player/week."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT snap_pct, snaps_offense, snaps_team_offense, updated_at
                    FROM player_week_stats
                    WHERE player_id=? AND season=? AND week=?
                    """,
                    (player_id, season, week),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return dict(row)
        except Exception as e:
            logger.debug(f"get_player_snap_pct failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Schedule / opponent helpers
    # ------------------------------------------------------------------
    def upsert_schedule_games(self, games: List[Dict]) -> int:
        """Insert or update schedule games for opponent lookup.

        Each dict requires: season, week, team, opponent, is_home (bool/int), kickoff (optional), raw(optional)
        Caller should provide both directions (team/opponent swapped) if desired.
        """
        if not games:
            return 0
        processed = 0
        with self._pool.get_connection() as conn:
            try:
                for g in games:
                    season = g.get("season")
                    week = g.get("week")
                    team = g.get("team")
                    opponent = g.get("opponent")
                    if None in (season, week, team, opponent):
                        continue
                    is_home = 1 if g.get("is_home") else 0
                    kickoff = g.get("kickoff")
                    raw = g.get("raw", {})
                    conn.execute(
                        """
                        INSERT INTO schedule_games(season, week, team, opponent, is_home, kickoff, raw)
                        VALUES(?,?,?,?,?,?, json(?))
                        ON CONFLICT(season, week, team) DO UPDATE SET
                            opponent=excluded.opponent,
                            is_home=excluded.is_home,
                            kickoff=excluded.kickoff,
                            raw=excluded.raw
                        """,
                        (season, week, team, opponent, is_home, kickoff, json.dumps(raw)),
                    )
                    processed += 1
                conn.commit()
                return processed
            except Exception as e:
                logger.error(f"upsert_schedule_games failed: {e}")
                conn.rollback()
                return processed

    def get_opponent(self, season: int, week: int, team: str) -> Optional[str]:
        """Return opponent abbreviation for team in given season/week if cached."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT opponent FROM schedule_games WHERE season=? AND week=? AND team=?",
                    (season, week, team),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return row[0]
        except Exception as e:
            logger.debug(f"get_opponent failed: {e}")
            return None
    
    # Async Database Operations
    # These methods provide async alternatives to the main database operations
    
    @asynccontextmanager
    async def _get_async_connection(self):  # Return type left un-annotated to satisfy runtime and avoid mismatched protocol
        """Get an async database connection with proper cleanup."""
        if not ASYNC_SUPPORT:
            raise RuntimeError("Async operations require aiosqlite. Install with: pip install aiosqlite")
        
        async with aiosqlite.connect(str(self.db_path)) as conn:
            # Enable WAL mode and set timeouts
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = aiosqlite.Row
            yield conn
    
    async def async_health_check(self) -> Dict[str, Union[bool, int, str]]:
        """Perform an async health check of the database."""
        if not ASYNC_SUPPORT:
            return {
                "healthy": False,
                "error": "Async operations not supported. Install aiosqlite.",
                "last_check": datetime.now(UTC).isoformat()
            }
        
        try:
            async with self._get_async_connection() as conn:
                # Test basic connectivity
                await conn.execute("SELECT 1")
                
                # Get database stats
                athlete_count = await conn.execute_fetchall("SELECT COUNT(*) FROM athletes")
                team_count = await conn.execute_fetchall("SELECT COUNT(*) FROM teams")
                
                last_athlete_update = await conn.execute_fetchall("SELECT MAX(updated_at) FROM athletes")
                last_team_update = await conn.execute_fetchall("SELECT MAX(updated_at) FROM teams")
                
                # Get database size
                db_size = 0
                if self.db_path.exists():
                    db_size = self.db_path.stat().st_size
                
                return {
                    "healthy": True,
                    "athlete_count": athlete_count[0][0] if athlete_count else 0,
                    "team_count": team_count[0][0] if team_count else 0,
                    "last_athlete_update": last_athlete_update[0][0] if last_athlete_update and last_athlete_update[0][0] else None,
                    "last_team_update": last_team_update[0][0] if last_team_update and last_team_update[0][0] else None,
                    "database_size_bytes": db_size,
                    "schema_version": self.CURRENT_SCHEMA_VERSION,
                    "async_support": True,
                    "last_check": datetime.now(UTC).isoformat()
                }
        except Exception as e:
            logger.error(f"Async database health check failed: {e}")
            return {
                "healthy": False,
                "error": str(e),
                "async_support": True,
                "last_check": datetime.now(UTC).isoformat()
            }
    
    async def async_get_athlete_by_id(self, athlete_id: str) -> Optional[Dict]:
        """
        Async version: Get athlete by ID.
        
        Args:
            athlete_id: The athlete's unique identifier
            
        Returns:
            Athlete dictionary or None if not found
        """
        if not ASYNC_SUPPORT:
            raise RuntimeError("Async operations require aiosqlite")
        
        async with self._get_async_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM athletes WHERE id = ?", 
                (athlete_id,)
            )
            row = await cursor.fetchone()
            
            if row:
                return dict(row)
            return None
    
    async def async_search_athletes_by_name(self, name: str, limit: int = 10) -> List[Dict]:
        """
        Async version: Search athletes by name (partial match).
        
        Args:
            name: Name to search for (partial match supported)
            limit: Maximum number of results to return
            
        Returns:
            List of matching athlete dictionaries
        """
        if not ASYNC_SUPPORT:
            raise RuntimeError("Async operations require aiosqlite")
        
        search_term = f"%{name}%"
        
        async with self._get_async_connection() as conn:
            cursor = await conn.execute("""
                SELECT * FROM athletes 
                WHERE full_name LIKE ? OR first_name LIKE ? OR last_name LIKE ?
                ORDER BY full_name
                LIMIT ?
            """, (search_term, search_term, search_term, limit))
            
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def async_get_athletes_by_team(self, team_id: str) -> List[Dict]:
        """
        Async version: Get all athletes for a specific team.
        
        Args:
            team_id: The team identifier
            
        Returns:
            List of athlete dictionaries for the team
        """
        if not ASYNC_SUPPORT:
            raise RuntimeError("Async operations require aiosqlite")
        
        async with self._get_async_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM athletes WHERE team_id = ? ORDER BY full_name",
                (team_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def async_get_team_by_id(self, team_id: str) -> Optional[Dict]:
        """
        Async version: Get team by ID.
        
        Args:
            team_id: The team's unique identifier
            
        Returns:
            Team dictionary or None if not found
        """
        if not ASYNC_SUPPORT:
            raise RuntimeError("Async operations require aiosqlite")
        
        async with self._get_async_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM teams WHERE id = ?", 
                (team_id,)
            )
            row = await cursor.fetchone()
            
            if row:
                return dict(row)
            return None
    
    async def async_get_team_by_abbreviation(self, abbreviation: str) -> Optional[Dict]:
        """
        Async version: Get team by abbreviation (e.g., 'KC', 'TB').
        
        Args:
            abbreviation: The team's abbreviation
            
        Returns:
            Team dictionary or None if not found
        """
        if not ASYNC_SUPPORT:
            raise RuntimeError("Async operations require aiosqlite")
        
        async with self._get_async_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM teams WHERE abbreviation = ?", 
                (abbreviation,)
            )
            row = await cursor.fetchone()
            
            if row:
                return dict(row)
            return None
    
    async def async_get_all_teams(self) -> List[Dict]:
        """
        Async version: Get all teams from the database.
        
        Returns:
            List of all team dictionaries
        """
        if not ASYNC_SUPPORT:
            raise RuntimeError("Async operations require aiosqlite")
        
        async with self._get_async_connection() as conn:
            cursor = await conn.execute("SELECT * FROM teams ORDER BY name")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def async_upsert_athletes(self, athletes_data: List[Dict]) -> int:
        """
        Async version: Insert or update athlete records.
        
        Args:
            athletes_data: List of athlete dictionaries from Sleeper API
            
        Returns:
            Number of athletes processed
        """
        if not ASYNC_SUPPORT:
            raise RuntimeError("Async operations require aiosqlite")
        
        if not athletes_data:
            return 0
        
        updated_at = datetime.now(UTC).isoformat()
        processed_count = 0
        
        async with self._get_async_connection() as conn:
            try:
                for athlete_id, athlete in athletes_data.items():
                    # Extract key fields with safe defaults
                    full_name = athlete.get('full_name', '') or ''
                    first_name = athlete.get('first_name', '') or ''
                    last_name = athlete.get('last_name', '') or ''
                    team_id = athlete.get('team', '') or ''
                    position = athlete.get('position', '') or ''
                    status = athlete.get('status', '') or ''
                    
                    # Store the complete raw data as JSON
                    raw_json = json.dumps(athlete)
                    
                    # Upsert the athlete record
                    await conn.execute("""
                        INSERT INTO athletes(
                            id, full_name, first_name, last_name, 
                            team_id, position, status, updated_at, raw
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, json(?))
                        ON CONFLICT(id) DO UPDATE SET 
                            full_name=excluded.full_name,
                            first_name=excluded.first_name,
                            last_name=excluded.last_name,
                            team_id=excluded.team_id,
                            position=excluded.position,
                            status=excluded.status,
                            updated_at=excluded.updated_at,
                            raw=excluded.raw
                    """, (
                        athlete_id, full_name, first_name, last_name,
                        team_id, position, status, updated_at, raw_json
                    ))
                    processed_count += 1
                
                await conn.commit()
                logger.info(f"Successfully processed {processed_count} athletes (async)")
                return processed_count
                
            except Exception as e:
                await conn.rollback()
                logger.error(f"Error upserting athletes (async): {e}")
                raise
    
    async def async_upsert_teams(self, teams_data: List[Dict]) -> int:
        """
        Async version: Insert or update team records.
        
        Args:
            teams_data: List of team dictionaries from ESPN API
            
        Returns:
            Number of teams processed
        """
        if not ASYNC_SUPPORT:
            raise RuntimeError("Async operations require aiosqlite")
        
        if not teams_data:
            return 0
        
        updated_at = datetime.now(UTC).isoformat()
        processed_count = 0
        
        async with self._get_async_connection() as conn:
            try:
                for team in teams_data:
                    # Extract key fields with safe defaults
                    team_id = team.get('id', '') or ''
                    abbreviation = team.get('abbreviation', '') or ''
                    name = team.get('name', '') or ''
                    display_name = team.get('displayName', '') or ''
                    short_display_name = team.get('shortDisplayName', '') or ''
                    location = team.get('location', '') or ''
                    color = team.get('color', '') or ''
                    alternate_color = team.get('alternateColor', '') or ''
                    logo = team.get('logo', '') or ''
                    
                    # Store the complete raw data as JSON
                    raw_json = json.dumps(team)
                    
                    # Upsert the team record
                    await conn.execute("""
                        INSERT INTO teams(
                            id, abbreviation, name, display_name, short_display_name,
                            location, color, alternate_color, logo, updated_at, raw
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, json(?))
                        ON CONFLICT(id) DO UPDATE SET 
                            abbreviation=excluded.abbreviation,
                            name=excluded.name,
                            display_name=excluded.display_name,
                            short_display_name=excluded.short_display_name,
                            location=excluded.location,
                            color=excluded.color,
                            alternate_color=excluded.alternate_color,
                            logo=excluded.logo,
                            updated_at=excluded.updated_at,
                            raw=excluded.raw
                    """, (
                        team_id, abbreviation, name, display_name, short_display_name,
                        location, color, alternate_color, logo, updated_at, raw_json
                    ))
                    processed_count += 1
                
                await conn.commit()
                logger.info(f"Successfully processed {processed_count} teams (async)")
                return processed_count
                
            except Exception as e:
                await conn.rollback()
                logger.error(f"Error upserting teams (async): {e}")
                raise
    
    def upsert_athletes(self, athletes_data: List[Dict]) -> int:
        """
        Insert or update athlete records.
        
        Args:
            athletes_data: List of athlete dictionaries from Sleeper API
            
        Returns:
            Number of athletes processed
        """
        if not athletes_data:
            return 0
        
        updated_at = datetime.now(UTC).isoformat()
        processed_count = 0
        
        with self._get_connection() as conn:
            try:
                for athlete_id, athlete in athletes_data.items():
                    # Extract key fields with safe defaults
                    full_name = athlete.get('full_name', '') or ''
                    first_name = athlete.get('first_name', '') or ''
                    last_name = athlete.get('last_name', '') or ''
                    team_id = athlete.get('team', '') or ''
                    position = athlete.get('position', '') or ''
                    status = athlete.get('status', '') or ''
                    
                    # Store the complete raw data as JSON
                    raw_json = json.dumps(athlete)
                    
                    # Upsert the athlete record
                    conn.execute("""
                        INSERT INTO athletes(
                            id, full_name, first_name, last_name, 
                            team_id, position, status, updated_at, raw
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, json(?))
                        ON CONFLICT(id) DO UPDATE SET 
                            full_name=excluded.full_name,
                            first_name=excluded.first_name,
                            last_name=excluded.last_name,
                            team_id=excluded.team_id,
                            position=excluded.position,
                            status=excluded.status,
                            updated_at=excluded.updated_at,
                            raw=excluded.raw
                    """, (
                        athlete_id, full_name, first_name, last_name,
                        team_id, position, status, updated_at, raw_json
                    ))
                    processed_count += 1
                
                conn.commit()
                logger.info(f"Successfully processed {processed_count} athletes")
                return processed_count
                
            except Exception as e:
                conn.rollback()
                logger.error(f"Error upserting athletes: {e}")
                raise
    
    def get_athlete_by_id(self, athlete_id: str) -> Optional[Dict]:
        """
        Get athlete by ID.
        
        Args:
            athlete_id: The athlete's unique identifier
            
        Returns:
            Athlete dictionary or None if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM athletes WHERE id = ?", 
                (athlete_id,)
            )
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
    
    def search_athletes_by_name(self, name: str, limit: int = 10) -> List[Dict]:
        """
        Search athletes by name (partial match).
        
        Args:
            name: Name to search for (partial match supported)
            limit: Maximum number of results to return
            
        Returns:
            List of matching athlete dictionaries
        """
        search_term = f"%{name}%"
        
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM athletes 
                WHERE full_name LIKE ? OR first_name LIKE ? OR last_name LIKE ?
                ORDER BY full_name
                LIMIT ?
            """, (search_term, search_term, search_term, limit))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_athletes_by_team(self, team_id: str) -> List[Dict]:
        """
        Get all athletes for a specific team.
        
        Args:
            team_id: The team identifier
            
        Returns:
            List of athlete dictionaries for the team
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM athletes WHERE team_id = ? ORDER BY full_name",
                (team_id,)
            )
            return [dict(row) for row in cursor.fetchall()]
    
    def get_athlete_count(self) -> int:
        """
        Get the total number of athletes in the database.
        
        Returns:
            Total count of athletes
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM athletes")
            return cursor.fetchone()[0]
    
    def get_last_updated(self) -> Optional[str]:
        """
        Get the timestamp of the most recent update.
        
        Returns:
            ISO timestamp string or None if no data
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT MAX(updated_at) FROM athletes"
            )
            result = cursor.fetchone()[0]
            return result
    
    def clear_athletes(self) -> int:
        """
        Clear all athlete data from the database.
        
        Returns:
            Number of records deleted
        """
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM athletes")
            count = cursor.rowcount
            conn.commit()
            logger.info(f"Cleared {count} athlete records")
            return count

    # Teams-related methods
    
    def upsert_teams(self, teams_data: List[Dict]) -> int:
        """
        Insert or update team records.
        
        Args:
            teams_data: List of team dictionaries from ESPN API
            
        Returns:
            Number of teams processed
        """
        if not teams_data:
            return 0
        
        updated_at = datetime.now(UTC).isoformat()
        processed_count = 0
        
        with self._get_connection() as conn:
            try:
                for team in teams_data:
                    # Extract key fields with safe defaults
                    team_id = team.get('id', '') or ''
                    abbreviation = team.get('abbreviation', '') or ''
                    name = team.get('name', '') or ''
                    display_name = team.get('displayName', '') or ''
                    short_display_name = team.get('shortDisplayName', '') or ''
                    location = team.get('location', '') or ''
                    color = team.get('color', '') or ''
                    alternate_color = team.get('alternateColor', '') or ''
                    logo = team.get('logo', '') or ''
                    
                    # Store the complete raw data as JSON
                    raw_json = json.dumps(team)
                    
                    # Upsert the team record
                    conn.execute("""
                        INSERT INTO teams(
                            id, abbreviation, name, display_name, short_display_name,
                            location, color, alternate_color, logo, updated_at, raw
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, json(?))
                        ON CONFLICT(id) DO UPDATE SET 
                            abbreviation=excluded.abbreviation,
                            name=excluded.name,
                            display_name=excluded.display_name,
                            short_display_name=excluded.short_display_name,
                            location=excluded.location,
                            color=excluded.color,
                            alternate_color=excluded.alternate_color,
                            logo=excluded.logo,
                            updated_at=excluded.updated_at,
                            raw=excluded.raw
                    """, (
                        team_id, abbreviation, name, display_name, short_display_name,
                        location, color, alternate_color, logo, updated_at, raw_json
                    ))
                    processed_count += 1
                
                conn.commit()
                logger.info(f"Successfully processed {processed_count} teams")
                return processed_count
                
            except Exception as e:
                conn.rollback()
                logger.error(f"Error upserting teams: {e}")
                raise
    
    def get_team_by_id(self, team_id: str) -> Optional[Dict]:
        """
        Get team by ID.
        
        Args:
            team_id: The team's unique identifier
            
        Returns:
            Team dictionary or None if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM teams WHERE id = ?", 
                (team_id,)
            )
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
    
    def get_team_by_abbreviation(self, abbreviation: str) -> Optional[Dict]:
        """
        Get team by abbreviation (e.g., 'KC', 'TB').
        
        Args:
            abbreviation: The team's abbreviation
            
        Returns:
            Team dictionary or None if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM teams WHERE abbreviation = ?", 
                (abbreviation,)
            )
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
    
    def get_all_teams(self) -> List[Dict]:
        """
        Get all teams from the database.
        
        Returns:
            List of all team dictionaries
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM teams ORDER BY name")
            return [dict(row) for row in cursor.fetchall()]
    
    def get_team_count(self) -> int:
        """
        Get the total number of teams in the database.
        
        Returns:
            Total count of teams
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM teams")
            return cursor.fetchone()[0]
    
    def get_teams_last_updated(self) -> Optional[str]:
        """
        Get the timestamp of the most recent teams update.
        
        Returns:
            ISO timestamp string or None if no data
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT MAX(updated_at) FROM teams"
            )
            result = cursor.fetchone()[0]
            return result
    
    def clear_teams(self) -> int:
        """
        Clear all team data from the database.
        
        Returns:
            Number of records deleted
        """
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM teams")
            count = cursor.rowcount
            conn.commit()
            logger.info(f"Cleared {count} team records")
            return count


# For backward compatibility
AthleteDatabase = NFLDatabase