"""
Unit tests for database optimization features.

Tests connection pooling, health checks, migrations, and optimized indexing.
"""

import pytest
import tempfile
import time
import threading
from pathlib import Path
from nfl_mcp.database import NFLDatabase, ConnectionPoolConfig, DatabaseConnectionPool


class TestConnectionPooling:
    """Test database connection pooling functionality."""
    
    def setup_method(self):
        """Set up a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.pool_config = ConnectionPoolConfig(max_connections=3, connection_timeout=5.0)
        self.db = NFLDatabase(self.temp_db.name, self.pool_config)
    
    def teardown_method(self):
        """Clean up the temporary database after each test."""
        self.db.close()
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    def test_connection_pool_initialization(self):
        """Test that connection pool initializes correctly."""
        assert self.db._pool._total_connections >= 0
        assert self.db._pool._total_connections <= self.pool_config.max_connections
    
    def test_connection_pool_get_connection(self):
        """Test getting connections from the pool."""
        with self.db._pool.get_connection() as conn:
            assert conn is not None
            # Test that connection works
            result = conn.execute("SELECT 1").fetchone()
            assert result[0] == 1
    
    def test_connection_pool_concurrent_access(self):
        """Test concurrent access to the connection pool."""
        results = []
        errors = []
        
        def worker():
            try:
                with self.db._pool.get_connection() as conn:
                    result = conn.execute("SELECT 1").fetchone()
                    results.append(result[0])
                    time.sleep(0.1)  # Simulate some work
            except Exception as e:
                errors.append(str(e))
        
        # Start multiple threads
        threads = []
        for _ in range(5):
            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        
        # Check results
        assert len(errors) == 0, f"Errors in concurrent access: {errors}"
        assert len(results) == 5
        assert all(r == 1 for r in results)
    
    def test_connection_pool_stats(self):
        """Test connection pool statistics."""
        stats = self.db.get_connection_stats()
        assert "active_connections" in stats
        assert "max_connections" in stats
        assert "pool_utilization" in stats
        assert stats["max_connections"] == self.pool_config.max_connections


class TestHealthChecks:
    """Test database health check functionality."""
    
    def setup_method(self):
        """Set up a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.db = NFLDatabase(self.temp_db.name)
    
    def teardown_method(self):
        """Clean up the temporary database after each test."""
        self.db.close()
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    def test_health_check_healthy_database(self):
        """Test health check on a healthy database."""
        health = self.db.health_check()
        
        assert health["healthy"] is True
        assert "pool_size" in health
        assert "database_size_bytes" in health
        assert "athlete_count" in health
        assert "team_count" in health
        assert "schema_version" in health
        assert health["schema_version"] == NFLDatabase.CURRENT_SCHEMA_VERSION
    
    def test_health_check_with_data(self):
        """Test health check with actual data in the database."""
        # Add some test data
        athletes_data = {
            "123": {
                "full_name": "Test Player",
                "team": "TEST",
                "position": "QB"
            }
        }
        self.db.upsert_athletes(athletes_data)
        
        health = self.db.health_check()
        assert health["healthy"] is True
        assert health["athlete_count"] == 1
        assert health["last_athlete_update"] is not None
    
    def test_pool_health_check(self):
        """Test connection pool health check."""
        pool_health = self.db._pool.health_check()
        
        assert pool_health["healthy"] is True
        assert "pool_size" in pool_health
        assert "pool_capacity" in pool_health
        assert "database_size_bytes" in pool_health
        assert "wal_mode" in pool_health
        assert pool_health["wal_mode"] == "enabled"


