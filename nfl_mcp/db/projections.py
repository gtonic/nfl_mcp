"""Projection log, projection accuracy, signal reviews and league check times.

Mixed into ``nfl_mcp.database.NFLDatabase``.
"""
import json
from datetime import UTC, datetime
from typing import Any

from ._common import (
    _DBMixinBase,
    logger,
)


class ProjectionsMixin(_DBMixinBase):
    """Projection log, projection accuracy, signal reviews and league check times."""

    # ------------------------------------------------------------------
    # Projection log (pre-kickoff projections) and league check times
    # ------------------------------------------------------------------
    def record_projections(
        self, season: int, week: int, scoring_key: str, rows: list[dict],
        league_id: str | None = None, source: str | None = None,
        now: str | None = None, ppr: float | None = None,
    ) -> int:
        """Append each player's projection when it differs from his last one.

        ``scoring_key`` is ``ScoringModel.fingerprint``; ``ppr`` is stored
        alongside for reading only. ``rows``: ``{player_id, projected_points,
        floor, ceiling, name, position, team}``. Whether a player's game has kicked off is the
        caller's call (it holds the kickoffs); this only de-duplicates, so a
        briefing run every fifteen minutes writes a row only when a number
        actually moved. Returns the number of rows written.
        """
        if not rows or not season or not week or not scoring_key:
            return 0
        now = now or datetime.now(UTC).isoformat()
        ppr = None if ppr is None else round(float(ppr), 2)
        written = 0
        try:
            with self._pool.get_connection() as conn:
                for r in rows:
                    pid = r.get("player_id")
                    points = r.get("projected_points")
                    if not pid or points is None:
                        continue
                    last = conn.execute(
                        """SELECT projected_points, floor, ceiling FROM projection_log
                           WHERE season=? AND week=? AND scoring_key=? AND player_id=?
                           ORDER BY recorded_at DESC, id DESC LIMIT 1""",
                        (season, week, scoring_key, str(pid)),
                    ).fetchone()
                    new = (round(float(points), 2),
                           None if r.get("floor") is None else round(float(r["floor"]), 2),
                           None if r.get("ceiling") is None else round(float(r["ceiling"]), 2))
                    if last and tuple(last) == new:
                        continue
                    conn.execute(
                        """INSERT INTO projection_log
                           (season, week, player_id, scoring_key, ppr, projected_points,
                            floor, ceiling, player_name, position, team, league_id,
                            source, recorded_at, signals)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (season, week, str(pid), scoring_key, ppr, *new, r.get("name"),
                         r.get("position"), r.get("team"), league_id, source, now,
                         json.dumps(r["signals"], separators=(",", ":"), default=str)
                         if r.get("signals") else None),
                    )
                    written += 1
                conn.commit()
        except Exception as e:
            logger.warning(f"record_projections failed: {e}")
        return written

    def get_logged_projections(
        self, season: int, week: int, scoring_key: str,
        player_ids: list[str] | None = None, as_of: str | None = None,
        which: str = "first",
    ) -> dict[str, dict]:
        """``{player_id: row}`` from the projection log for one week.

        ``which="first"`` is the earliest row (the projection the lineup was
        set against), ``"latest"`` the newest one — at or before ``as_of`` when
        given, which is "what it said when you last looked".
        """
        order = "ASC" if which == "first" else "DESC"
        params: list = [season, week, scoring_key]
        where = ["season=?", "week=?", "scoring_key=?"]
        if as_of:
            where.append("recorded_at <= ?")
            params.append(as_of)
        if player_ids:
            where.append(f"player_id IN ({','.join('?' * len(player_ids))})")
            params.extend(str(p) for p in player_ids)
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    f"""SELECT * FROM (
                            SELECT *, ROW_NUMBER() OVER (
                                PARTITION BY player_id ORDER BY recorded_at {order}, id {order}
                            ) AS rn
                            FROM projection_log WHERE {' AND '.join(where)}
                        ) WHERE rn = 1""",
                    params,
                )
                return {row["player_id"]: dict(row) for row in cur.fetchall()}
        except Exception as e:
            logger.debug(f"get_logged_projections failed: {e}")
            return {}

    def get_logged_projection_weeks(self, season: int, scoring_key: str) -> list[int]:
        """Weeks of a season with at least one logged projection, ascending."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT DISTINCT week FROM projection_log"
                    " WHERE season=? AND scoring_key=? ORDER BY week",
                    (season, scoring_key),
                )
                return [row[0] for row in cur.fetchall()]
        except Exception as e:
            logger.debug(f"get_logged_projection_weeks failed: {e}")
            return []

    def get_logged_week_rows(self, season: int, week: int) -> list[dict]:
        """The latest logged row per (scoring, player) for one week, every
        scoring -- the last pre-kickoff projection, which the accuracy loop
        grades (``projection_accuracy``)."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """SELECT * FROM (
                            SELECT *, ROW_NUMBER() OVER (
                                PARTITION BY scoring_key, player_id
                                ORDER BY recorded_at DESC, id DESC) AS rn
                            FROM projection_log WHERE season=? AND week=?
                        ) WHERE rn = 1""",
                    (season, week),
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.debug(f"get_logged_week_rows failed: {e}")
            return []

    def get_logged_weeks(self, season: int) -> list[int]:
        """Weeks of a season with any logged projection (every scoring)."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT DISTINCT week FROM projection_log WHERE season=? ORDER BY week",
                    (season,))
                return [row[0] for row in cur.fetchall()]
        except Exception as e:
            logger.debug(f"get_logged_weeks failed: {e}")
            return []

    _ACCURACY_COLUMNS = (
        "season", "week", "scoring_key", "player_id", "league_id", "player_name",
        "position", "team", "projected", "model_projection", "sleeper_projection",
        "floor", "ceiling", "actual", "actual_half_ppr", "actual_source", "played",
        "projection_source", "injury_status", "practice_status", "practice_pattern",
        "role_trend", "returning_teammates", "qb_mult", "news_flags", "signals",
        "log_source", "projected_at", "graded_at",
    )

    def upsert_projection_accuracy(self, rows: list[dict]) -> int:
        """Write graded rows (replacing a week's earlier grading of the same
        player, e.g. after a stat correction). Returns rows written."""
        if not rows:
            return 0
        cols = self._ACCURACY_COLUMNS
        sql = (f"INSERT OR REPLACE INTO projection_accuracy ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' * len(cols))})")

        def _cell(v: Any) -> Any:
            return json.dumps(v, separators=(",", ":"), default=str) \
                if isinstance(v, dict | list) else v
        try:
            with self._pool.get_connection() as conn:
                conn.executemany(sql, [tuple(_cell(r.get(c)) for c in cols) for r in rows])
                conn.commit()
            return len(rows)
        except Exception as e:
            logger.warning(f"upsert_projection_accuracy failed: {e}")
            return 0

    def get_projection_accuracy(
        self, season: int, weeks: list[int] | None = None, position: str | None = None,
        scoring_key: str | None = None, league_id: str | None = None,
    ) -> list[dict]:
        """Graded rows for a season, optionally filtered."""
        where: list[str] = ["season=?"]
        params: list[Any] = [season]
        if weeks:
            where.append(f"week IN ({','.join('?' * len(weeks))})")
            params.extend(int(w) for w in weeks)
        if position:
            where.append("position=?")
            params.append(position)
        if scoring_key:
            where.append("scoring_key=?")
            params.append(scoring_key)
        if league_id:
            where.append("league_id=?")
            params.append(str(league_id))
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    f"SELECT * FROM projection_accuracy WHERE {' AND '.join(where)} "
                    "ORDER BY week, scoring_key, player_id", params)
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.debug(f"get_projection_accuracy failed: {e}")
            return []

    def save_signal_review(self, season: int, week: int, scope: str, payload: dict,
                           rows_graded: int | None = None) -> bool:
        """Store one week's signal review summary (replacing an earlier one)."""
        try:
            with self._pool.get_connection() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO signal_reviews (season, week, scope, payload_json,"
                    " rows_graded, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (int(season), int(week), str(scope),
                     json.dumps(payload, separators=(",", ":"), default=str),
                     rows_graded, datetime.now(UTC).isoformat()))
                conn.commit()
            return True
        except Exception as e:
            logger.warning(f"save_signal_review failed: {e}")
            return False

    def get_signal_review(self, season: int, week: int | None = None,
                          scope: str = "all") -> dict | None:
        """``{season, week, scope, payload, rows_graded, created_at}`` for a
        stored review (the newest week when ``week`` is None), or None."""
        try:
            with self._pool.get_connection() as conn:
                if week is None:
                    row = conn.execute(
                        "SELECT * FROM signal_reviews WHERE season=? AND scope=?"
                        " ORDER BY week DESC LIMIT 1", (int(season), str(scope))).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM signal_reviews WHERE season=? AND week=? AND scope=?",
                        (int(season), int(week), str(scope))).fetchone()
            if not row:
                return None
            out = dict(row)
            out["payload"] = json.loads(out.pop("payload_json") or "{}")
            return out
        except Exception as e:
            logger.debug(f"get_signal_review failed: {e}")
            return None

    def get_accuracy_graded_weeks(self, season: int) -> dict[int, str]:
        """``{week: last graded_at}`` for the season's graded weeks."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT week, MAX(graded_at) FROM projection_accuracy WHERE season=? "
                    "GROUP BY week", (season,))
                return {row[0]: row[1] for row in cur.fetchall()}
        except Exception as e:
            logger.debug(f"get_accuracy_graded_weeks failed: {e}")
            return {}

    def get_league_last_check(self, league_id: str, roster_id: int) -> str | None:
        """When this roster's league changes were last read (ISO), or None."""
        try:
            with self._pool.get_connection() as conn:
                row = conn.execute(
                    "SELECT last_checked_at FROM league_checks WHERE league_id=? AND roster_id=?",
                    (str(league_id), int(roster_id)),
                ).fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.debug(f"get_league_last_check failed: {e}")
            return None

    def set_league_last_check(self, league_id: str, roster_id: int, checked_at: str) -> None:
        """Advance (or set) this roster's last-check time."""
        try:
            with self._pool.get_connection() as conn:
                conn.execute(
                    """INSERT INTO league_checks (league_id, roster_id, last_checked_at)
                       VALUES (?,?,?)
                       ON CONFLICT(league_id, roster_id)
                       DO UPDATE SET last_checked_at=excluded.last_checked_at""",
                    (str(league_id), int(roster_id), checked_at),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"set_league_last_check failed: {e}")

    def get_schedule_week_spans(self) -> list[dict]:
        """``[{season, week, first_kickoff, last_kickoff, teams}]`` for every
        cached week, newest season first — enough to tell which week is on
        without the NFL state feed."""
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """SELECT season, week, MIN(kickoff) AS first_kickoff,
                              MAX(kickoff) AS last_kickoff, COUNT(*) AS teams
                       FROM schedule_games WHERE kickoff IS NOT NULL
                       GROUP BY season, week ORDER BY season DESC, week ASC"""
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.debug(f"get_schedule_week_spans failed: {e}")
            return []
