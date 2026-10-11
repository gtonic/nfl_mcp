"""Practice reports, injury reports, injury history and status changes.

Mixed into ``nfl_mcp.database.NFLDatabase``.
"""
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from ..teams import normalize_team
from ._common import (
    _DEFAULT_SEVERITY,
    CLEARED_REPORT_TEXT,
    _DBMixinBase,
    logger,
)


class InjuriesMixin(_DBMixinBase):
    """Practice reports, injury reports, injury history and status changes."""

    # ------------------------------------------------------------------
    # Practice status helpers (DNP/LP/FP)
    # ------------------------------------------------------------------
    def upsert_practice_status(self, reports: list[dict]) -> int:
        """Insert or update real practice reports, one row per player and day.

        Expected keys: player_name, team, date (report day, YYYY-MM-DD, US
        Eastern), status (DNP/LP/FP/REST) and source; optional position,
        description, injury, game_status, game_date, season, week, estimated.
        A row keyed only by ``player_id`` (the pre-v14 shape) is still accepted.
        A lower-ranked source never overwrites a higher-ranked one for the
        same day: a news blurb does not replace the official report.
        """
        if not reports:
            return 0
        from ..opportunity_tools import norm_name
        from ..practice_reports import SOURCE_RANK

        now = datetime.now(UTC).isoformat()
        processed = 0
        with self._pool.get_connection() as conn:
            try:
                for r in reports:
                    date_str = r.get("date")
                    status = r.get("status")
                    name_key = norm_name(r.get("player_name"))
                    team = normalize_team(r.get("team")) or ""
                    player_id = str(r.get("player_id") or "")
                    if not date_str or not status or not ((name_key and team) or player_id):
                        continue
                    source = r.get("source", "unknown")
                    cur = conn.execute(
                        """
                        INSERT INTO player_practice_status(
                            name_key, team, player_id, date, player_name, position,
                            status, description, injury, game_status, game_date,
                            season, week, estimated, source, source_rank, updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(name_key, team, player_id, date) DO UPDATE SET
                            status=excluded.status,
                            description=excluded.description,
                            injury=excluded.injury,
                            game_status=COALESCE(excluded.game_status, game_status),
                            game_date=COALESCE(excluded.game_date, game_date),
                            position=COALESCE(excluded.position, position),
                            season=COALESCE(excluded.season, season),
                            week=COALESCE(excluded.week, week),
                            estimated=excluded.estimated,
                            source=excluded.source,
                            source_rank=excluded.source_rank,
                            updated_at=excluded.updated_at
                        WHERE excluded.source_rank >= player_practice_status.source_rank
                        """,
                        (
                            name_key, team, player_id, date_str, r.get("player_name"),
                            r.get("position"), status, r.get("description"), r.get("injury"),
                            r.get("game_status"), r.get("game_date"), r.get("season"),
                            r.get("week"), 1 if r.get("estimated") else 0, source,
                            SOURCE_RANK.get(source, 0), now,
                        ),
                    )
                    processed += cur.rowcount
                    # Every distinct (player, day, status, source) is kept, also
                    # when the live row above refused it (a lower-ranked source)
                    # or later replaces it: the backtest needs what was known when.
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO practice_report_history(
                            name_key, team, player_id, date, status, source,
                            player_name, game_status, season, week, recorded_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (name_key, team, player_id, date_str, status, source or "",
                         r.get("player_name"), r.get("game_status"), r.get("season"),
                         r.get("week"), now),
                    )
                conn.commit()
                return processed
            except Exception as e:
                logger.error(f"upsert_practice_status failed: {e}")
                conn.rollback()
                return 0  # rolled back: nothing was written

    def get_practice_reports(
        self,
        player_name: str | None,
        team: str | None,
        season: int | None = None,
        week: int | None = None,
    ) -> list[dict]:
        """One player's stored report days, oldest first.

        With ``season``/``week``: that week's report, read together with
        ``week + 1`` — a short-week team's NFL.com report is stored under the
        next week while Sleeper's counter still shows this one on Monday and
        Tuesday (its ESPN notes land under this one). Only for the current
        week: a past week's lookup reads that week alone. Without: days since this
        practice week's Monday (the previous Tuesday on a Monday, so a Monday
        night team keeps its Thu-Sat report). Either way only the latest
        practice week among the rows is returned, so last week's Friday never
        passes for this week's Wednesday.
        """
        from ..opportunity_tools import norm_name
        from ..practice_reports import latest_practice_week, reads_next_week

        name_key = norm_name(player_name)
        team = normalize_team(team)
        if not name_key or not team:
            return []
        try:
            with self._pool.get_connection() as conn:
                if season and week:
                    cur = conn.execute(
                        """
                        SELECT * FROM player_practice_status
                        WHERE name_key=? AND team=? AND season=? AND week IN (?, ?)
                        ORDER BY date ASC
                        """,
                        # week + 1 only for the current week (reads_next_week).
                        (name_key, team, int(season), int(week),
                         int(week) + 1 if reads_next_week(self, season, week) else int(week)),
                    )
                else:
                    from ..practice_reports import practice_week_start, to_eastern
                    today = to_eastern(datetime.now(UTC)).date()
                    cutoff = min(practice_week_start(today),
                                 practice_week_start(today, include_monday=True)).isoformat()
                    cur = conn.execute(
                        """
                        SELECT * FROM player_practice_status
                        WHERE name_key=? AND team=? AND date >= ?
                        ORDER BY date ASC
                        """,
                        (name_key, team, cutoff),
                    )
                return latest_practice_week([dict(row) for row in cur.fetchall()])
        except Exception as e:
            logger.debug(f"get_practice_reports failed: {e}")
            return []

    def get_team_practice_days(
        self, season: int, week: int, before: Any = None, source: str | None = None
    ) -> dict[str, dict[str, str]]:
        """``team -> {name_key: status}`` for each team's latest stored day
        before ``before`` (a date or ISO string) in one week."""
        before_s = before.isoformat() if hasattr(before, "isoformat") else before
        try:
            with self._pool.get_connection() as conn:
                params: list = [int(season), int(week)]
                sql = ("SELECT team, name_key, date, status FROM player_practice_status"
                       " WHERE season=? AND week=?")
                if before_s:
                    sql += " AND date < ?"
                    params.append(before_s)
                if source:
                    sql += " AND source=?"
                    params.append(source)
                rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        except Exception as e:
            logger.debug(f"get_team_practice_days failed: {e}")
            return {}
        latest: dict[str, str] = {}
        for r in rows:
            latest[r["team"]] = max(latest.get(r["team"], ""), r["date"])
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            if r["date"] == latest[r["team"]]:
                out.setdefault(r["team"], {})[r["name_key"]] = r["status"]
        return out

    def get_team_practice_snapshots(
        self, season: int, week: int, source: str | None = None
    ) -> dict[str, dict[str, dict[str, str]]]:
        """``team -> {date: {name_key: status}}`` for every stored day of one
        week -- what the snapshot dating in ``practice_reports`` compares a
        fresh page against."""
        try:
            with self._pool.get_connection() as conn:
                params: list = [int(season), int(week)]
                sql = ("SELECT team, name_key, date, status FROM player_practice_status"
                       " WHERE season=? AND week=?")
                if source:
                    sql += " AND source=?"
                    params.append(source)
                rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            logger.debug(f"get_team_practice_snapshots failed: {e}")
            return {}
        out: dict[str, dict[str, dict[str, str]]] = {}
        for r in rows:
            out.setdefault(r["team"], {}).setdefault(r["date"], {})[r["name_key"]] = r["status"]
        return out

    def get_latest_practice_status(self, player_id: str, max_age_hours: int = 72) -> dict | None:
        """Most recent practice row stored under ``player_id`` (pre-v14 writers).

        Real reports are keyed by name and team; read them with
        ``get_practice_reports``.
        """
        try:
            cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT status, date, updated_at, source
                    FROM player_practice_status
                    WHERE player_id=? AND updated_at >= ?
                    ORDER BY date DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (player_id, cutoff),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return dict(row)
        except Exception as e:
            logger.debug(f"get_latest_practice_status failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Injury reports helpers
    # ------------------------------------------------------------------
    def upsert_injuries(self, injuries: list[dict], prune_missing: bool = False,
                        complete_teams: set[str] | None = None) -> int:
        """Insert or update player injury reports.

        Args:
            injuries: List of injury dicts with keys:
                - player_id: Player identifier
                - player_name: Player full name
                - team_id: Team abbreviation
                - position: Position (optional)
                - injury_status: Status (Out/Questionable/Doubtful/DNP/etc)
                - injury_type: Body part/type (optional)
                - injury_description: Full description (optional)
                - game_status: Game-day designation (Active/Inactive/IR/PUP)
                - severity: 1-5 scale (optional)
                - confidence: 0-100 confidence score (optional)
                - sources: List of source names (optional)
                - date_reported: Date of injury report (optional)
            prune_missing: The batch is a complete crawl. A player on a team
                present in the batch but absent from it has left the report —
                he is marked ``Active`` (with a history row) instead of keeping
                his last designation forever. Teams missing from the batch are
                left alone, so a team whose fetch failed keeps its reports.
            complete_teams: With ``prune_missing``, the teams the crawl covered
                completely (every page listed, every report resolved). Only
                these are pruned — a team in the batch whose crawl was partial
                is not, and one absent from the batch (no reports left) is.
                ``None`` falls back to the teams present in the batch.

        A report whose ``player_name`` is empty or ``"Unknown"`` (a failed name
        fetch) keeps the name already stored for that player.

        Returns:
            Number of rows inserted/updated
        """
        injuries = injuries or []
        if not injuries and not (prune_missing and complete_teams):
            return 0

        try:
            now = datetime.now(UTC).isoformat()
            with self._pool.get_connection() as conn:
                # Prior state for every known player, so `injury_history` records
                # transitions instead of a fresh copy of the feed on every
                # prefetch cycle (which would add thousands of identical rows
                # every 15 minutes and make the timeline useless).
                prior = {
                    (row["player_id"], row["team_id"]): (
                        row["injury_status"],
                        row["injury_type"],
                    )
                    for row in conn.execute(
                        "SELECT player_id, team_id, injury_status, injury_type "
                        "FROM player_injuries"
                    )
                }
                history_rows = []
                news_rows = []

                processed = 0
                for inj in injuries:
                    player_id = inj.get("player_id")
                    if not player_id:
                        continue

                    # Log the first sighting and every later status/type change.
                    key = (player_id, inj.get("team_id", ""))
                    new_state = (
                        inj.get("injury_status", "Unknown"),
                        inj.get("injury_type"),
                    )
                    if prior.get(key) != new_state:
                        history_rows.append(
                            (player_id, key[1], new_state[0], new_state[1], now,
                             inj.get("return_date"))
                        )

                    # Handle sources as JSON array
                    sources = inj.get("sources", ["ESPN"])
                    if isinstance(sources, list):
                        sources = json.dumps(sources)

                    conn.execute(
                        """
                        INSERT INTO player_injuries(
                            player_id, player_name, team_id, position,
                            injury_status, injury_type, injury_description,
                            game_status, severity, confidence, sources,
                            date_reported, return_date, updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(player_id, team_id) DO UPDATE SET
                            player_name=CASE WHEN excluded.player_name IN ('', 'Unknown')
                                THEN player_name ELSE excluded.player_name END,
                            position=excluded.position,
                            injury_status=excluded.injury_status,
                            injury_type=excluded.injury_type,
                            injury_description=excluded.injury_description,
                            game_status=excluded.game_status,
                            severity=excluded.severity,
                            confidence=excluded.confidence,
                            sources=excluded.sources,
                            date_reported=excluded.date_reported,
                            return_date=excluded.return_date,
                            updated_at=excluded.updated_at
                        """,
                        (
                            player_id,
                            inj.get("player_name", ""),
                            inj.get("team_id", ""),
                            inj.get("position"),
                            inj.get("injury_status", "Unknown"),
                            inj.get("injury_type"),
                            inj.get("injury_description"),
                            inj.get("game_status"),
                            inj.get("severity"),
                            inj.get("confidence", 50),
                            sources,
                            inj.get("date_reported"),
                            inj.get("return_date"),
                            now
                        )
                    )
                    processed += 1
                    news = self._news_history_row(inj, sources, now)
                    if news:
                        news_rows.append(news)

                if prune_missing:
                    history_rows.extend(self._clear_missing_injuries(
                        conn, injuries, prior, now, complete_teams=complete_teams))

                if history_rows:
                    conn.executemany(
                        """
                        INSERT INTO injury_history(
                            player_id, team_id, injury_status, injury_type, recorded_at,
                            return_date
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        history_rows,
                    )
                    logger.info(
                        f"upsert_injuries: {len(history_rows)} status change(s) recorded"
                    )
                if news_rows:
                    # Content-deduplicated: the same blurb on every crawl is one row.
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO injury_news_history(
                            content_hash, player_id, team_id, player_name, injury_status,
                            text, date_reported, return_date, source, recorded_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        news_rows,
                    )
                conn.commit()
                return processed
        except Exception as e:
            logger.error(f"upsert_injuries failed: {e}")
            return 0

    @staticmethod
    def _news_history_row(inj: dict, sources: Any, now: str) -> tuple | None:
        """An ``injury_news_history`` row for a report's blurb, or None.

        Deduplicated on (player, text, date reported, source): the blurb is the
        signal ``news_signals`` reads, and its date is what ages it.
        """
        text = (inj.get("injury_description") or "").strip()
        player_id = inj.get("player_id")
        if not text or text == CLEARED_REPORT_TEXT or not player_id:
            return None
        source = sources if isinstance(sources, str) else json.dumps(sources)
        digest = hashlib.sha1(
            "\x1f".join((str(player_id), text, str(inj.get("date_reported") or ""),
                          source)).encode("utf-8")
        ).hexdigest()
        return (digest, str(player_id), inj.get("team_id", ""), inj.get("player_name"),
                inj.get("injury_status"), text, inj.get("date_reported"),
                inj.get("return_date"), source, now)

    @staticmethod
    def _clear_missing_injuries(
        conn: sqlite3.Connection, injuries: list[dict], prior: dict, now: str,
        complete_teams: set[str] | None = None,
    ) -> list[tuple]:
        """Mark reports that a complete crawl no longer lists as ``Active``.

        Without this a recovered player keeps his last designation indefinitely:
        ESPN drops him from the feed rather than reporting him healthy, and the
        briefing's worst-status-wins merge then benches him on a stale "Out".
        Only ``complete_teams`` are touched (default: the teams in the batch).
        Returns the history rows for the transitions.
        """
        seen = {(inj.get("player_id"), inj.get("team_id", "")) for inj in injuries}
        crawled_teams = (set(complete_teams) if complete_teams is not None
                         else {team for _, team in seen if team})
        history = []
        for (player_id, team_id), (status, _) in prior.items():
            if team_id not in crawled_teams or (player_id, team_id) in seen:
                continue
            if status == "Active":
                continue
            conn.execute(
                """
                UPDATE player_injuries
                SET injury_status='Active', injury_type=NULL, game_status=NULL,
                    severity=1, injury_description=?, return_date=NULL, updated_at=?
                WHERE player_id=? AND team_id=?
                """,
                (CLEARED_REPORT_TEXT, now, player_id, team_id),
            )
            history.append((player_id, team_id, "Active", None, now, None))
        if history:
            logger.info(f"upsert_injuries: {len(history)} report(s) cleared as no longer listed")
        return history

    def get_all_current_injuries(self) -> list[dict]:
        """Every player's latest injury report, league-wide.

        The league-wide counterpart to ``get_team_injuries_from_cache``, for
        callers that hold a roster rather than a team — a fantasy roster spans
        a dozen teams, and asking per team would be a dozen queries.

        No TTL filter: the caller decides what age is acceptable and is told
        how old the data is (see ``get_injury_data_age_hours``), rather than
        silently receiving an empty list.
        """
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT player_id, player_name, team_id, position,
                           injury_status, injury_type, injury_description,
                           game_status, severity, confidence, sources,
                           date_reported, return_date, updated_at
                    FROM player_injuries
                    """
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.warning(f"get_all_current_injuries failed: {e}")
            return []

    def get_team_injuries_from_cache(
        self,
        team_id: str,
        max_age_hours: int | None = None
    ) -> list[dict]:
        """Get cached injury reports for a team with adaptive TTL.

        Args:
            team_id: Team abbreviation
            max_age_hours: Maximum age of cached data in hours
                          If None, uses adaptive TTL based on day of week

        Returns:
            List of injury dicts
        """
        try:
            # Adaptive TTL: 2h during game windows (Thu-Mon), 12h for off-days
            if max_age_hours is None:
                day_of_week = datetime.now(UTC).weekday()
                # Thu=3, Fri=4, Sat=5, Sun=6, Mon=0
                if day_of_week in (0, 3, 4, 5, 6):
                    max_age_hours = 2  # Game window - fresher data
                else:
                    max_age_hours = 12  # Off-day - longer cache

            cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT player_id, player_name, team_id, position,
                           injury_status, injury_type, injury_description,
                           game_status, severity, confidence, sources,
                           date_reported, return_date, updated_at
                    FROM player_injuries
                    WHERE team_id=? AND updated_at >= ?
                    ORDER BY updated_at DESC
                    """,
                    (normalize_team(team_id) or team_id, cutoff),
                )
                rows = cur.fetchall()
                result = []
                for row in rows:
                    d = dict(row)
                    # Parse sources JSON
                    if d.get("sources"):
                        try:
                            d["sources"] = json.loads(d["sources"])
                        except (json.JSONDecodeError, TypeError):
                            d["sources"] = ["ESPN"]
                    result.append(d)
                return result
        except Exception as e:
            logger.debug(f"get_team_injuries_from_cache failed: {e}")
            return []

    def get_injury_player_names(self) -> dict[str, str]:
        """``{player_id: player_name}`` for every stored injury report with a
        real name. Seeds the crawl's athlete-name cache: a cold process used
        to fetch one ESPN athlete page per report (~1900) just for names it
        had already stored."""
        try:
            with self._pool.get_connection() as conn:
                rows = conn.execute(
                    "SELECT player_id, player_name FROM player_injuries "
                    "WHERE player_name IS NOT NULL AND player_name NOT IN ('', 'Unknown')"
                ).fetchall()
            return {str(r[0]): r[1] for r in rows}
        except Exception as e:
            logger.debug(f"get_injury_player_names failed: {e}")
            return {}

    def get_player_injury_from_cache(
        self,
        player_id: str,
        max_age_hours: int | None = None
    ) -> dict | None:
        """Get cached injury report for a specific player with adaptive TTL.

        Args:
            player_id: Player identifier
            max_age_hours: Maximum age of cached data in hours
                          If None, uses adaptive TTL based on day of week

        Returns:
            Injury dict or None
        """
        try:
            # Adaptive TTL: 2h during game windows (Thu-Mon), 12h for off-days
            if max_age_hours is None:
                day_of_week = datetime.now(UTC).weekday()
                max_age_hours = 2 if day_of_week in (0, 3, 4, 5, 6) else 12

            cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT player_id, player_name, team_id, position,
                           injury_status, injury_type, injury_description,
                           game_status, severity, confidence, sources,
                           date_reported, return_date, updated_at
                    FROM player_injuries
                    WHERE player_id=? AND updated_at >= ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (player_id, cutoff),
                )
                row = cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                # Parse sources JSON
                if d.get("sources"):
                    try:
                        d["sources"] = json.loads(d["sources"])
                    except (json.JSONDecodeError, TypeError):
                        d["sources"] = ["ESPN"]
                return d
        except Exception as e:
            logger.debug(f"get_player_injury_from_cache failed: {e}")
            return None

    def find_player_injury(
        self,
        player_name: str | None,
        team_id: str | None,
        max_age_hours: int | None = None,
    ) -> dict | None:
        """Cached injury report for a player identified by name and team.

        The report table is keyed by ESPN athlete ids, which share nothing with
        the Sleeper ids a roster carries — see ``injury_match``. Looking a
        Sleeper id up in ``get_player_injury_from_cache`` finds nothing, or a
        different player whose ESPN id happens to be the same number.
        """
        # Deferred: both modules pull in the wider tool stack.
        from ..injury_match import build_injury_index, find_report, team_names_from
        from ..teams import normalize_team

        team = normalize_team(team_id)
        if not player_name or not team:
            return None
        rows = self.get_team_injuries_from_cache(team, max_age_hours)
        return find_report({"full_name": player_name},
                           build_injury_index(rows, team_names_from(self)), team)

    def add_injury_history(self, player_id: str, team_id: str, status: str, injury_type: str | None = None) -> bool:
        """Add entry to injury history for trend analysis.

        Args:
            player_id: Player identifier
            team_id: Team abbreviation
            status: Injury status
            injury_type: Type of injury (optional)

        Returns:
            True if successful
        """
        try:
            now = datetime.now(UTC).isoformat()
            with self._pool.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO injury_history(player_id, team_id, injury_status, injury_type, recorded_at)
                    VALUES(?,?,?,?,?)
                    """,
                    (player_id, team_id, status, injury_type, now)
                )
                conn.commit()
                return True
        except Exception as e:
            logger.debug(f"add_injury_history failed: {e}")
            return False

    def get_injury_history(self, player_id: str, limit: int = 10) -> list[dict]:
        """Get injury history for a player.

        Args:
            player_id: Player identifier
            limit: Max number of history entries

        Returns:
            List of historical injury entries
        """
        try:
            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT player_id, team_id, injury_status, injury_type, recorded_at,
                           return_date
                    FROM injury_history
                    WHERE player_id=?
                    ORDER BY recorded_at DESC
                    LIMIT ?
                    """,
                    (player_id, limit)
                )
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.debug(f"get_injury_history failed: {e}")
            return []

    def get_injury_status_changes(
        self, since: str, teams: list[str] | None = None, limit: int = 100,
        player_ids: list[str] | None = None, direction: str | None = None,
        severity_map: dict[str, int] | None = None,
    ) -> list[dict]:
        """Status transitions recorded since an ISO timestamp, newest first.

        Each row carries the preceding status (``previous_status``, NULL for a
        player's first sighting) so callers can tell a downgrade from a
        recovery, plus the player's name and position from the current report.

        Every filter is applied *before* ``limit``. Filtering the returned rows
        instead silently drops matches: a single feed backfill writes thousands
        of first sightings in one timestamp, so "the newest 500 rows, then keep
        the downgrades" can legitimately return nothing while downgrades exist.

        Args:
            since: ISO-8601 cutoff; only changes at or after this are returned.
            teams: Optional team abbreviations to filter to.
            limit: Max rows, applied after every filter.
            player_ids: Optional player ids to filter to.
            direction: "worse", "better", "lateral" or "new" — needs
                ``severity_map``; the severity vocabulary lives with the injury
                service, not here.
            severity_map: status -> severity rank, for ``direction``.
        """
        try:
            params: list = []
            # Severity is resolved in SQL so `direction` can filter before the
            # LIMIT. Without a map there is nothing to compare, so the caller's
            # direction is ignored rather than guessed at.
            direction = (direction or "").lower() or None
            _COMPARISONS = {"worse": ">", "better": "<", "lateral": "="}
            use_severity = bool(direction in _COMPARISONS and severity_map)

            def _case(column: str) -> str:
                branches = []
                for status, severity in severity_map.items():  # type: ignore[union-attr]
                    branches.append("WHEN ? THEN ?")
                    params.extend([status, int(severity)])
                params.append(_DEFAULT_SEVERITY)
                return f"CASE {column} {' '.join(branches)} ELSE ? END"

            if use_severity:
                severity_cols = (
                    f",\n                            {_case('injury_status')} AS new_severity"
                    f",\n                            {_case('previous_status')} AS prev_severity"
                )
            else:
                severity_cols = ""

            params.append(since)
            where = ["h.recorded_at >= ?"]
            if teams:
                where.append(f"h.team_id IN ({','.join('?' * len(teams))})")
                params.extend(t.upper() for t in teams)
            if player_ids:
                where.append(f"h.player_id IN ({','.join('?' * len(player_ids))})")
                params.extend(str(p) for p in player_ids)
            if direction == "new":
                where.append("h.previous_status IS NULL")
            elif use_severity:
                comparison = _COMPARISONS[direction]  # type: ignore[index]
                where.append("h.previous_status IS NOT NULL")
                where.append(f"h.new_severity {comparison} h.prev_severity")
            params.append(limit)

            with self._pool.get_connection() as conn:
                cur = conn.execute(
                    f"""
                    WITH ordered AS (
                        SELECT
                            id, player_id, team_id, injury_status, injury_type,
                            recorded_at,
                            LAG(injury_status) OVER (
                                PARTITION BY player_id, team_id
                                ORDER BY recorded_at, id
                            ) AS previous_status
                        FROM injury_history
                    ),
                    scored AS (
                        SELECT
                            id, player_id, team_id, injury_status, injury_type,
                            recorded_at, previous_status{severity_cols}
                        FROM ordered
                    )
                    SELECT
                        h.player_id, h.team_id, h.injury_status, h.injury_type,
                        h.previous_status, h.recorded_at,
                        p.player_name, p.position, p.injury_description
                    FROM scored h
                    LEFT JOIN player_injuries p
                        ON p.player_id = h.player_id AND p.team_id = h.team_id
                    WHERE {' AND '.join(where)}
                    -- `id` breaks ties: several transitions can share a
                    -- timestamp when one feed refresh moves a player twice.
                    ORDER BY h.recorded_at DESC, h.id DESC
                    LIMIT ?
                    """,
                    params,
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.debug(f"get_injury_status_changes failed: {e}")
            return []