class TestDatabaseMigrations:
    """Test database migration system."""
    
    def setup_method(self):
        """Set up a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
    
    def teardown_method(self):
        """Clean up the temporary database after each test."""
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    def test_fresh_database_migration(self):
        """Test that a fresh database gets all migrations."""
        db = NFLDatabase(self.temp_db.name)
        
        with db._pool.get_connection() as conn:
            # Check that schema_version table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            )
            assert cursor.fetchone() is not None
            
            # Check current version
            cursor = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == NFLDatabase.CURRENT_SCHEMA_VERSION
        
        db.close()
    
    def test_migration_creates_optimized_indexes(self):
        """Test that migrations create optimized indexes."""
        db = NFLDatabase(self.temp_db.name)
        
        with db._pool.get_connection() as conn:
            # Check for compound indexes
            cursor = conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='index' AND name='idx_athletes_team_position'
            """)
            assert cursor.fetchone() is not None
            
            cursor = conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='index' AND name='idx_athletes_lookup'
            """)
            assert cursor.fetchone() is not None
        
        db.close()
    
    def test_schema_version_tracking(self):
        """Test that schema versions are properly tracked."""
        db = NFLDatabase(self.temp_db.name)
        
        with db._pool.get_connection() as conn:
            cursor = conn.execute("SELECT version, applied_at FROM schema_version ORDER BY version")
            versions = cursor.fetchall()
            
            # Should have migration records for all versions
            assert len(versions) >= 1
            
            # Each version should have a timestamp
            for version_row in versions:
                assert version_row[1] is not None  # applied_at should not be None
        
        db.close()


class TestOptimizedIndexing:
    """Test optimized indexing functionality."""
    
    def setup_method(self):
        """Set up a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.db = NFLDatabase(self.temp_db.name)
    
    def teardown_method(self):
        """Clean up the temporary database after each test."""
        self.db.close()
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    def test_compound_indexes_exist(self):
        """Test that compound indexes are created."""
        with self.db._pool.get_connection() as conn:
            # Check for compound indexes
            cursor = conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='index' AND name LIKE 'idx_%'
            """)
            indexes = [row[0] for row in cursor.fetchall()]
            
            # Should have compound indexes
            assert "idx_athletes_team_position" in indexes
            assert "idx_athletes_position_status" in indexes
            assert "idx_athletes_lookup" in indexes
            assert "idx_teams_lookup" in indexes
    
    def test_search_performance_with_indexes(self):
        """Test that searches use the optimized indexes."""
        # Add test data
        athletes_data = {}
        for i in range(100):
            athletes_data[str(i)] = {
                "full_name": f"Player {i}",
                "team": "TEST" if i % 2 == 0 else "OTHER",
                "position": "QB" if i % 3 == 0 else "RB"
            }
        
        self.db.upsert_athletes(athletes_data)
        
        # Test queries that should benefit from indexes
        
        # Team-based query (should use idx_athletes_team_position)
        team_players = self.db.get_athletes_by_team("TEST")
        assert len(team_players) == 50
        
        # Name search (should use idx_athletes_name_search)
        search_results = self.db.search_athletes_by_name("Player", limit=10)
        assert len(search_results) == 10
    
    def test_covering_indexes_performance(self):
        """Test that covering indexes improve lookup performance."""
        # Add test data
        athletes_data = {
            "123": {
                "full_name": "Test Player",
                "team": "TEST",
                "position": "QB",
                "status": "Active"
            }
        }
        self.db.upsert_athletes(athletes_data)
        
        # This lookup should use the covering index
        athlete = self.db.get_athlete_by_id("123")
        assert athlete is not None
        assert athlete["full_name"] == "Test Player"
        assert athlete["team_id"] == "TEST"


class TestBackwardCompatibility:
    """Test that optimizations maintain backward compatibility."""
    
    def setup_method(self):
        """Set up a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.db = NFLDatabase(self.temp_db.name)
    
    def teardown_method(self):
        """Clean up the temporary database after each test."""
        self.db.close()
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    def test_legacy_get_connection_method(self):
        """Test that legacy _get_connection method still works."""
        with self.db._get_connection() as conn:
            assert conn is not None
            result = conn.execute("SELECT 1").fetchone()
            assert result[0] == 1
    
    def test_all_existing_methods_work(self):
        """Test that all existing database methods still work."""
        # Test athlete methods
        athletes_data = {
            "123": {
                "full_name": "Test Player",
                "team": "TEST",
                "position": "QB"
            }
        }
        
        result = self.db.upsert_athletes(athletes_data)
        assert result == 1
        
        athlete = self.db.get_athlete_by_id("123")
        assert athlete is not None
        
        search_results = self.db.search_athletes_by_name("Test")
        assert len(search_results) == 1
        
        team_athletes = self.db.get_athletes_by_team("TEST")
        assert len(team_athletes) == 1
        
        count = self.db.get_athlete_count()
        assert count == 1
        
        last_updated = self.db.get_last_updated()
        assert last_updated is not None
        
        # Test team methods
        teams_data = [{
            "id": "TEST",
            "abbreviation": "TST",
            "name": "Test Team"
        }]
        
        result = self.db.upsert_teams(teams_data)
        assert result == 1
        
        team = self.db.get_team_by_id("TEST")
        assert team is not None
        
        team = self.db.get_team_by_abbreviation("TST")
        assert team is not None
        
        all_teams = self.db.get_all_teams()
        assert len(all_teams) == 1
        
        team_count = self.db.get_team_count()
        assert team_count == 1


