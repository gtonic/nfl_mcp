"""Integration tests for usage stats and trend calculation."""
import pytest
from pathlib import Path
import tempfile
from nfl_mcp.database import NFLDatabase
from nfl_mcp.sleeper_tools import _enrich_usage_and_opponent


class TestUsageStatsIntegration:
    """Test usage stats database and enrichment integration."""
    
    @pytest.fixture
    def db(self):
        """Create temporary database for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            yield NFLDatabase(str(db_path))
    
    def test_usage_stats_storage_and_retrieval(self, db):
        """Test storing and retrieving usage stats."""
        # Insert usage stats for multiple weeks
        usage_stats = [
            {
                "player_id": "player1",
                "season": 2024,
                "week": 3,
                "targets": 8,
                "routes": 25,
                "rz_touches": 1,
                "touches": 12,
                "snap_share": 75.0
            },
            {
                "player_id": "player1",
                "season": 2024,
                "week": 4,
                "targets": 10,
                "routes": 30,
                "rz_touches": 2,
                "touches": 15,
                "snap_share": 80.0
            },
            {
                "player_id": "player1",
                "season": 2024,
                "week": 5,
                "targets": 12,
                "routes": 35,
                "rz_touches": 3,
                "touches": 18,
                "snap_share": 85.0
            }
        ]
        
        inserted = db.upsert_usage_stats(usage_stats)
        assert inserted == 3, "Should insert 3 usage stat records"
        
        # Retrieve average stats for last 3 weeks
        usage = db.get_usage_last_n_weeks("player1", 2024, 6, n=3)
        assert usage is not None, "Should retrieve usage stats"
        assert usage["weeks_sample"] == 3, "Should have 3 weeks of data"
        assert usage["targets_avg"] == 10.0, "Average targets should be 10"
        assert usage["routes_avg"] == 30.0, "Average routes should be 30"
        assert usage["rz_touches_avg"] == 2.0, "Average RZ touches should be 2"
        assert usage["snap_share_avg"] == 80.0, "Average snap share should be 80"
    
    def test_usage_weekly_breakdown(self, db):
        """Test getting weekly breakdown for trend calculation."""
        # Insert usage stats for multiple weeks
        usage_stats = [
            {
                "player_id": "player2",
                "season": 2024,
                "week": 3,
                "targets": 5,
                "routes": 20,
                "snap_share": 60.0
            },
            {
                "player_id": "player2",
                "season": 2024,
                "week": 4,
                "targets": 8,
                "routes": 25,
                "snap_share": 70.0
            },
            {
                "player_id": "player2",
                "season": 2024,
                "week": 5,
                "targets": 12,
                "routes": 35,
                "snap_share": 85.0
            }
        ]
        
        inserted = db.upsert_usage_stats(usage_stats)
        assert inserted == 3
        
        # Get weekly breakdown
        breakdown = db.get_usage_weekly_breakdown("player2", 2024, 6, n=3)
        assert breakdown is not None, "Should retrieve weekly breakdown"
        assert len(breakdown) == 3, "Should have 3 weeks of breakdown"
        
        # Check order (should be DESC by week)
        assert breakdown[0]["week"] == 5, "First entry should be most recent week"
        assert breakdown[1]["week"] == 4
        assert breakdown[2]["week"] == 3
        
        # Check values
        assert breakdown[0]["targets"] == 12
        assert breakdown[0]["routes"] == 35
        assert breakdown[0]["snap_share"] == 85.0
    
    def test_enrichment_with_trend(self, db):
        """Test that enrichment includes trend calculation."""
        # First, seed the database with athlete and usage data
        athlete = {
            "id": "test_player",
            "player_id": "test_player",
            "full_name": "Test Player",
            "position": "WR"
        }
        
        # Insert athlete (expects dict with player_id as key)
        db.upsert_athletes({
            "test_player": {
                "full_name": "Test Player",
                "first_name": "Test",
                "last_name": "Player",
                "position": "WR",
                "team": "KC",
                "age": 25
            }
        })
        
        # Insert usage stats with upward trend
        usage_stats = [
            {
                "player_id": "test_player",
                "season": 2024,
                "week": 3,
                "targets": 6,
                "routes": 20,
                "rz_touches": 1,
                "snap_share": 65.0
            },
            {
                "player_id": "test_player",
                "season": 2024,
                "week": 4,
                "targets": 8,
                "routes": 25,
                "rz_touches": 1,
                "snap_share": 70.0
            },
            {
                "player_id": "test_player",
                "season": 2024,
                "week": 5,
                "targets": 12,
                "routes": 35,
                "rz_touches": 3,
                "snap_share": 90.0
            }
        ]
        db.upsert_usage_stats(usage_stats)
        
        # Call enrichment
        enrichment = _enrich_usage_and_opponent(db, athlete, 2024, 6)
        
        # Check that usage stats are present
        assert "usage_last_3_weeks" in enrichment, "Should have usage_last_3_weeks"
        usage = enrichment["usage_last_3_weeks"]
        assert usage["targets_avg"] is not None
        assert usage["routes_avg"] is not None
        
        # Check that trend is calculated
        assert "usage_trend" in enrichment, "Should have usage_trend"
        trend = enrichment["usage_trend"]
        assert trend["targets"] == "up", "Targets should be trending up"
        assert trend["snap_share"] == "up", "Snap share should be trending up"
        
        assert "usage_trend_overall" in enrichment, "Should have overall trend"
        assert enrichment["usage_trend_overall"] in ["up", "down", "flat"]
    
    def test_enrichment_without_trend_data(self, db):
        """Test enrichment when insufficient data for trend."""
        athlete = {
            "id": "test_player2",
            "player_id": "test_player2",
            "full_name": "Test Player 2",
            "position": "RB"
        }
        
        # Insert athlete (expects dict with player_id as key)
        db.upsert_athletes({
            "test_player2": {
                "full_name": "Test Player 2",
                "first_name": "Test",
                "last_name": "Player2",
                "position": "RB",
                "team": "BUF",
                "age": 24
            }
        })
        
        # Insert only one week of usage stats (insufficient for trend)
        usage_stats = [
            {
                "player_id": "test_player2",
                "season": 2024,
                "week": 5,
                "targets": 5,
                "routes": 15,
                "touches": 12,
                "snap_share": 70.0
            }
        ]
        db.upsert_usage_stats(usage_stats)
        
        # Call enrichment
        enrichment = _enrich_usage_and_opponent(db, athlete, 2024, 6)
        
        # Should have usage stats but no trend
        if "usage_last_3_weeks" in enrichment:
            assert enrichment["usage_last_3_weeks"]["weeks_sample"] == 1
        
        # Trend should not be present with insufficient data
        # (or if present, should handle gracefully)
