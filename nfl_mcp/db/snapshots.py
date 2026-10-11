"""Roster / transaction / matchup snapshots, cleanup and pruning.

Mixed into ``nfl_mcp.database.NFLDatabase``.
"""
import hashlib
from datetime import UTC, datetime, timedelta

from ._common import (
    SIGNAL_HISTORY_DAYS,
    _DBMixinBase,
    logger,
    season_start,
)


class SnapshotsMixin(_DBMixinBase):
    """Roster / transaction / matchup snapshots, cleanup and pruning."""

    # ------------------------------------------------------------------
    # Roster snapshot helpers
    # ------------------------------------------------------------------
    # Rows kept per snapshot key (league[/week]); readers use only the newest.
    SNAPSHOT_ROWS_PER_KEY = 3

    def _save_snapshot(self, conn, table: str, key: dict, payload_json: str, fetched_at: str) -> None:
        """Insert a snapshot unless it equals the newest one for ``key``.

        An unchanged payload only refreshes the newest row's ``fetched_at`` (so
        staleness checks still see a fresh fetch); a changed one is inserted
        and rows beyond ``SNAPSHOT_ROWS_PER_KEY`` for the key are dropped.
        """
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        where = " AND ".join(f"{col}=?" for col in key)
        params = tuple(key.values())
        latest = conn.execute(
            f"SELECT id, payload_hash FROM {table} WHERE {where} "
            "ORDER BY fetched_at DESC, id DESC LIMIT 1",
            params,
        ).fetchone()
        if latest is not None and latest[1] == payload_hash:
            conn.execute(f"UPDATE {table} SET fetched_at=? WHERE id=?", (fetched_at, latest[0]))
            return
        cols = ", ".join((*key, "payload_json", "payload_hash", "fetched_at"))
        marks = ", ".join("?" * (len(key) + 3))
        conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})",
            (*params, payload_json, payload_hash, fetched_at),
        )
        conn.execute(
            f"""
            DELETE FROM {table} WHERE {where} AND id NOT IN (
                SELECT id FROM {table} WHERE {where}
                ORDER BY fetched_at DESC, id DESC LIMIT ?
            )
            """,
            (*params, *params, self.SNAPSHOT_ROWS_PER_KEY),
        )

    def save_roster_snapshot(self, league_id: str, rosters) -> None:
        """Persist latest roster payload snapshot (JSON serialized)."""
        try:
            import datetime
            import json
            with self._pool.get_connection() as conn:
                self._save_snapshot(
                    conn, "roster_snapshots", {"league_id": league_id}, json.dumps(rosters),
                    datetime.datetime.now(datetime.UTC).isoformat(),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"save_roster_snapshot failed: {e}")

    def load_roster_snapshot(self, league_id: str, ttl_minutes: int = 15):
        """Load most recent roster snapshot; mark stale if beyond TTL."""
        try:
            import datetime
            import json
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT payload_json, fetched_at FROM roster_snapshots WHERE league_id=? ORDER BY fetched_at DESC LIMIT 1",
                    (league_id,)
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload_json, fetched_at = row
                dt = datetime.datetime.fromisoformat(fetched_at)
                age_seconds = (datetime.datetime.now(datetime.UTC) - dt).total_seconds()
                stale = age_seconds > ttl_minutes * 60
                return {"rosters": json.loads(payload_json), "stale": stale, "fetched_at": fetched_at, "age_seconds": age_seconds}
        except Exception as e:
            logger.debug(f"load_roster_snapshot failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Transaction snapshot helpers
    # ------------------------------------------------------------------
    def save_transaction_snapshot(self, league_id: str, week: int, transactions) -> None:
        """Persist latest transactions payload snapshot keyed by league/week."""
        try:
            import datetime
            import json
            with self._pool.get_connection() as conn:
                self._save_snapshot(
                    conn, "transaction_snapshots", {"league_id": league_id, "week": week},
                    json.dumps(transactions), datetime.datetime.now(datetime.UTC).isoformat(),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"save_transaction_snapshot failed: {e}")

    def load_transaction_snapshot(self, league_id: str, week: int | None = None, ttl_minutes: int = 15):
        """Load most recent transactions snapshot for league (and week if provided)."""
        try:
            import datetime
            import json
            with self._pool.get_connection() as conn:
                if week is not None:
                    cur = conn.execute(
                        "SELECT week, payload_json, fetched_at FROM transaction_snapshots WHERE league_id=? AND week=? ORDER BY fetched_at DESC LIMIT 1",
                        (league_id, week)
                    )
                else:
                    cur = conn.execute(
                        "SELECT week, payload_json, fetched_at FROM transaction_snapshots WHERE league_id=? ORDER BY fetched_at DESC LIMIT 1",
                        (league_id,)
                    )
                row = cur.fetchone()
                if not row:
                    return None
                snap_week, payload_json, fetched_at = row
                dt = datetime.datetime.fromisoformat(fetched_at)
                age_seconds = (datetime.datetime.now(datetime.UTC) - dt).total_seconds()
                stale = age_seconds > ttl_minutes * 60
                return {"transactions": json.loads(payload_json), "week": snap_week, "stale": stale, "fetched_at": fetched_at, "age_seconds": age_seconds}
        except Exception as e:
            logger.debug(f"load_transaction_snapshot failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Matchup snapshot helpers
    # ------------------------------------------------------------------
    def save_matchup_snapshot(self, league_id: str, week: int, matchups) -> None:
        try:
            import datetime
            import json
            with self._pool.get_connection() as conn:
                self._save_snapshot(
                    conn, "matchup_snapshots", {"league_id": league_id, "week": week},
                    json.dumps(matchups), datetime.datetime.now(datetime.UTC).isoformat(),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"save_matchup_snapshot failed: {e}")

    def load_matchup_snapshot(self, league_id: str, week: int, ttl_minutes: int = 15):
        try:
            import datetime
            import json
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    "SELECT payload_json, fetched_at FROM matchup_snapshots WHERE league_id=? AND week=? ORDER BY fetched_at DESC LIMIT 1",
                    (league_id, week)
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload_json, fetched_at = row
                dt = datetime.datetime.fromisoformat(fetched_at)
                age_seconds = (datetime.datetime.now(datetime.UTC) - dt).total_seconds()
                stale = age_seconds > ttl_minutes * 60
                return {"matchups": json.loads(payload_json), "stale": stale, "fetched_at": fetched_at, "age_seconds": age_seconds}
        except Exception as e:
            logger.debug(f"load_matchup_snapshot failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Snapshot cleanup helpers
    # ------------------------------------------------------------------
    def cleanup_old_snapshots(self, max_age_days: int = 7) -> dict[str, int]:
        """
        Clean up old snapshots to prevent unbounded database growth.

        Args:
            max_age_days: Delete snapshots older than this many days (default 7)

        Returns:
            Dictionary with count of deleted rows per table
        """
        import datetime
        cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=max_age_days)).isoformat()
        deleted = {"roster_snapshots": 0, "matchup_snapshots": 0, "transaction_snapshots": 0}

        try:
            with self._pool.get_connection() as conn:
                # Roster snapshots
                cursor = conn.execute(
                    "DELETE FROM roster_snapshots WHERE fetched_at < ?",
                    (cutoff,)
                )
                deleted["roster_snapshots"] = cursor.rowcount

                # Matchup snapshots
                cursor = conn.execute(
                    "DELETE FROM matchup_snapshots WHERE fetched_at < ?",
                    (cutoff,)
                )
                deleted["matchup_snapshots"] = cursor.rowcount

                # Transaction snapshots
                cursor = conn.execute(
                    "DELETE FROM transaction_snapshots WHERE fetched_at < ?",
                    (cutoff,)
                )
                deleted["transaction_snapshots"] = cursor.rowcount

                conn.commit()

                total = sum(deleted.values())
                if total > 0:
                    logger.info(f"[Cleanup] Deleted {total} old snapshots: {deleted}")

                return deleted
        except Exception as e:
            logger.warning(f"Failed to cleanup old snapshots: {e}")
            return deleted

    def prune_old_data(
        self,
        snapshot_days: int = 7,
        practice_days: int = 30,
        injury_history_days: int = 90,
        signal_history_days: int = SIGNAL_HISTORY_DAYS,
    ) -> dict[str, int]:
        """Prune the append-only tables, then checkpoint the WAL.

        Retention sits well past what any reader looks at:

        - snapshots: loaders only ever read the newest row per league/week.
        - ``player_practice_status``: read one practice week (6 days) back.
        - ``injury_history``: read at most 30 days back (``get_injury_trends``,
          briefing: 7). Each player's newest row older than the cutoff is kept
          as an anchor, so the ``LAG`` in ``get_injury_status_changes`` still
          sees the true previous status and a stable player's next change is
          not reported as a first sighting.

        - ``practice_report_history`` / ``injury_news_history`` /
          ``player_news``: calibration data (``player_news`` is also read for
          the news flags, at most two weeks back). Kept ``signal_history_days`` and never pruned
          inside the current NFL season (see ``season_start``), so a whole
          season is always there to backtest against.

        Season-keyed stats (snaps, usage, schedule, defense) are not touched:
        they are bounded per season and read by season.
        """
        deleted = self.cleanup_old_snapshots(max_age_days=snapshot_days)
        now = datetime.now(UTC)
        practice_cutoff = (now - timedelta(days=practice_days)).isoformat()
        history_cutoff = (now - timedelta(days=injury_history_days)).isoformat()
        signal_cutoff = min(now - timedelta(days=signal_history_days),
                            season_start(now)).isoformat()
        try:
            with self._pool.get_connection() as conn:
                deleted["player_practice_status"] = conn.execute(
                    "DELETE FROM player_practice_status WHERE updated_at < ?",
                    (practice_cutoff,),
                ).rowcount
                deleted["injury_history"] = conn.execute(
                    """
                    DELETE FROM injury_history
                    WHERE recorded_at < ?
                      AND id NOT IN (
                        SELECT id FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY player_id, team_id
                                ORDER BY recorded_at DESC, id DESC
                            ) AS rn
                            FROM injury_history
                            WHERE recorded_at < ?
                        ) WHERE rn = 1
                      )
                    """,
                    (history_cutoff, history_cutoff),
                ).rowcount
                for table in ("practice_report_history", "injury_news_history", "player_news"):
                    deleted[table] = conn.execute(
                        f"DELETE FROM {table} WHERE recorded_at < ?", (signal_cutoff,)
                    ).rowcount
                conn.commit()
                # Fold the WAL back into the main file so it does not stay at its
                # high-water mark after a large delete.
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except Exception as e:
            logger.warning(f"Failed to prune old data: {e}")
        total = sum(deleted.values())
        if total:
            logger.info(f"[Prune] Deleted {total} old rows: {deleted}")
        return deleted
