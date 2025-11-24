"""
Tests for enrichment helper functions in sleeper_tools.py.

Tests cover:
- _calculate_usage_trend: Usage metric trend calculation
- _estimate_snap_pct: Snap percentage estimation from depth chart
- Enrichment field generation
"""

import pytest
from unittest.mock import MagicMock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCalculateUsageTrend:
    """Tests for _calculate_usage_trend function."""
    
    def test_upward_trend_targets(self):
        """Test upward trend detection for targets."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        # Most recent week has significantly more targets
        weekly_data = [
            {"targets": 12},  # Most recent
            {"targets": 7},
            {"targets": 6},
        ]
        result = _calculate_usage_trend(weekly_data, "targets")
        assert result == "up", f"Expected 'up' but got '{result}'"
    
    def test_downward_trend_targets(self):
        """Test downward trend detection for targets."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        # Most recent week has significantly fewer targets
        weekly_data = [
            {"targets": 3},  # Most recent
            {"targets": 8},
            {"targets": 9},
        ]
        result = _calculate_usage_trend(weekly_data, "targets")
        assert result == "down", f"Expected 'down' but got '{result}'"
    
    def test_flat_trend_targets(self):
        """Test flat trend detection for targets."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        # Most recent week is similar to prior weeks
        weekly_data = [
            {"targets": 7},  # Most recent
            {"targets": 7},
            {"targets": 8},
        ]
        result = _calculate_usage_trend(weekly_data, "targets")
        assert result == "flat", f"Expected 'flat' but got '{result}'"
    
    def test_insufficient_data_single_week(self):
        """Test None return when only one week of data."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        weekly_data = [{"targets": 10}]
        result = _calculate_usage_trend(weekly_data, "targets")
        assert result is None, f"Expected None but got '{result}'"
    
    def test_insufficient_data_empty(self):
        """Test None return when no data."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        result = _calculate_usage_trend([], "targets")
        assert result is None, f"Expected None but got '{result}'"
    
    def test_none_values_filtered(self):
        """Test that None values in weekly data are filtered out."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        weekly_data = [
            {"targets": 10},  # Most recent
            {"targets": None},  # Should be ignored
            {"targets": 6},
        ]
        result = _calculate_usage_trend(weekly_data, "targets")
        # 10 vs avg(6) = 10 vs 6 = 66% increase -> up
        assert result == "up", f"Expected 'up' but got '{result}'"
    
    def test_routes_trend(self):
        """Test trend calculation for routes metric."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        weekly_data = [
            {"routes": 35},  # Most recent
            {"routes": 25},
            {"routes": 22},
        ]
        result = _calculate_usage_trend(weekly_data, "routes")
        # 35 vs avg(25, 22) = 35 vs 23.5 = 48% increase -> up
        assert result == "up", f"Expected 'up' but got '{result}'"
    
    def test_snap_share_trend(self):
        """Test trend calculation for snap_share metric."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        weekly_data = [
            {"snap_share": 45.0},  # Most recent
            {"snap_share": 65.0},
            {"snap_share": 70.0},
        ]
        result = _calculate_usage_trend(weekly_data, "snap_share")
        # 45 vs avg(65, 70) = 45 vs 67.5 = -33% -> down
        assert result == "down", f"Expected 'down' but got '{result}'"
    
    def test_zero_prior_average_with_positive_recent(self):
        """Test edge case where prior average is 0 but recent is positive."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        weekly_data = [
            {"targets": 5},  # Most recent (positive)
            {"targets": 0},
            {"targets": 0},
        ]
        result = _calculate_usage_trend(weekly_data, "targets")
        assert result == "up", f"Expected 'up' for positive value from 0 average, got '{result}'"
    
    def test_zero_prior_average_with_zero_recent(self):
        """Test edge case where both prior and recent are 0."""
        from nfl_mcp.sleeper_tools import _calculate_usage_trend
        
        weekly_data = [
            {"targets": 0},
            {"targets": 0},
            {"targets": 0},
        ]
        result = _calculate_usage_trend(weekly_data, "targets")
        assert result == "flat", f"Expected 'flat' for all zeros, got '{result}'"


class TestEstimateSnapPct:
    """Tests for _estimate_snap_pct function."""
    
    def test_qb_starter(self):
        """Test QB starter gets ~95% snap estimate."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(1, "QB")
        assert result == 95.0, f"Expected 95.0 for QB starter, got {result}"
    
    def test_qb_backup(self):
        """Test QB backup gets ~5% snap estimate."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(2, "QB")
        assert result == 5.0, f"Expected 5.0 for QB backup, got {result}"
    
    def test_rb_starter(self):
        """Test RB starter gets ~55% snap estimate (committee consideration)."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(1, "RB")
        assert result == 55.0, f"Expected 55.0 for RB starter, got {result}"
    
    def test_rb_backup(self):
        """Test RB backup gets ~35% snap estimate."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(2, "RB")
        assert result == 35.0, f"Expected 35.0 for RB backup, got {result}"
    
    def test_wr_starter(self):
        """Test WR starter gets ~85% snap estimate."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(1, "WR")
        assert result == 85.0, f"Expected 85.0 for WR starter, got {result}"
    
    def test_wr_backup(self):
        """Test WR backup (#2 receiver) gets ~50% snap estimate."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(2, "WR")
        assert result == 50.0, f"Expected 50.0 for WR backup, got {result}"
    
    def test_te_starter(self):
        """Test TE starter gets ~65% snap estimate."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(1, "TE")
        assert result == 65.0, f"Expected 65.0 for TE starter, got {result}"
    
    def test_third_string_low_snaps(self):
        """Test third string or lower gets ~15% snap estimate."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(3, "RB")
        assert result == 15.0, f"Expected 15.0 for third string, got {result}"
        
        result = _estimate_snap_pct(4, "WR")
        assert result == 15.0, f"Expected 15.0 for fourth string, got {result}"
    
    def test_unknown_position_starter(self):
        """Test unknown position starter gets default 70%."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(1, "K")  # Kicker
        assert result == 70.0, f"Expected 70.0 for unknown position starter, got {result}"
    
    def test_unknown_position_backup(self):
        """Test unknown position backup gets default 45%."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(2, "K")
        assert result == 45.0, f"Expected 45.0 for unknown position backup, got {result}"
    
    def test_none_depth_rank(self):
        """Test None depth rank returns None."""
        from nfl_mcp.sleeper_tools import _estimate_snap_pct
        
        result = _estimate_snap_pct(None, "WR")
        assert result is None, f"Expected None for None depth rank, got {result}"


class TestOutboundRateLimiter:
    """Tests for OutboundRateLimiter class."""
    
    def test_initial_tokens_available(self):
        """Test that initial tokens are available."""
        from nfl_mcp.config import OutboundRateLimiter
        
        limiter = OutboundRateLimiter(calls_per_minute=60)
        status = limiter.get_status()
        
        assert status["capacity"] == 60
        assert status["available_tokens"] >= 59  # Allow for small time drift
    
    def test_try_acquire_success(self):
        """Test successful token acquisition."""
        from nfl_mcp.config import OutboundRateLimiter
        
        limiter = OutboundRateLimiter(calls_per_minute=60)
        result = limiter.try_acquire(1)
        
        assert result is True, "Should successfully acquire token"
    
    def test_try_acquire_exhausted(self):
        """Test token acquisition when exhausted."""
        from nfl_mcp.config import OutboundRateLimiter
        
        limiter = OutboundRateLimiter(calls_per_minute=5, burst_capacity=5)
        
        # Exhaust all tokens
        for _ in range(5):
            limiter.try_acquire(1)
        
        # Should fail to acquire
        result = limiter.try_acquire(1)
        assert result is False, "Should fail when tokens exhausted"
    
    @pytest.mark.asyncio
    async def test_acquire_waits_for_tokens(self):
        """Test that acquire waits for tokens to replenish."""
        from nfl_mcp.config import OutboundRateLimiter
        import asyncio
        
        limiter = OutboundRateLimiter(calls_per_minute=60, burst_capacity=2)
        
        # Exhaust tokens
        limiter.try_acquire(2)
        
        # Acquire should wait and return
        wait_time = await asyncio.wait_for(limiter.acquire(1), timeout=2.0)
        
        # Should have waited some time
        assert wait_time > 0, "Should have waited for token replenishment"
    
    def test_status_report(self):
        """Test status report contains expected fields."""
        from nfl_mcp.config import OutboundRateLimiter
        
        limiter = OutboundRateLimiter(calls_per_minute=100)
        status = limiter.get_status()
        
        assert "available_tokens" in status
        assert "capacity" in status
        assert "rate_per_second" in status
        assert "rate_per_minute" in status
        assert status["rate_per_minute"] == 100.0


class TestCircuitBreakerStatus:
    """Tests for circuit breaker status reporting."""
    
    def test_get_all_circuit_breaker_status_empty(self):
        """Test status when no breakers exist."""
        from nfl_mcp.retry_utils import get_all_circuit_breaker_status, _circuit_breakers
        
        # Clear existing breakers
        _circuit_breakers.clear()
        
        status = get_all_circuit_breaker_status()
        assert status == {}, "Should return empty dict when no breakers"
    
    def test_get_all_circuit_breaker_status_with_breakers(self):
        """Test status with active breakers."""
        from nfl_mcp.retry_utils import get_circuit_breaker, get_all_circuit_breaker_status
        
        # Create some breakers
        get_circuit_breaker("test_api_1")
        get_circuit_breaker("test_api_2")
        
        status = get_all_circuit_breaker_status()
        
        assert "test_api_1" in status
        assert "test_api_2" in status
        assert status["test_api_1"]["state"] == "closed"
        assert status["test_api_1"]["failure_count"] == 0


class TestBatchDatabaseQueries:
    """Tests for batch database query methods."""
    
    def test_get_athletes_by_ids_empty(self):
        """Test batch query with empty list."""
        from nfl_mcp.database import NFLDatabase
        import tempfile
        
        with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
            db = NFLDatabase(tmp.name)
            result = db.get_athletes_by_ids([])
        
        assert result == {}, "Should return empty dict for empty input"
    
    def test_get_athletes_by_ids_not_found(self):
        """Test batch query when athletes don't exist."""
        from nfl_mcp.database import NFLDatabase
        import tempfile
        
        with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
            db = NFLDatabase(tmp.name)
            result = db.get_athletes_by_ids(["nonexistent1", "nonexistent2"])
        
            assert result == {}, "Should return empty dict when no athletes found"
    
    def test_get_athletes_by_ids_found(self):
        """Test batch query finds existing athletes."""
        from nfl_mcp.database import NFLDatabase
        import tempfile
        
        with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
            db = NFLDatabase(tmp.name)
            
            # Insert test athletes as a dict (the format upsert_athletes expects)
            test_athletes = {
                "test1": {"id": "test1", "full_name": "Player One", "position": "WR", "team_id": "KC"},
                "test2": {"id": "test2", "full_name": "Player Two", "position": "RB", "team_id": "SF"},
            }
            db.upsert_athletes(test_athletes)
            
            # Query them back
            result = db.get_athletes_by_ids(["test1", "test2", "test3"])
            
            assert len(result) == 2, f"Should find 2 athletes, found {len(result)}"
            assert "test1" in result
            assert "test2" in result
            assert "test3" not in result  # Doesn't exist
            assert result["test1"]["full_name"] == "Player One"
            assert result["test2"]["position"] == "RB"


