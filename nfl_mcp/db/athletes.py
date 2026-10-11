"""Athletes (Sleeper) and teams (ESPN) tables.

Mixed into ``nfl_mcp.database.NFLDatabase``.
"""
import json
from datetime import UTC, datetime

from ..teams import normalize_team
from ._common import (
    _DBMixinBase,
    logger,
)


class AthletesMixin(_DBMixinBase):
    """Athletes (Sleeper) and teams (ESPN) tables."""

    def upsert_athletes(self, athletes_data: list[dict]) -> int:
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
                    raw_team = athlete.get('team', '') or ''
                    team_id = normalize_team(raw_team) or raw_team
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

    def get_athlete_by_id(self, athlete_id: str) -> dict | None:
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

    def search_athletes_by_name(self, name: str, limit: int = 10) -> list[dict]:
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

    def get_athletes_by_ids(self, athlete_ids: list[str]) -> dict[str, dict]:
        """
        Batch get multiple athletes by their IDs in a single query.

        Args:
            athlete_ids: List of athlete IDs to fetch

        Returns:
            Dictionary mapping athlete_id -> athlete dict (only found athletes included)
        """
        if not athlete_ids:
            return {}

        # SQLite has a limit on parameters, so chunk if needed
        chunk_size = 500
        results = {}

        with self._get_connection() as conn:
            for i in range(0, len(athlete_ids), chunk_size):
                chunk = athlete_ids[i:i + chunk_size]
                placeholders = ','.join('?' * len(chunk))
                cursor = conn.execute(
                    f"SELECT * FROM athletes WHERE id IN ({placeholders})",
                    chunk
                )
                for row in cursor.fetchall():
                    athlete_dict = dict(row)
                    results[athlete_dict['id']] = athlete_dict

        return results

    def get_athletes_by_espn_ids(self, espn_ids: list[str]) -> dict[str, dict]:
        """``{espn_id: athlete row}`` for the Sleeper athletes carrying these ESPN ids.

        Sleeper's player payload (``raw``) names each player's ESPN id, which is
        the one exact join between the two id spaces (see ``injury_match``).
        """
        wanted = {str(i) for i in espn_ids or [] if i}
        if not wanted:
            return {}
        results: dict[str, dict] = {}
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT id, full_name, team_id, position, "
                    "CAST(json_extract(raw, '$.espn_id') AS TEXT) AS espn_id "
                    "FROM athletes WHERE json_extract(raw, '$.espn_id') IS NOT NULL"
                )
                for row in cursor.fetchall():
                    d = dict(row)
                    if d.get("espn_id") in wanted:
                        results[d["espn_id"]] = d
        except Exception as e:
            logger.debug(f"get_athletes_by_espn_ids failed: {e}")
        return results

    def get_athletes_by_team(self, team_id: str) -> list[dict]:
        """
        Get all athletes for a specific team.

        Args:
            team_id: The team identifier, in any spelling (``WAS``, ``was``,
                ``Washington Commanders``)

        Returns:
            List of athlete dictionaries for the team
        """
        # Rows are stored under the canonical code (see upsert_athletes); the
        # raw value matched nothing for WAS/JAC/LA or any lowercase input.
        team = normalize_team(team_id) or (team_id or "").strip().upper()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM athletes WHERE team_id = ? ORDER BY full_name",
                (team,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_athletes_by_positions(
        self, positions: list[str], exclude_ids: set[str] | None = None
    ) -> list[dict]:
        """Athletes at the given positions, optionally excluding known ids.

        Built for waiver discovery: the exclusion set is the league's rostered
        players, which is far smaller than the athlete table, so it is applied
        in Python rather than as a multi-thousand-parameter NOT IN.

        Args:
            positions: Position codes (e.g. ["RB", "WR"]). Empty means all.
            exclude_ids: Athlete ids to leave out.

        Returns:
            Athlete dicts with a team, newest data first is not guaranteed.
        """
        exclude_ids = exclude_ids or set()
        clause, params = "", []
        if positions:
            upper = [p.upper() for p in positions]
            clause = f" WHERE position IN ({','.join('?' * len(upper))})"
            params = upper

        with self._get_connection() as conn:
            cursor = conn.execute(f"SELECT * FROM athletes{clause}", params)
            return [
                row_dict for row in cursor.fetchall()
                if (row_dict := dict(row))["id"] not in exclude_ids
            ]

    def get_athlete_count(self) -> int:
        """
        Get the total number of athletes in the database.

        Returns:
            Total count of athletes
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM athletes")
            return cursor.fetchone()[0]

    def get_last_updated(self) -> str | None:
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

    # Teams-related methods

    def upsert_teams(self, teams_data: list[dict]) -> int:
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

    def get_team_by_id(self, team_id: str) -> dict | None:
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

    def get_team_by_abbreviation(self, abbreviation: str) -> dict | None:
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

    def get_all_teams(self) -> list[dict]:
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

    def get_teams_last_updated(self) -> str | None:
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
