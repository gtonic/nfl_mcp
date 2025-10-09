"""Tests for practice status enrichment logic."""

import pytest
from unittest.mock import Mock, MagicMock
from datetime import datetime, UTC
from nfl_mcp.sleeper_tools import _enrich_usage_and_opponent


class TestPracticeStatusEnrichment:
    """Test practice status enrichment for various scenarios."""
    
    def test_explicit_practice_status_from_database(self):
        """Test that explicit practice status from database is used when available."""
        # Setup mock database
        mock_db = Mock()
        mock_db.get_latest_practice_status = Mock(return_value={
            "status": "LP",
            "date": "2025-01-15",
            "updated_at": datetime.now(UTC).isoformat(),
            "source": "espn_injuries"
        })
        mock_db.get_player_injury_from_cache = Mock(return_value=None)
        mock_db.get_usage_last_n_weeks = Mock(return_value=None)  # No usage data
        
        # Setup athlete data
        athlete = {
            "id": "12345",
            "full_name": "Test Player",
            "position": "WR"
        }
        
        # Call enrichment
        result = _enrich_usage_and_opponent(mock_db, athlete, 2025, 6)
        
        # Verify practice status is set from database
        assert result["practice_status"] == "LP"
        assert result["practice_status_date"] == "2025-01-15"
        assert "practice_status_age_hours" in result
    
    def test_practice_status_derived_from_injury_out(self):
        """Test that practice status is derived from OUT injury status."""
        # Setup mock database
        mock_db = Mock()
        mock_db.get_latest_practice_status = Mock(return_value=None)  # No explicit practice status
        mock_db.get_player_injury_from_cache = Mock(return_value={
            "injury_status": "Out",
            "injury_type": "Knee",
            "updated_at": datetime.now(UTC).isoformat()
        })
        mock_db.get_usage_last_n_weeks = Mock(return_value=None)  # No usage data
        
        # Setup athlete data
        athlete = {
            "id": "12345",
            "full_name": "Injured Player",
            "position": "RB"
        }
        
        # Call enrichment
        result = _enrich_usage_and_opponent(mock_db, athlete, 2025, 6)
        
        # Verify practice status is derived as DNP
        assert result["practice_status"] == "DNP"
        assert result["practice_status_source"] == "derived_from_injury"
        assert result["injury_status"] == "Out"
    
    def test_practice_status_derived_from_injury_questionable(self):
        """Test that practice status is derived from Questionable injury status."""
        # Setup mock database
        mock_db = Mock()
        mock_db.get_latest_practice_status = Mock(return_value=None)
        mock_db.get_player_injury_from_cache = Mock(return_value={
            "injury_status": "Questionable",
            "injury_type": "Ankle",
            "updated_at": datetime.now(UTC).isoformat()
        })
        mock_db.get_usage_last_n_weeks = Mock(return_value=None)  # No usage data
        
        # Setup athlete data
        athlete = {
            "id": "12345",
            "full_name": "Questionable Player",
            "position": "TE"
        }
        
        # Call enrichment
        result = _enrich_usage_and_opponent(mock_db, athlete, 2025, 6)
        
        # Verify practice status is derived as LP
        assert result["practice_status"] == "LP"
        assert result["practice_status_source"] == "derived_from_injury"
        assert result["injury_status"] == "Questionable"
    
    def test_practice_status_default_healthy(self):
        """Test that practice status defaults to FP for healthy players."""
        # Setup mock database
        mock_db = Mock()
        mock_db.get_latest_practice_status = Mock(return_value=None)  # No practice status
        mock_db.get_player_injury_from_cache = Mock(return_value=None)  # No injury
        mock_db.get_usage_last_n_weeks = Mock(return_value=None)  # No usage data
        
        # Setup athlete data
        athlete = {
            "id": "12345",
            "full_name": "Healthy Player",
            "position": "WR"
        }
        
        # Call enrichment
        result = _enrich_usage_and_opponent(mock_db, athlete, 2025, 6)
        
        # Verify practice status defaults to FP
        assert result["practice_status"] == "FP"
        assert result["practice_status_source"] == "default_healthy"
        assert "injury_status" not in result
    
    def test_practice_status_derived_from_injury_doubtful(self):
        """Test that practice status is derived from Doubtful injury status."""
        # Setup mock database
        mock_db = Mock()
        mock_db.get_latest_practice_status = Mock(return_value=None)
        mock_db.get_player_injury_from_cache = Mock(return_value={
            "injury_status": "Doubtful",
            "injury_type": "Hamstring",
            "updated_at": datetime.now(UTC).isoformat()
        })
        mock_db.get_usage_last_n_weeks = Mock(return_value=None)  # No usage data
        
        # Setup athlete data
        athlete = {
            "id": "12345",
            "full_name": "Doubtful Player",
            "position": "RB"
        }
        
        # Call enrichment
        result = _enrich_usage_and_opponent(mock_db, athlete, 2025, 6)
        
        # Verify practice status is derived as LP
        assert result["practice_status"] == "LP"
        assert result["practice_status_source"] == "derived_from_injury"
        assert result["injury_status"] == "Doubtful"
    
    def test_practice_status_derived_from_injury_ir(self):
        """Test that practice status is derived from IR injury status."""
        # Setup mock database
        mock_db = Mock()
        mock_db.get_latest_practice_status = Mock(return_value=None)
        mock_db.get_player_injury_from_cache = Mock(return_value={
            "injury_status": "Injured Reserve",
            "injury_type": "ACL",
            "updated_at": datetime.now(UTC).isoformat()
        })
        mock_db.get_usage_last_n_weeks = Mock(return_value=None)  # No usage data
        
        # Setup athlete data
        athlete = {
            "id": "12345",
            "full_name": "IR Player",
            "position": "WR"
        }
        
        # Call enrichment
        result = _enrich_usage_and_opponent(mock_db, athlete, 2025, 6)
        
        # Verify practice status is derived as DNP
        assert result["practice_status"] == "DNP"
        assert result["practice_status_source"] == "derived_from_injury"
        assert result["injury_status"] == "Injured Reserve"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
