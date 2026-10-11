"""Player news items, news fetch state and news source health.

Mixed into ``nfl_mcp.database.NFLDatabase``.
"""
from datetime import UTC, datetime

from ._common import (
    _DBMixinBase,
    logger,
)


class NewsMixin(_DBMixinBase):
    """Player news items, news fetch state and news source health."""

    # ------------------------------------------------------------------
    # Player news (schema v19, `news_sources`)
    # ------------------------------------------------------------------

    _PLAYER_NEWS_COLUMNS = ("content_hash", "source", "source_id", "player_id", "espn_id",
                            "player_name", "name_key", "team", "position", "headline",
                            "text", "url", "published_at", "recorded_at")

    def upsert_player_news(self, items: list[dict]) -> int:
        """Store news items; returns how many were new.

        Append-only and deduplicated on ``content_hash`` (source, player and
        normalized text, see ``news_sources.content_hash``): the same item on
        every poll is one row. Items without a player id or text are skipped.
        """
        now = datetime.now(UTC).isoformat()
        rows = []
        for it in items or []:
            if not it.get("player_id") or not (it.get("text") or "").strip() \
                    or not it.get("content_hash"):
                continue
            row = {**it, "recorded_at": it.get("recorded_at") or now}
            rows.append(tuple(row.get(c) for c in self._PLAYER_NEWS_COLUMNS))
        if not rows:
            return 0
        cols = ", ".join(self._PLAYER_NEWS_COLUMNS)
        marks = ", ".join("?" for _ in self._PLAYER_NEWS_COLUMNS)
        try:
            with self._pool.get_connection() as conn:
                before = conn.total_changes
                conn.executemany(
                    f"INSERT OR IGNORE INTO player_news({cols}) VALUES({marks})", rows)
                written = conn.total_changes - before
                conn.commit()
                return written
        except Exception as e:
            logger.error(f"upsert_player_news failed: {e}")
            return 0

    def get_player_news(self, player_ids: list[str] | None = None,
                        since: str | None = None, teams: list[str] | None = None,
                        limit: int | None = None) -> list[dict]:
        """Stored news items, newest first.

        `player_ids` (Sleeper ids) and `teams` filter (either matches);
        `since` (ISO) keeps items published -- or, undated, recorded -- on or
        after it.
        """
        where, params = [], []
        ors = []
        if player_ids:
            ids = [str(i) for i in player_ids if i]
            ors.append(f"player_id IN ({','.join('?' for _ in ids)})")
            params += ids
        if teams:
            ors.append(f"team IN ({','.join('?' for _ in teams)})")
            params += list(teams)
        if ors:
            where.append("(" + " OR ".join(ors) + ")")
        if since:
            where.append("COALESCE(published_at, recorded_at) >= ?")
            params.append(since)
        sql = "SELECT * FROM player_news"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(published_at, recorded_at) DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        try:
            with self._pool.get_connection() as conn:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except Exception as e:
            logger.debug(f"get_player_news failed: {e}")
            return []

    def record_news_fetch(self, source: str, items: int, written: int,
                          newest_published: str | None = None, status: str = "ok",
                          error: str | None = None, *, health: str | None = None,
                          detail: str | None = None, parsed_items: int | None = None,
                          expected_items: int | None = None,
                          consecutive_failures: int | None = None,
                          next_attempt_at: str | None = None,
                          at: str | None = None) -> None:
        """Remember a news source's fetch (freshness, incremental paging,
        health). A failed fetch keeps the last good ``newest_published`` and
        ``last_success_at``; a good one keeps ``last_error_at`` / ``last_error``."""
        now = at or datetime.now(UTC).isoformat()
        failed = status != "ok" or health == "failing"
        health = health or ("failing" if failed else "ok")
        failures = consecutive_failures if consecutive_failures is not None else (1 if failed else 0)
        try:
            with self._pool.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO news_fetch_state(source, fetched_at, newest_published,
                                                 items, written, status, error, health, detail,
                                                 parsed_items, expected_items,
                                                 consecutive_failures, last_success_at,
                                                 last_error_at, last_error, next_attempt_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source) DO UPDATE SET
                        fetched_at=excluded.fetched_at,
                        newest_published=COALESCE(excluded.newest_published,
                                                  news_fetch_state.newest_published),
                        items=excluded.items, written=excluded.written,
                        status=excluded.status, error=excluded.error,
                        health=excluded.health, detail=excluded.detail,
                        parsed_items=excluded.parsed_items,
                        expected_items=excluded.expected_items,
                        consecutive_failures=excluded.consecutive_failures,
                        last_success_at=COALESCE(excluded.last_success_at,
                                                 news_fetch_state.last_success_at),
                        last_error_at=COALESCE(excluded.last_error_at,
                                               news_fetch_state.last_error_at),
                        last_error=COALESCE(excluded.last_error, news_fetch_state.last_error),
                        next_attempt_at=excluded.next_attempt_at
                    """,
                    (source, now, newest_published, items, written, status, error, health,
                     detail, parsed_items, expected_items, failures,
                     None if failed else now, now if failed else None,
                     (error or detail) if failed else None, next_attempt_at),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"record_news_fetch failed: {e}")

    def get_news_fetch_state(self) -> dict[str, dict]:
        """``{source: {fetched_at, newest_published, items, written, status, error}}``."""
        try:
            with self._pool.get_connection() as conn:
                return {r["source"]: dict(r) for r in
                        conn.execute("SELECT * FROM news_fetch_state").fetchall()}
        except Exception as e:
            logger.debug(f"get_news_fetch_state failed: {e}")
            return {}

    def get_news_health(self) -> dict:
        """``{sources: {source: health}, warnings?}`` of the news fetch
        (`news_sources.health_summary`); {} without the table."""
        from ..news_sources import health_summary
        try:
            return health_summary(self.get_news_fetch_state())
        except Exception as e:
            logger.debug(f"news health unavailable: {e}")
            return {}
