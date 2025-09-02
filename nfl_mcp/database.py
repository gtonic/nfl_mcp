"""
SQLite database management for NFL athletes and teams data.

This module handles the persistence layer for athlete information fetched from
the Sleeper API and teams information from ESPN API, providing caching and lookup functionality.
"""

import sqlite3
import json
import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, List, Optional, Union
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class NFLDatabase:
    """SQLite database manager for NFL athlete and teams data with caching and lookup functionality."""
    
    def __init__(self, db_path: str = "nfl_data.db"):
        """
        Initialize the NFL database.
        
        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = Path(db_path)
        self._ensure_database()
    
    def _ensure_database(self) -> None:
        """Create database and tables if they don't exist."""
        with self._get_connection() as conn:
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
            
            # Create indexes for efficient lookups
            conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_team ON athletes(team_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_name ON athletes(full_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_position ON athletes(position)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_name ON teams(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_abbreviation ON teams(abbreviation)")
            
            conn.commit()
    
    @contextmanager
    def _get_connection(self):
        """Get a database connection with proper cleanup."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Enable dict-like access
        try:
            yield conn
        finally:
            conn.close()
    
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