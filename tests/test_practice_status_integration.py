"""Integration test to demonstrate practice status fix."""

import pytest
from unittest.mock import Mock
from datetime import datetime, UTC
from nfl_mcp.sleeper_tools import _enrich_usage_and_opponent


def test_practice_status_integration_scenario():
    """
    Integration test demonstrating the fix for the issue where practice_status
    was showing as 'Unklar (fehlend: practice_status)' for healthy players.
    
    This test verifies that all players get a practice_status, whether they:
    - Have an explicit practice report (from database)
    - Have an injury (derive status from injury)
    - Are healthy (default to FP)
    """
    
    # Setup mock database
    mock_db = Mock()
    
    # Scenario 1: Christian McCaffrey - healthy player (no injury, no practice report)
    mock_db.get_latest_practice_status = Mock(return_value=None)
    mock_db.get_player_injury_from_cache = Mock(return_value=None)
    mock_db.get_usage_last_n_weeks = Mock(return_value=None)
    
    mccaffrey = {
        "id": "4035",
        "full_name": "Christian McCaffrey",
        "position": "RB"
    }
    
    result1 = _enrich_usage_and_opponent(mock_db, mccaffrey, 2025, 6)
    
    # Verify healthy player gets FP status
    assert "practice_status" in result1, "Healthy player should have practice_status"
    assert result1["practice_status"] == "FP", "Healthy player should have FP status"
    assert result1["practice_status_source"] == "default_healthy"
    print(f"✓ {mccaffrey['full_name']}: practice_status={result1['practice_status']} (source: {result1['practice_status_source']})")
    
    # Scenario 2: Injured player - Questionable
    mock_db.get_player_injury_from_cache = Mock(return_value={
        "injury_status": "Questionable",
        "injury_type": "Ankle",
        "updated_at": datetime.now(UTC).isoformat()
    })
    
    kamara = {
        "id": "4098",
        "full_name": "Alvin Kamara",
        "position": "RB"
    }
    
    result2 = _enrich_usage_and_opponent(mock_db, kamara, 2025, 6)
    
    # Verify injured player gets derived status
    assert "practice_status" in result2, "Injured player should have practice_status"
    assert result2["practice_status"] == "LP", "Questionable player should have LP status"
    assert result2["practice_status_source"] == "derived_from_injury"
    assert result2["injury_status"] == "Questionable"
    print(f"✓ {kamara['full_name']}: practice_status={result2['practice_status']} (source: {result2['practice_status_source']}, injury: {result2['injury_status']})")
    
    # Scenario 3: Player with explicit practice report
    mock_db.get_latest_practice_status = Mock(return_value={
        "status": "LP",
        "date": "2025-01-15",
        "updated_at": datetime.now(UTC).isoformat(),
        "source": "espn_injuries"
    })
    mock_db.get_player_injury_from_cache = Mock(return_value=None)
    
    ferguson = {
        "id": "8112",
        "full_name": "Jake Ferguson",
        "position": "TE"
    }
    
    result3 = _enrich_usage_and_opponent(mock_db, ferguson, 2025, 6)
    
    # Verify explicit practice report is used
    assert "practice_status" in result3, "Player with report should have practice_status"
    assert result3["practice_status"] == "LP", "Should use explicit practice report"
    assert "practice_status_date" in result3
    assert result3["practice_status_source"] == "cached", "Cached status should have source field"
    print(f"✓ {ferguson['full_name']}: practice_status={result3['practice_status']} (from database, date: {result3['practice_status_date']})")
    
    # Scenario 4: Player Out with injury
    mock_db.get_latest_practice_status = Mock(return_value=None)
    mock_db.get_player_injury_from_cache = Mock(return_value={
        "injury_status": "Out",
        "injury_type": "Knee",
        "updated_at": datetime.now(UTC).isoformat()
    })
    
    injured_player = {
        "id": "9999",
        "full_name": "Injured Player",
        "position": "WR"
    }
    
    result4 = _enrich_usage_and_opponent(mock_db, injured_player, 2025, 6)
    
    # Verify Out player gets DNP
    assert "practice_status" in result4, "Out player should have practice_status"
    assert result4["practice_status"] == "DNP", "Out player should have DNP status"
    assert result4["practice_status_source"] == "derived_from_injury"
    assert result4["injury_status"] == "Out"
    print(f"✓ {injured_player['full_name']}: practice_status={result4['practice_status']} (source: {result4['practice_status_source']}, injury: {result4['injury_status']})")
    
    print("\n✅ All scenarios pass - practice_status is now always provided!")
    print("   - Healthy players: Default to FP")
    print("   - Injured players: Derived from injury status")
    print("   - Players with reports: Use explicit data")


if __name__ == "__main__":
    test_practice_status_integration_scenario()
