"""Defense-vs-position rankings and player market values.

Mixed into ``nfl_mcp.database.NFLDatabase``.
"""
from datetime import UTC, datetime, timedelta

from ._common import (
    _DBMixinBase,
    logger,
)


class RankingsMixin(_DBMixinBase):
    """Defense-vs-position rankings and player market values."""

    # ------------------------------------------------------------------
    # Defense rankings helpers (for lineup optimization)
    # ------------------------------------------------------------------
    def upsert_defense_rankings(self, rankings: dict[str, list[dict]], season: int, week: int = 0) -> int:
        """Insert or update defense vs position rankings.

        Args:
            rankings: Dict mapping position -> list of team rankings
                     Each ranking dict: {team, rank, points_allowed_avg, matchup_tier}
            season: NFL season year
            week: NFL week (0 for season-long averages)

        Returns:
            Number of rankings inserted/updated
        """
        if not rankings:
            return 0

        try:
            now = datetime.now(UTC).isoformat()
            processed = 0

            with self._pool.get_connection() as conn:
                for position, team_rankings in rankings.items():
                    for team_rank in team_rankings:
                        team = team_rank.get("team", "")
                        rank = team_rank.get("rank", 16)
                        pts_allowed = team_rank.get("points_allowed_avg", 0)
                        tier = team_rank.get("matchup_tier", "neutral")
                        provisional = 1 if team_rank.get("is_provisional") else 0

                        # The neutral placeholder is not a ranking. Stored, it
                        # was read back for a week as a confident rank 16.
                        if not team or team_rank.get("is_fallback"):
                            continue

                        conn.execute(
                            """
                            INSERT INTO defense_rankings
                                (season, week, team, position, rank, points_allowed_avg,
                                 matchup_tier, is_provisional, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(season, week, team, position) DO UPDATE SET
                                rank=excluded.rank,
                                points_allowed_avg=excluded.points_allowed_avg,
                                matchup_tier=excluded.matchup_tier,
                                is_provisional=excluded.is_provisional,
                                updated_at=excluded.updated_at
                            """,
                            (season, week, team, position, rank, pts_allowed, tier, provisional, now)
                        )
                        processed += 1

                conn.commit()
                return processed
        except Exception as e:
            logger.error(f"upsert_defense_rankings failed: {e}")
            return 0

    def get_defense_rankings(
        self,
        season: int,
        position: str | None = None,
        week: int = 0,
        max_age_hours: int = 24
    ) -> dict[str, list[dict]]:
        """Get cached defense rankings for positions.

        Args:
            season: NFL season year
            position: Specific position (QB, RB, WR, TE) or None for all
            week: NFL week (0 for season-long averages)
            max_age_hours: Maximum age of cached data

        Returns:
            Dict mapping position -> list of team rankings
        """
        try:
            cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
            rankings = {}

            with self._pool.get_connection() as conn:
                if position:
                    cur = conn.execute(
                        """
                        SELECT team, position, rank, points_allowed_avg, matchup_tier, is_provisional
                        FROM defense_rankings
                        WHERE season=? AND week=? AND position=? AND updated_at >= ?
                        ORDER BY rank ASC
                        """,
                        (season, week, position.upper(), cutoff)
                    )
                else:
                    cur = conn.execute(
                        """
                        SELECT team, position, rank, points_allowed_avg, matchup_tier, is_provisional
                        FROM defense_rankings
                        WHERE season=? AND week=? AND updated_at >= ?
                        ORDER BY position, rank ASC
                        """,
                        (season, week, cutoff)
                    )

                for row in cur.fetchall():
                    pos = row["position"]
                    if pos not in rankings:
                        rankings[pos] = []
                    rankings[pos].append({
                        "team": row["team"],
                        "rank": row["rank"],
                        "points_allowed_avg": row["points_allowed_avg"],
                        "matchup_tier": row["matchup_tier"],
                        "is_provisional": bool(row["is_provisional"]),
                    })

            return rankings
        except Exception as e:
            logger.debug(f"get_defense_rankings failed: {e}")
            return {}

    def get_matchup_difficulty(
        self,
        season: int,
        team: str,
        position: str,
        week: int = 0
    ) -> dict | None:
        """Get matchup difficulty for specific team/position.

        Args:
            season: NFL season year
            team: Team abbreviation
            position: Position (QB, RB, WR, TE)
            week: NFL week (0 for season-long)

        Returns:
            Dict with rank and tier, or None if not found
        """
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT rank, points_allowed_avg, matchup_tier
                    FROM defense_rankings
                    WHERE season=? AND week=? AND team=? AND position=?
                    """,
                    (season, week, team.upper(), position.upper())
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
                return None
        except Exception as e:
            logger.debug(f"get_matchup_difficulty failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Player market values (FantasyCalc) - powers trades & draft board
    # ------------------------------------------------------------------
    def upsert_player_values(self, values: list[dict], format_key: str) -> int:
        """Insert or update consensus player market values for a league format.

        Args:
            values: List of dicts with keys player_id, name, position, team,
                    value, redraft_value, overall_rank, position_rank, tier,
                    trend_30day, source
            format_key: Encoded league format (see player_values.format_key)

        Returns:
            Number of rows inserted/updated
        """
        if not values or not format_key:
            return 0

        try:
            now = datetime.now(UTC).isoformat()
            processed = 0
            with self._pool.get_connection() as conn:
                for v in values:
                    player_id = v.get("player_id")
                    if not player_id:
                        continue
                    conn.execute(
                        """
                        INSERT INTO player_values
                            (format_key, player_id, name, position, team, value,
                             redraft_value, overall_rank, position_rank, tier,
                             trend_30day, source, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(format_key, player_id) DO UPDATE SET
                            name=excluded.name,
                            position=excluded.position,
                            team=excluded.team,
                            value=excluded.value,
                            redraft_value=excluded.redraft_value,
                            overall_rank=excluded.overall_rank,
                            position_rank=excluded.position_rank,
                            tier=excluded.tier,
                            trend_30day=excluded.trend_30day,
                            source=excluded.source,
                            updated_at=excluded.updated_at
                        """,
                        (
                            format_key, str(player_id), v.get("name"), v.get("position"),
                            v.get("team"), v.get("value"), v.get("redraft_value"),
                            v.get("overall_rank"), v.get("position_rank"), v.get("tier"),
                            v.get("trend_30day"), v.get("source", "fantasycalc"), now,
                        ),
                    )
                    processed += 1
                conn.commit()
                return processed
        except Exception as e:
            logger.error(f"upsert_player_values failed: {e}")
            return 0

    def get_player_value(self, player_id: str, format_key: str) -> dict | None:
        """Get the cached market value for a single player in a given format."""
        if not player_id or not format_key:
            return None
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT * FROM player_values WHERE format_key=? AND player_id=?",
                    (format_key, str(player_id)),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.debug(f"get_player_value failed: {e}")
            return None

    def get_player_values(
        self,
        format_key: str,
        position: str | None = None,
        limit: int | None = None,
        max_age_hours: int | None = None,
    ) -> list[dict]:
        """Get cached player values for a format, ordered by overall rank (best first).

        Args:
            format_key: Encoded league format
            position: Optional position filter (QB, RB, WR, TE, ...)
            limit: Optional max rows
            max_age_hours: If set, only return rows fresher than this
        """
        if not format_key:
            return []
        try:
            params: list = [format_key]
            where = "format_key=?"
            if position:
                where += " AND position=?"
                params.append(position.upper())
            if max_age_hours is not None:
                cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
                where += " AND updated_at >= ?"
                params.append(cutoff)
            sql = (
                f"SELECT * FROM player_values WHERE {where} "
                "ORDER BY overall_rank ASC"
            )
            if limit:
                sql += " LIMIT ?"
                params.append(int(limit))
            with self._pool.get_connection() as conn:
                cur = conn.execute(sql, tuple(params))
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.debug(f"get_player_values failed: {e}")
            return []

    def get_player_values_last_updated(self, format_key: str) -> str | None:
        """Return the newest updated_at timestamp for a format (or None)."""
        if not format_key:
            return None
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT MAX(updated_at) FROM player_values WHERE format_key=?",
                    (format_key,),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None
        except Exception as e:
            logger.debug(f"get_player_values_last_updated failed: {e}")
            return None
