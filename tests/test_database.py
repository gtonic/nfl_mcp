"""
Unit tests for the AthleteDatabase module.

Tests the SQLite database operations for athlete data management.
"""

import pytest
import tempfile
import json
from pathlib import Path
from nfl_mcp.database import NFLDatabase


class TestAthleteDatabase:
    """Test the AthleteDatabase class functionality."""
    
    def setup_method(self):
        """Set up a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.db = NFLDatabase(self.temp_db.name)
    
    def teardown_method(self):
        """Clean up the temporary database after each test."""
        Path(self.temp_db.name).unlink(missing_ok=True)
    
    def test_database_initialization(self):
        """Test that the database initializes correctly."""
        assert self.db.db_path.exists()
        
        # Test that tables are created
        with self.db._get_connection() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='athletes'"
            )
            assert cursor.fetchone() is not None
    
    def test_upsert_athletes_empty_data(self):
        """Test upsert with empty data."""
        result = self.db.upsert_athletes([])
        assert result == 0
        assert self.db.get_athlete_count() == 0
    
    def test_upsert_athletes_single_athlete(self):
        """Test upserting a single athlete."""
        athletes_data = {
            "123": {
                "full_name": "Tom Brady",
                "first_name": "Tom",
                "last_name": "Brady",
                "team": "TB",
                "position": "QB",
                "status": "Active"
            }
        }
        
        result = self.db.upsert_athletes(athletes_data)
        assert result == 1
        assert self.db.get_athlete_count() == 1
        
        # Verify the athlete was stored correctly
        athlete = self.db.get_athlete_by_id("123")
        assert athlete is not None
        assert athlete["full_name"] == "Tom Brady"
        assert athlete["team_id"] == "TB"
        assert athlete["position"] == "QB"
    
    def test_upsert_athletes_multiple(self):
        """Test upserting multiple athletes."""
        athletes_data = {
            "123": {
                "full_name": "Tom Brady",
                "first_name": "Tom",
                "last_name": "Brady",
                "team": "TB",
                "position": "QB",
                "status": "Active"
            },
            "456": {
                "full_name": "Patrick Mahomes",
                "first_name": "Patrick",
                "last_name": "Mahomes",
                "team": "KC", 
                "position": "QB",
                "status": "Active"
            }
        }
        
        result = self.db.upsert_athletes(athletes_data)
        assert result == 2
        assert self.db.get_athlete_count() == 2
    
    def test_upsert_athletes_update_existing(self):
        """Test that upsert updates existing records."""
        # Insert initial athlete
        athletes_data = {
            "123": {
                "full_name": "Tom Brady",
                "first_name": "Tom",
                "last_name": "Brady",
                "team": "TB",
                "position": "QB",
                "status": "Active"
            }
        }
        self.db.upsert_athletes(athletes_data)
        
        # Update the same athlete
        updated_data = {
            "123": {
                "full_name": "Tom Brady",
                "first_name": "Tom",
                "last_name": "Brady",
                "team": "NE",  # Changed team
                "position": "QB",
                "status": "Retired"  # Changed status
            }
        }
        result = self.db.upsert_athletes(updated_data)
        
        assert result == 1
        assert self.db.get_athlete_count() == 1  # Still only one record
        
        # Verify the update
        athlete = self.db.get_athlete_by_id("123")
        assert athlete["team_id"] == "NE"
        assert athlete["status"] == "Retired"
    
    def test_get_athlete_by_id_not_found(self):
        """Test getting athlete by ID when not found."""
        athlete = self.db.get_athlete_by_id("nonexistent")
        assert athlete is None
    
    def test_get_athlete_by_id_found(self):
        """Test getting athlete by ID when found."""
        athletes_data = {
            "123": {
                "full_name": "Tom Brady",
                "first_name": "Tom",
                "last_name": "Brady",
                "team": "TB",
                "position": "QB",
                "status": "Active"
            }
        }
        self.db.upsert_athletes(athletes_data)
        
        athlete = self.db.get_athlete_by_id("123")
        assert athlete is not None
        assert athlete["id"] == "123"
        assert athlete["full_name"] == "Tom Brady"
    
    def test_search_athletes_by_name_no_results(self):
        """Test searching for athletes with no matches."""
        results = self.db.search_athletes_by_name("nonexistent")
        assert results == []
    
    def test_search_athletes_by_name_full_match(self):
        """Test searching for athletes with full name match."""
        athletes_data = {
            "123": {
                "full_name": "Tom Brady",
                "first_name": "Tom",
                "last_name": "Brady",
                "team": "TB",
                "position": "QB",
                "status": "Active"
            }
        }
        self.db.upsert_athletes(athletes_data)
        
        results = self.db.search_athletes_by_name("Tom Brady")
        assert len(results) == 1
        assert results[0]["full_name"] == "Tom Brady"
    
    def test_search_athletes_by_name_partial_match(self):
        """Test searching for athletes with partial name match."""
        athletes_data = {
            "123": {
                "full_name": "Tom Brady",
                "first_name": "Tom",
                "last_name": "Brady",
                "team": "TB",
                "position": "QB",
                "status": "Active"
            },
            "456": {
                "full_name": "Tommy Thompson",
                "first_name": "Tommy",
                "last_name": "Thompson",
                "team": "SF",
                "position": "RB",
                "status": "Active"
            }
        }
        self.db.upsert_athletes(athletes_data)
        
        # Search for "Tom" should match both
        results = self.db.search_athletes_by_name("Tom")
        assert len(results) == 2
        
        # Search for "Brady" should match one
        results = self.db.search_athletes_by_name("Brady")
        assert len(results) == 1
        assert results[0]["full_name"] == "Tom Brady"
    
    def test_search_athletes_by_name_limit(self):
        """Test the limit parameter in name search."""
        # Create multiple athletes
        athletes_data = {}
        for i in range(5):
            athletes_data[str(i)] = {
                "full_name": f"Test Player {i}",
                "first_name": "Test",
                "last_name": f"Player{i}",
                "team": "TST",
                "position": "QB",
                "status": "Active"
            }
        self.db.upsert_athletes(athletes_data)
        
        # Search with limit
        results = self.db.search_athletes_by_name("Test", limit=3)
        assert len(results) == 3
    
    def test_get_athletes_by_team_no_results(self):
        """Test getting athletes by team with no matches."""
        results = self.db.get_athletes_by_team("NONEXISTENT")
        assert results == []
    
    def test_get_athletes_by_team_with_results(self):
        """Test getting athletes by team with matches."""
        athletes_data = {
            "123": {
                "full_name": "Tom Brady",
                "first_name": "Tom",
                "last_name": "Brady",
                "team": "TB",
                "position": "QB",
                "status": "Active"
            },
            "456": {
                "full_name": "Mike Evans",
                "first_name": "Mike",
                "last_name": "Evans",
                "team": "TB",
                "position": "WR",
                "status": "Active"
            },
            "789": {
                "full_name": "Patrick Mahomes",
                "first_name": "Patrick",
                "last_name": "Mahomes",
                "team": "KC",
                "position": "QB",
                "status": "Active"
            }
        }
        self.db.upsert_athletes(athletes_data)
        
        # Get TB team
        tb_athletes = self.db.get_athletes_by_team("TB")
        assert len(tb_athletes) == 2
        
        # Get KC team
        kc_athletes = self.db.get_athletes_by_team("KC")
        assert len(kc_athletes) == 1
        assert kc_athletes[0]["full_name"] == "Patrick Mahomes"
    
    def test_get_athlete_count_empty(self):
        """Test getting athlete count when database is empty."""
        count = self.db.get_athlete_count()
        assert count == 0
    
    def test_get_athlete_count_with_data(self):
        """Test getting athlete count with data."""
        athletes_data = {
            "123": {"full_name": "Test 1", "team": "TB"},
            "456": {"full_name": "Test 2", "team": "KC"}
        }
        self.db.upsert_athletes(athletes_data)
        
        count = self.db.get_athlete_count()
        assert count == 2
    
    def test_get_last_updated_empty(self):
        """Test getting last updated when database is empty."""
        last_updated = self.db.get_last_updated()
        assert last_updated is None
    
    def test_get_last_updated_with_data(self):
        """Test getting last updated with data."""
        athletes_data = {
            "123": {"full_name": "Test 1", "team": "TB"}
        }
        self.db.upsert_athletes(athletes_data)
        
        last_updated = self.db.get_last_updated()
        assert last_updated is not None
        assert "T" in last_updated  # ISO format contains 'T'
    
    def test_clear_athletes(self):
        """Test clearing all athletes."""
        athletes_data = {
            "123": {"full_name": "Test 1", "team": "TB"},
            "456": {"full_name": "Test 2", "team": "KC"}
        }
        self.db.upsert_athletes(athletes_data)
        assert self.db.get_athlete_count() == 2
        
        cleared = self.db.clear_athletes()
        assert cleared == 2
        assert self.db.get_athlete_count() == 0
    
    def test_athlete_raw_json_storage(self):
        """Test that raw JSON data is properly stored and retrieved."""
        athletes_data = {
            "123": {
                "full_name": "Tom Brady",
                "team": "TB",
                "position": "QB",
                "extra_field": "extra_value",
                "nested": {"key": "value"}
            }
        }
        self.db.upsert_athletes(athletes_data)
        
        athlete = self.db.get_athlete_by_id("123")
        assert athlete is not None
        
        # Parse the raw JSON
        raw_data = json.loads(athlete["raw"])
        assert raw_data["extra_field"] == "extra_value"
        assert raw_data["nested"]["key"] == "value"
    
    def test_athlete_missing_fields(self):
        """Test handling of athletes with missing fields."""
        athletes_data = {
            "123": {
                "full_name": "Incomplete Player"
                # Missing team, position, etc.
            }
        }
        self.db.upsert_athletes(athletes_data)
        
        athlete = self.db.get_athlete_by_id("123")
        assert athlete is not None
        assert athlete["full_name"] == "Incomplete Player"
        assert athlete["team_id"] == ""  # Should be empty string, not None
        assert athlete["position"] == ""
        assert athlete["status"] == ""