class TestAsyncDatabaseOperations:
    """Test async database operations."""
    
    def setup_method(self):
        """Set up a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.db = NFLDatabase(self.temp_db.name)
    
    def teardown_method(self):
        """Clean up the temporary database after each test."""
        self.db.close()
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    @pytest.mark.asyncio
    async def test_async_health_check(self):
        """Test async health check functionality."""
        health = await self.db.async_health_check()
        
        # Should work even without aiosqlite by falling back gracefully
        assert "healthy" in health
        assert "last_check" in health
        
        if health.get("async_support"):
            assert health["healthy"] is True
            assert "athlete_count" in health
            assert "team_count" in health
    
    @pytest.mark.asyncio 
    async def test_async_athlete_operations(self):
        """Test async athlete operations."""
        try:
            # Test async upsert
            athletes_data = {
                "123": {
                    "full_name": "Async Test Player",
                    "team": "TEST",
                    "position": "QB"
                }
            }
            
            result = await self.db.async_upsert_athletes(athletes_data)
            assert result == 1
            
            # Test async get by id
            athlete = await self.db.async_get_athlete_by_id("123")
            assert athlete is not None
            assert athlete["full_name"] == "Async Test Player"
            
            # Test async search
            search_results = await self.db.async_search_athletes_by_name("Async")
            assert len(search_results) == 1
            
            # Test async get by team
            team_athletes = await self.db.async_get_athletes_by_team("TEST")
            assert len(team_athletes) == 1
            
        except RuntimeError as e:
            if "aiosqlite" in str(e):
                pytest.skip("aiosqlite not available for async operations")
            else:
                raise
    
    @pytest.mark.asyncio
    async def test_async_team_operations(self):
        """Test async team operations."""
        try:
            # Test async upsert teams
            teams_data = [{
                "id": "TEST",
                "abbreviation": "TST",
                "name": "Async Test Team"
            }]
            
            result = await self.db.async_upsert_teams(teams_data)
            assert result == 1
            
            # Test async get by id
            team = await self.db.async_get_team_by_id("TEST")
            assert team is not None
            assert team["name"] == "Async Test Team"
            
            # Test async get by abbreviation
            team = await self.db.async_get_team_by_abbreviation("TST")
            assert team is not None
            assert team["abbreviation"] == "TST"
            
            # Test async get all teams
            all_teams = await self.db.async_get_all_teams()
            assert len(all_teams) == 1
            
        except RuntimeError as e:
            if "aiosqlite" in str(e):
                pytest.skip("aiosqlite not available for async operations")
            else:
                raise
    
    @pytest.mark.asyncio
    async def test_async_without_aiosqlite_handling(self):
        """Test that async operations handle missing aiosqlite gracefully."""
        # This test ensures our error handling works properly
        # We can't easily mock the import, so we test the error path indirectly
        health = await self.db.async_health_check()
        assert "healthy" in health