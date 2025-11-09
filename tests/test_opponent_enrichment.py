"""Test opponent enrichment for offensive and defensive players."""
import pytest
from nfl_mcp.database import NFLDatabase
from nfl_mcp.sleeper_tools import _enrich_usage_and_opponent


class TestOpponentEnrichment:
    """Test that opponent data is correctly enriched for all positions."""
    
    def test_offensive_player_gets_opponent_from_team_id(self, tmp_path):
        """Test that offensive players get opponent data using team_id field."""
        # Create a test database
        db_path = tmp_path / "test_nfl.db"
        nfl_db = NFLDatabase(db_path=str(db_path))
        
        # Insert schedule data
        schedule_games = [
            {
                "season": 2024,
                "week": 10,
                "team": "CHI",
                "opponent": "NE",
                "is_home": 1,
                "kickoff": "2024-11-10T13:00:00Z",
                "raw": {}
            },
            {
                "season": 2024,
                "week": 10,
                "team": "SF",
                "opponent": "TB",
                "is_home": 1,
                "kickoff": "2024-11-10T16:00:00Z",
                "raw": {}
            },
            {
                "season": 2024,
                "week": 10,
                "team": "CLE",
                "opponent": "NYJ",
                "is_home": 1,
                "kickoff": "2024-11-10T13:00:00Z",
                "raw": {}
            }
        ]
        nfl_db.upsert_schedule_games(schedule_games)
        
        # Test offensive player with team_id (QB)
        qb_athlete = {
            "id": "test_qb",
            "player_id": "test_qb",
            "full_name": "Caleb Williams",
            "name": "Caleb Williams",
            "position": "QB",
            "team": None,  # This should be None from database
            "team_id": "CHI",  # This should have the value
            "raw": {}
        }
        
        enriched_qb = _enrich_usage_and_opponent(nfl_db, qb_athlete, 2024, 10)
        
        assert "opponent" in enriched_qb, "QB should have opponent field"
        assert enriched_qb["opponent"] == "NE", "QB opponent should be NE"
        assert enriched_qb["opponent_source"] == "cached", "Opponent should be from cache"
        
        # Test offensive player (RB)
        rb_athlete = {
            "id": "test_rb",
            "player_id": "test_rb",
            "full_name": "Christian McCaffrey",
            "name": "Christian McCaffrey",
            "position": "RB",
            "team": None,
            "team_id": "SF",
            "raw": {}
        }
        
        enriched_rb = _enrich_usage_and_opponent(nfl_db, rb_athlete, 2024, 10)
        
        assert "opponent" in enriched_rb, "RB should have opponent field"
        assert enriched_rb["opponent"] == "TB", "RB opponent should be TB"
        
        # Test defensive player (DEF)
        def_athlete = {
            "id": "test_def",
            "player_id": "test_def",
            "full_name": "Cleveland Browns",
            "name": "Cleveland Browns",
            "position": "DEF",
            "team": None,
            "team_id": "CLE",
            "raw": {}
        }
        
        enriched_def = _enrich_usage_and_opponent(nfl_db, def_athlete, 2024, 10)
        
        assert "opponent" in enriched_def, "DEF should have opponent field"
        assert enriched_def["opponent"] == "NYJ", "DEF opponent should be NYJ"
    
    def test_player_without_team_id_has_no_opponent(self, tmp_path):
        """Test that players without team_id don't get opponent data."""
        db_path = tmp_path / "test_nfl.db"
        nfl_db = NFLDatabase(db_path=str(db_path))
        
        # Insert schedule data
        schedule_games = [
            {
                "season": 2024,
                "week": 10,
                "team": "CHI",
                "opponent": "NE",
                "is_home": 1,
                "kickoff": "2024-11-10T13:00:00Z",
                "raw": {}
            }
        ]
        nfl_db.upsert_schedule_games(schedule_games)
        
        # Test player without team_id
        athlete = {
            "id": "test_player",
            "player_id": "test_player",
            "full_name": "Test Player",
            "name": "Test Player",
            "position": "WR",
            "team": None,
            "team_id": None,  # No team_id
            "raw": {}
        }
        
        enriched = _enrich_usage_and_opponent(nfl_db, athlete, 2024, 10)
        
        assert "opponent" not in enriched, "Player without team_id should not have opponent"
    
    def test_player_with_missing_schedule_has_no_opponent(self, tmp_path):
        """Test that players get no opponent when schedule is not cached."""
        db_path = tmp_path / "test_nfl.db"
        nfl_db = NFLDatabase(db_path=str(db_path))
        
        # Don't insert any schedule data
        
        # Test player with team_id but no schedule
        athlete = {
            "id": "test_player",
            "player_id": "test_player",
            "full_name": "Test Player",
            "name": "Test Player",
            "position": "QB",
            "team": None,
            "team_id": "CHI",
            "raw": {}
        }
        
        enriched = _enrich_usage_and_opponent(nfl_db, athlete, 2024, 10)
        
        assert "opponent" not in enriched, "Player should not have opponent when schedule not cached"