class TestSnapshotCleanup:
    """Tests for snapshot cleanup functionality."""
    
    def test_cleanup_old_snapshots_empty_db(self):
        """Test cleanup on empty database."""
        from nfl_mcp.database import NFLDatabase
        import tempfile
        
        with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
            db = NFLDatabase(tmp.name)
            result = db.cleanup_old_snapshots(max_age_days=7)
            
            assert result["roster_snapshots"] == 0
            assert result["matchup_snapshots"] == 0
            assert result["transaction_snapshots"] == 0
    
    def test_cleanup_old_snapshots_with_data(self):
        """Test cleanup removes old snapshots."""
        from nfl_mcp.database import NFLDatabase
        from datetime import datetime, timedelta
        import tempfile
        
        with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
            db = NFLDatabase(tmp.name)
            
            # Insert old snapshots (8 days ago)
            old_time = (datetime.utcnow() - timedelta(days=8)).isoformat()
            new_time = datetime.utcnow().isoformat()
            
            with db._pool.get_connection() as conn:
                # Insert old roster snapshot
                conn.execute(
                    "INSERT INTO roster_snapshots (league_id, payload_json, fetched_at) VALUES (?, ?, ?)",
                    ("old_league", "[]", old_time)
                )
                # Insert new roster snapshot
                conn.execute(
                    "INSERT INTO roster_snapshots (league_id, payload_json, fetched_at) VALUES (?, ?, ?)",
                    ("new_league", "[]", new_time)
                )
                conn.commit()
            
            # Cleanup
            result = db.cleanup_old_snapshots(max_age_days=7)
            
            # Should have deleted the old one
            assert result["roster_snapshots"] == 1, f"Expected 1 deleted, got {result['roster_snapshots']}"
            
            # Verify new one still exists
            with db._pool.get_connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM roster_snapshots WHERE league_id = 'new_league'")
                count = cursor.fetchone()[0]
                assert count == 1, "New snapshot should still exist"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
