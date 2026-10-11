"""Weekly snap stats, schedule games and usage stats.

Mixed into ``nfl_mcp.database.NFLDatabase``.
"""
import json
from datetime import UTC, datetime

from ..teams import normalize_team
from ._common import (
    _DBMixinBase,
    logger,
)


class StatsMixin(_DBMixinBase):
    """Weekly snap stats, schedule games and usage stats."""

    # ------------------------------------------------------------------
    # Player weekly snap stats helpers
    # ------------------------------------------------------------------
    def upsert_player_week_stats(self, stats: list[dict]) -> int:
        """Insert or update player weekly snap stats.

        Expected dict keys per item: player_id, season, week, snaps_offense, snaps_team_offense, snap_pct (optional), raw (optional)
        If snap_pct is missing but snaps_offense and snaps_team_offense are present and >0, it's computed.
        """
        if not stats:
            return 0
        now = datetime.now(UTC).isoformat()
        processed = 0
        with self._pool.get_connection() as conn:
            try:
                for s in stats:
                    player_id = s.get("player_id")
                    season = s.get("season")
                    week = s.get("week")
                    if player_id is None or season is None or week is None:
                        continue  # skip invalid rows silently
                    snaps_off = s.get("snaps_offense")
                    snaps_team = s.get("snaps_team_offense")
                    snap_pct = s.get("snap_pct")
                    if snap_pct is None and snaps_off is not None and snaps_team not in (None, 0):
                        try:
                            snap_pct = round((snaps_off / snaps_team) * 100, 1)
                        except Exception:
                            snap_pct = None
                    raw = s.get("raw", {})
                    conn.execute(
                        """
                        INSERT INTO player_week_stats(
                            player_id, season, week, snaps_offense, snaps_team_offense, snap_pct, updated_at, raw
                        ) VALUES(?,?,?,?,?,?,?, json(?))
                        ON CONFLICT(player_id, season, week) DO UPDATE SET
                            snaps_offense=excluded.snaps_offense,
                            snaps_team_offense=excluded.snaps_team_offense,
                            snap_pct=excluded.snap_pct,
                            updated_at=excluded.updated_at,
                            raw=excluded.raw
                        """,
                        (
                            player_id,
                            season,
                            week,
                            snaps_off,
                            snaps_team,
                            snap_pct,
                            now,
                            json.dumps(raw),
                        ),
                    )
                    processed += 1
                conn.commit()
                return processed
            except Exception as e:
                logger.error(f"upsert_player_week_stats failed: {e}")
                conn.rollback()
                return 0  # rolled back: nothing was written

    def get_player_snap_pct(self, player_id: str, season: int, week: int) -> dict | None:
        """Fetch cached snap percentage info for a player/week."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT snap_pct, snaps_offense, snaps_team_offense, updated_at
                    FROM player_week_stats
                    WHERE player_id=? AND season=? AND week=?
                    """,
                    (player_id, season, week),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return dict(row)
        except Exception as e:
            logger.debug(f"get_player_snap_pct failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Schedule / opponent helpers
    # ------------------------------------------------------------------
    def upsert_schedule_games(self, games: list[dict]) -> int:
        """Insert or update schedule games for opponent lookup.

        Each dict requires: season, week, team, opponent, is_home (bool/int), kickoff (optional), raw(optional)
        Caller should provide both directions (team/opponent swapped) if desired.
        """
        if not games:
            return 0
        now = datetime.now(UTC).isoformat()
        processed = 0
        with self._pool.get_connection() as conn:
            try:
                for g in games:
                    season = g.get("season")
                    week = g.get("week")
                    team = g.get("team")
                    opponent = g.get("opponent")
                    if None in (season, week, team, opponent):
                        continue
                    is_home = 1 if g.get("is_home") else 0
                    kickoff = g.get("kickoff")
                    raw = g.get("raw", {})
                    conn.execute(
                        """
                        INSERT INTO schedule_games(
                            season, week, team, opponent, is_home, kickoff, raw, updated_at)
                        VALUES(?,?,?,?,?,?, json(?), ?)
                        ON CONFLICT(season, week, team) DO UPDATE SET
                            opponent=excluded.opponent,
                            is_home=excluded.is_home,
                            kickoff=excluded.kickoff,
                            raw=excluded.raw,
                            updated_at=excluded.updated_at
                        """,
                        (season, week, team, opponent, is_home, kickoff, json.dumps(raw), now),
                    )
                    processed += 1
                conn.commit()
                return processed
            except Exception as e:
                logger.error(f"upsert_schedule_games failed: {e}")
                conn.rollback()
                return 0  # rolled back: nothing was written

    def get_opponent(self, season: int, week: int, team: str) -> str | None:
        """Return opponent abbreviation for team in given season/week if cached."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT opponent FROM schedule_games WHERE season=? AND week=? AND team=?",
                    (season, week, team),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return row[0]
        except Exception as e:
            logger.debug(f"get_opponent failed: {e}")
            return None

    def get_week_opponents(self, season: int, week: int) -> dict[str, str]:
        """``{team: opponent}`` for one week, from the cached schedule.

        One query instead of 32 `get_opponent` round-trips, and the schedule is
        the right source for "who plays whom" — the odds feed publishes several
        weeks at once and cannot answer it unambiguously.
        """
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT team, opponent FROM schedule_games WHERE season=? AND week=?",
                    (season, week),
                )
                return {row["team"]: row["opponent"] for row in cur.fetchall()}
        except Exception as e:
            logger.debug(f"get_week_opponents failed: {e}")
            return {}

    def get_week_kickoffs(self, season: int, week: int) -> dict[str, str]:
        """``{team: kickoff}`` for one week, so callers can tell what has been
        played from what is still to come."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT team, kickoff FROM schedule_games WHERE season=? AND week=?",
                    (season, week),
                )
                return {row["team"]: row["kickoff"] for row in cur.fetchall() if row["kickoff"]}
        except Exception as e:
            logger.debug(f"get_week_kickoffs failed: {e}")
            return {}

    def get_week_game_states(self, season: int, week: int) -> dict[str, dict]:
        """``{team: {kickoff, state, completed}}`` for one week.

        ``state`` is ESPN's ``pre``/``in``/``post`` as of the last schedule
        fetch, read out of the stored event rather than a column of its own:
        the scoreboard (``status``) and the team-schedule feed
        (``competitions[0].status``) both put it in the raw payload already.
        None when neither does.
        """
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT team, kickoff,
                        COALESCE(json_extract(raw, '$.status.type.state'),
                                 json_extract(raw, '$.competitions[0].status.type.state')) AS state,
                        COALESCE(json_extract(raw, '$.status.type.completed'),
                                 json_extract(raw, '$.competitions[0].status.type.completed')) AS completed
                    FROM schedule_games WHERE season=? AND week=?
                    """,
                    (season, week),
                )
                return {
                    row["team"]: {
                        "kickoff": row["kickoff"],
                        "state": row["state"],
                        "completed": None if row["completed"] is None else bool(row["completed"]),
                    }
                    for row in cur.fetchall()
                }
        except Exception as e:
            logger.debug(f"get_week_game_states failed: {e}")
            return {}

    def get_usage_for_week(self, season: int, week: int) -> list[dict]:
        """All recorded usage rows for one week (empty before it is ingested)."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT * FROM player_usage_stats WHERE season=? AND week=?",
                    (season, week),
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.debug(f"get_usage_for_week failed: {e}")
            return []

    def get_kickoff_week_index(self, season: int) -> dict[tuple[str, str], int]:
        """Map ``(team, 'YYYY-MM-DD')`` to the NFL week of that kickoff.

        Lets callers holding a bare kickoff timestamp — the odds feed, for
        instance, which publishes two weeks at once and labels neither — decide
        which week a game belongs to instead of guessing from the date.
        """
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT team, substr(kickoff, 1, 10) AS day, week "
                    "FROM schedule_games WHERE season=?",
                    (season,),
                )
                return {(row["team"], row["day"]): row["week"] for row in cur.fetchall()}
        except Exception as e:
            logger.debug(f"get_kickoff_week_index failed: {e}")
            return {}

    def get_team_schedule_from_cache(self, team: str, season: int) -> list[dict]:
        """Fetch team's full schedule from cache (all weeks for given season).

        Returns list of games with: week, opponent, is_home, kickoff, raw
        """
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT week, opponent, is_home, kickoff, raw
                    FROM schedule_games
                    WHERE season=? AND team=?
                    ORDER BY week ASC
                    """,
                    (season, normalize_team(team) or team),
                )
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.debug(f"get_team_schedule_from_cache failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Usage stats helpers (targets, routes, RZ touches)
    # ------------------------------------------------------------------
    def upsert_usage_stats(self, stats: list[dict]) -> int:
        """Insert or update player weekly usage stats.

        Expected dict keys: player_id, season, week, targets, routes, rz_touches, touches, air_yards, snap_share (all optional except player_id/season/week).
        """
        if not stats:
            return 0
        now = datetime.now(UTC).isoformat()
        processed = 0
        with self._pool.get_connection() as conn:
            try:
                for s in stats:
                    player_id = s.get("player_id")
                    season = s.get("season")
                    week = s.get("week")
                    if None in (player_id, season, week):
                        continue
                    conn.execute(
                        """
                        INSERT INTO player_usage_stats(
                            player_id, season, week, targets, routes, rz_touches, touches, air_yards, snap_share, updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(player_id, season, week) DO UPDATE SET
                            targets=excluded.targets,
                            routes=excluded.routes,
                            rz_touches=excluded.rz_touches,
                            touches=excluded.touches,
                            air_yards=excluded.air_yards,
                            snap_share=excluded.snap_share,
                            updated_at=excluded.updated_at
                        """,
                        (
                            player_id,
                            season,
                            week,
                            s.get("targets"),
                            s.get("routes"),
                            s.get("rz_touches"),
                            s.get("touches"),
                            s.get("air_yards"),
                            s.get("snap_share"),
                            now,
                        ),
                    )
                    processed += 1
                conn.commit()
                return processed
            except Exception as e:
                logger.error(f"upsert_usage_stats failed: {e}")
                conn.rollback()
                return 0  # rolled back: nothing was written

    def get_usage_last_n_weeks(self, player_id: str, season: int, current_week: int, n: int = 3) -> dict | None:
        """Calculate average usage stats for a player over the last n weeks (excluding current_week)."""
        try:
            start_week = max(1, current_week - n)
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT
                        AVG(targets) as targets_avg,
                        AVG(routes) as routes_avg,
                        AVG(rz_touches) as rz_touches_avg,
                        AVG(snap_share) as snap_share_avg,
                        COUNT(*) as weeks_sample
                    FROM player_usage_stats
                    WHERE player_id=? AND season=? AND week BETWEEN ? AND ?
                    """,
                    (player_id, season, start_week, current_week - 1),
                )
                row = cur.fetchone()
                if not row or row["weeks_sample"] == 0:
                    return None
                return dict(row)
        except Exception as e:
            logger.debug(f"get_usage_last_n_weeks failed: {e}")
            return None

    def get_usage_weekly_breakdown(self, player_id: str, season: int, current_week: int, n: int = 3) -> list[dict] | None:
        """Get individual week usage stats for trend calculation.

        Returns list of dicts with week and stats, ordered by week DESC.
        Used to calculate if usage is trending up/down/flat.
        """
        try:
            start_week = max(1, current_week - n)
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT
                        week,
                        targets,
                        routes,
                        rz_touches,
                        snap_share,
                        touches
                    FROM player_usage_stats
                    WHERE player_id=? AND season=? AND week BETWEEN ? AND ?
                    ORDER BY week DESC
                    """,
                    (player_id, season, start_week, current_week - 1),
                )
                rows = cur.fetchall()
                if not rows:
                    return None
                return [dict(row) for row in rows]
        except Exception as e:
            logger.debug(f"get_usage_weekly_breakdown failed: {e}")
            return None
