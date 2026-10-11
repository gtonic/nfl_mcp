"""Schema migrations v1..v21 (``schema_version`` table).

Mixed into ``nfl_mcp.database.NFLDatabase``.
"""
import sqlite3
from datetime import UTC, datetime

from ._common import (
    _DBMixinBase,
    logger,
)


class MigrationsMixin(_DBMixinBase):
    """Schema migrations v1..v21 (``schema_version`` table)."""

    def _run_migrations(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Run database migrations from the current version to the latest."""
        migrations = self._migrations()

        for version in range(from_version + 1, self.CURRENT_SCHEMA_VERSION + 1):
            if version in migrations:
                logger.info(f"Running migration to version {version}")
                migrations[version](conn)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat())
                )

    def _migrations(self) -> dict:
        """Schema version -> migration."""
        return {
            1: self._migration_v1_initial_schema,
            2: self._migration_v2_optimized_indexes,
            3: self._migration_v3_roster_snapshots,
            4: self._migration_v4_transaction_snapshots,
            5: self._migration_v5_matchup_snapshots,
            6: self._migration_v6_player_week_stats,
            7: self._migration_v7_schedule_games,
            8: self._migration_v8_practice_and_usage,
            9: self._migration_v9_injuries,
            10: self._migration_v10_defense_rankings,
            11: self._migration_v11_injuries_v2,
            12: self._migration_v12_player_values,
            13: self._migration_v13_defense_rankings_flags,
            14: self._migration_v14_real_practice_reports,
            15: self._migration_v15_projection_log_and_checks,
            16: self._migration_v16_snapshot_dedup,
            17: self._migration_v17_return_dates_freshness_signal_history,
            18: self._migration_v18_projection_accuracy,
            19: self._migration_v19_player_news,
            20: self._migration_v20_news_source_health,
            21: self._migration_v21_signal_reviews,
        }

    def _migration_v1_initial_schema(self, conn: sqlite3.Connection) -> None:
        """Migration v1: Create initial database schema."""
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

        # Create basic indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_team ON athletes(team_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_name ON athletes(full_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_abbreviation ON teams(abbreviation)")

    def _migration_v2_optimized_indexes(self, conn: sqlite3.Connection) -> None:
        """Migration v2: Add optimized indexes for better query performance."""
        # Compound indexes for common query patterns
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_team_position ON athletes(team_id, position)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_position_status ON athletes(position, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_name_search ON athletes(full_name COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_athletes_updated ON athletes(updated_at)")

        # Team search optimizations
        conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_name_search ON teams(name COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_updated ON teams(updated_at)")

        # Covering indexes for frequent lookups
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_athletes_lookup
            ON athletes(id, full_name, team_id, position, status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_teams_lookup
            ON teams(id, abbreviation, name, display_name)
        """)

    def _migration_v3_roster_snapshots(self, conn: sqlite3.Connection) -> None:
        """Migration v3: Add roster_snapshots table for robust roster fallback."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS roster_snapshots (
                id INTEGER PRIMARY KEY,
                league_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_roster_snapshots_league_time
            ON roster_snapshots (league_id, fetched_at DESC)
            """
        )

    def _migration_v4_transaction_snapshots(self, conn: sqlite3.Connection) -> None:
        """Migration v4: Add transaction_snapshots table for robust transactions fallback."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transaction_snapshots (
                id INTEGER PRIMARY KEY,
                league_id TEXT NOT NULL,
                week INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_transaction_snapshots_league_week_time
            ON transaction_snapshots (league_id, week, fetched_at DESC)
            """
        )

    def _migration_v5_matchup_snapshots(self, conn: sqlite3.Connection) -> None:
        """Migration v5: Add matchup_snapshots table for robust matchup fallback."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS matchup_snapshots (
                id INTEGER PRIMARY KEY,
                league_id TEXT NOT NULL,
                week INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_matchup_snapshots_league_week_time
            ON matchup_snapshots (league_id, week, fetched_at DESC)
            """
        )

    def _migration_v6_player_week_stats(self, conn: sqlite3.Connection) -> None:
        """Migration v6: Table for caching per-player weekly snap statistics."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_week_stats (
                player_id TEXT NOT NULL,
                season INTEGER NOT NULL,
                week INTEGER NOT NULL,
                snaps_offense INTEGER,
                snaps_team_offense INTEGER,
                snap_pct REAL,
                updated_at TEXT NOT NULL,
                raw JSON,
                PRIMARY KEY (player_id, season, week)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_pws_lookup ON player_week_stats (player_id, season, week)"""
        )

    def _migration_v7_schedule_games(self, conn: sqlite3.Connection) -> None:
        """Migration v7: Table for caching schedule to derive opponent for a team/DEF."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_games (
                season INTEGER NOT NULL,
                week INTEGER NOT NULL,
                team TEXT NOT NULL,
                opponent TEXT NOT NULL,
                is_home INTEGER NOT NULL,
                kickoff TEXT,
                raw JSON,
                PRIMARY KEY (season, week, team)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_sched_week ON schedule_games (season, week)"""
        )

    def _migration_v8_practice_and_usage(self, conn: sqlite3.Connection) -> None:
        """Migration v8: Tables for practice status (DNP/LP/FP) and usage stats (targets, routes, RZ touches)."""
        # Practice Status table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_practice_status (
                player_id TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(player_id, date)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_practice_status_date ON player_practice_status(player_id, updated_at DESC)"""
        )

        # Usage Stats table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_usage_stats (
                player_id TEXT NOT NULL,
                season INTEGER NOT NULL,
                week INTEGER NOT NULL,
                targets INTEGER,
                routes INTEGER,
                rz_touches INTEGER,
                touches INTEGER,
                air_yards REAL,
                snap_share REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(player_id, season, week)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_usage_lookup ON player_usage_stats(player_id, season, week DESC)"""
        )

    def _migration_v9_injuries(self, conn: sqlite3.Connection) -> None:
        """Migration v9: Table for player injury reports (DNP/Questionable/Out/Doubtful/etc)."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_injuries (
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                team_id TEXT NOT NULL,
                position TEXT,
                injury_status TEXT NOT NULL,
                injury_type TEXT,
                injury_description TEXT,
                date_reported TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(player_id, team_id, updated_at)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_injury_player ON player_injuries(player_id, updated_at DESC)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_injury_team ON player_injuries(team_id, updated_at DESC)"""
        )

    def _migration_v10_defense_rankings(self, conn: sqlite3.Connection) -> None:
        """Migration v10: Table for defense vs position rankings (for lineup optimization)."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS defense_rankings (
                season INTEGER NOT NULL,
                week INTEGER NOT NULL,
                team TEXT NOT NULL,
                position TEXT NOT NULL,
                rank INTEGER NOT NULL,
                points_allowed_avg REAL,
                matchup_tier TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(season, week, team, position)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_defense_rankings_lookup
               ON defense_rankings(season, week, position)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_defense_rankings_team
               ON defense_rankings(team, season, position)"""
        )

    def _migration_v11_injuries_v2(self, conn: sqlite3.Connection) -> None:
        """Migration v11: Improved injuries table with proper PK and multi-source support.

        Changes:
        - New table with (player_id, team_id) as PK (enables true upserts)
        - Added: severity (1-5 scale), confidence (0-100), sources JSON
        - Added: game_status for game-day designations (Active/Inactive/IR/PUP)
        - Migrates existing data from old table
        """
        # Create new injuries table with improved schema
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_injuries_v2 (
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                team_id TEXT NOT NULL,
                position TEXT,
                injury_status TEXT NOT NULL,
                injury_type TEXT,
                injury_description TEXT,
                game_status TEXT,
                severity INTEGER,
                confidence INTEGER DEFAULT 50,
                sources TEXT,
                date_reported TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(player_id, team_id)
            )
            """
        )

        # Create optimized indexes
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_injuries_v2_team
               ON player_injuries_v2(team_id)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_injuries_v2_status
               ON player_injuries_v2(injury_status)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_injuries_v2_updated
               ON player_injuries_v2(updated_at DESC)"""
        )

        # Migrate data from old table (most recent entry per player)
        conn.execute(
            """
            INSERT OR REPLACE INTO player_injuries_v2 (
                player_id, player_name, team_id, position,
                injury_status, injury_type, injury_description,
                game_status, severity, confidence, sources,
                date_reported, updated_at
            )
            SELECT
                player_id, player_name, team_id, position,
                injury_status, injury_type, injury_description,
                NULL, NULL, 50, '["ESPN"]',
                date_reported, MAX(updated_at)
            FROM player_injuries
            GROUP BY player_id, team_id
            """
        )

        # Drop old table and rename new one
        conn.execute("DROP TABLE IF EXISTS player_injuries")
        conn.execute("ALTER TABLE player_injuries_v2 RENAME TO player_injuries")

        # Create injury history table for trend analysis
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS injury_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id TEXT NOT NULL,
                team_id TEXT NOT NULL,
                injury_status TEXT NOT NULL,
                injury_type TEXT,
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_injury_history_player
               ON injury_history(player_id, recorded_at DESC)"""
        )

    def _migration_v13_defense_rankings_flags(self, conn: sqlite3.Connection) -> None:
        """Migration v13: carry the provisional flag, purge persisted placeholders.

        Earlier versions stored the neutral fallback table (every team rank 16)
        whenever nflverse was unreachable, and enrichment then read it back for
        a week as if it were real. A real ranking never gives every team the
        same rank, so those groups are deleted outright.
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(defense_rankings)")}
        if "is_provisional" not in cols:
            conn.execute(
                "ALTER TABLE defense_rankings ADD COLUMN is_provisional INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            DELETE FROM defense_rankings
            WHERE (season, week, position) IN (
                SELECT season, week, position FROM defense_rankings
                GROUP BY season, week, position
                HAVING MIN(rank) = MAX(rank)
            )
            """
        )

    def _migration_v15_projection_log_and_checks(self, conn: sqlite3.Connection) -> None:
        """Migration v15: pre-kickoff projection log and per-league last-check times.

        ``projection_log`` is append-only and written only before a player's
        kickoff, one row per change: the first row of a week is the projection
        the lineup was set against (what a retro grades), later rows are what
        moved since (what "what changed" reports). Keyed by scoring (``ppr``)
        (``scoring_key``, a fingerprint of the league's full scoring weights)
        rather than by league, because the same player in the same week
        projects the same in every league that scores exactly the same way —
        and differently in two that merely share a reception value.

        ``league_checks`` remembers when a roster's changes were last read.
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season INTEGER NOT NULL,
                week INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                scoring_key TEXT NOT NULL,
                ppr REAL,
                projected_points REAL NOT NULL,
                floor REAL,
                ceiling REAL,
                player_name TEXT,
                position TEXT,
                team TEXT,
                league_id TEXT,
                source TEXT,
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_projection_log_key
               ON projection_log(season, week, scoring_key, player_id, recorded_at)"""
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS league_checks (
                league_id TEXT NOT NULL,
                roster_id INTEGER NOT NULL,
                last_checked_at TEXT NOT NULL,
                PRIMARY KEY (league_id, roster_id)
            )
            """
        )

    # Snapshot tables and the columns identifying one logical snapshot.
    _SNAPSHOT_KEYS = {
        "roster_snapshots": ("league_id",),
        "matchup_snapshots": ("league_id", "week"),
        "transaction_snapshots": ("league_id", "week"),
    }

    def _migration_v16_snapshot_dedup(self, conn: sqlite3.Connection) -> None:
        """Migration v16: payload hashes on snapshots + one-time prune.

        Every roster/matchup call appended a ~177 KB row although readers only
        ever load the newest one per key (~118 MB of a 152 MB DB). Saves now
        skip identical payloads (``payload_hash``) and keep a few rows per
        key; this prunes the backlog down to the newest row per key.
        """
        for table, keys in self._SNAPSHOT_KEYS.items():
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "payload_hash" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN payload_hash TEXT")
            partition = ", ".join(keys)
            deleted = conn.execute(
                f"""
                DELETE FROM {table} WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY {partition} ORDER BY fetched_at DESC, id DESC
                        ) AS rn FROM {table}
                    ) WHERE rn = 1
                )
                """
            ).rowcount
            if deleted:
                logger.info(f"[Migration v16] Pruned {deleted} superseded rows from {table}")
                self._vacuum_after_migration = True

    def _migration_v17_return_dates_freshness_signal_history(self, conn: sqlite3.Connection) -> None:
        """Migration v17: ESPN return dates, schedule freshness, signal history.

        - ``player_injuries.return_date`` / ``injury_history.return_date``:
          ESPN's estimated return (``details.returnDate``), which the crawl
          used to drop, so ROS priced every absence from the designation alone.
        - ``schedule_games.updated_at``: the schedule had no timestamp, so its
          age could not be reported. Existing rows stay NULL ("unknown") until
          the next schedule fetch rewrites them.
        - ``practice_report_history`` / ``injury_news_history``: append-only,
          deduplicated on content. The live tables keep one practice row per
          player and day and one blurb per player, so the practice and news
          weights had nothing to be backtested against.
        """
        for table, column in (("player_injuries", "return_date"),
                              ("injury_history", "return_date"),
                              ("schedule_games", "updated_at")):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS practice_report_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_key TEXT NOT NULL DEFAULT '',
                team TEXT NOT NULL DEFAULT '',
                player_id TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                player_name TEXT,
                game_status TEXT,
                season INTEGER,
                week INTEGER,
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_practice_history_content
               ON practice_report_history(name_key, team, player_id, date, status, source)"""
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS injury_news_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT NOT NULL UNIQUE,
                player_id TEXT NOT NULL,
                team_id TEXT,
                player_name TEXT,
                injury_status TEXT,
                text TEXT NOT NULL,
                date_reported TEXT,
                return_date TEXT,
                source TEXT,
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_injury_news_history_player
               ON injury_news_history(player_id, recorded_at)"""
        )
        self._repair_practice_weeks(conn)
        # Seed the history with what the live tables hold now.
        conn.execute(
            """
            INSERT OR IGNORE INTO practice_report_history(
                name_key, team, player_id, date, status, source,
                player_name, game_status, season, week, recorded_at)
            SELECT name_key, team, player_id, date, status, COALESCE(source, ''),
                   player_name, game_status, season, week, updated_at
            FROM player_practice_status
            """
        )
        news = [self._news_history_row(dict(row), row["sources"] or "[]", row["updated_at"])
                for row in conn.execute("SELECT * FROM player_injuries")]
        conn.executemany(
            """
            INSERT OR IGNORE INTO injury_news_history(
                content_hash, player_id, team_id, player_name, injury_status,
                text, date_reported, return_date, source, recorded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [r for r in news if r],
        )

    def _migration_v18_projection_accuracy(self, conn: sqlite3.Connection) -> None:
        """Migration v18: the weekly accuracy loop (``projection_accuracy``).

        - ``projection_log.signals``: the signals active when a projection was
          logged (role trend, returning teammates, practice pattern, QB
          coupling, news flags, injury status, projection source, our and
          Sleeper's numbers), as JSON. Rows logged before v18 have none.
        - ``projection_accuracy``: one graded row per (week, scoring, player):
          the last pre-kickoff projection against the actual points in that
          scoring and in neutral half-PPR, with the signals unpacked into
          columns so they can be grouped on.
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(projection_log)")}
        if "signals" not in cols:
            conn.execute("ALTER TABLE projection_log ADD COLUMN signals TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projection_accuracy (
                season INTEGER NOT NULL,
                week INTEGER NOT NULL,
                scoring_key TEXT NOT NULL,
                player_id TEXT NOT NULL,
                league_id TEXT,
                player_name TEXT,
                position TEXT,
                team TEXT,
                projected REAL NOT NULL,
                model_projection REAL,
                sleeper_projection REAL,
                floor REAL,
                ceiling REAL,
                actual REAL NOT NULL,
                actual_half_ppr REAL,
                actual_source TEXT,
                played INTEGER,
                projection_source TEXT,
                injury_status TEXT,
                practice_status TEXT,
                practice_pattern TEXT,
                role_trend TEXT,
                returning_teammates INTEGER,
                qb_mult REAL,
                news_flags TEXT,
                signals TEXT,
                log_source TEXT,
                projected_at TEXT,
                graded_at TEXT NOT NULL,
                PRIMARY KEY (season, week, scoring_key, player_id)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_projection_accuracy_week
               ON projection_accuracy(season, week, position)"""
        )

    def _migration_v19_player_news(self, conn: sqlite3.Connection) -> None:
        """Migration v19: per-player news from several sources (``player_news``).

        ``injury_news_history`` keeps the one ESPN injury blurb per player as
        it changes; the role and availability notes in between ("benched after
        his fumble", "only an outside chance to play") were overwritten by the
        next practice note before anything read them. ``player_news`` keeps
        every item of every news source (`news_sources`: ESPN's fantasy
        player feed, NBC Sports / Rotoworld, CBS), mapped to the Sleeper id,
        deduplicated per source on content. ``news_fetch_state`` records each
        source's last fetch (freshness, incremental paging).
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                source_id TEXT,
                player_id TEXT NOT NULL,
                espn_id TEXT,
                player_name TEXT,
                name_key TEXT NOT NULL DEFAULT '',
                team TEXT NOT NULL DEFAULT '',
                position TEXT,
                headline TEXT,
                text TEXT NOT NULL,
                url TEXT,
                published_at TEXT,
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_player_news_player
               ON player_news(player_id, published_at)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_player_news_published
               ON player_news(published_at)"""
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news_fetch_state (
                source TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                newest_published TEXT,
                items INTEGER,
                written INTEGER,
                status TEXT,
                error TEXT
            )
            """
        )

    # Per-source health of the news fetch (`news_sources.assess`).
    NEWS_HEALTH_COLUMNS = (
        ("health", "TEXT"),                 # ok | degraded | failing
        ("detail", "TEXT"),                 # why it is not ok
        ("parsed_items", "INTEGER"),        # items the parser read from the first page
        ("expected_items", "INTEGER"),      # items a page normally has
        ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("last_success_at", "TEXT"),
        ("last_error_at", "TEXT"),
        ("last_error", "TEXT"),
        ("next_attempt_at", "TEXT"),        # backoff: no fetch before this
    )

    def _migration_v20_news_source_health(self, conn: sqlite3.Connection) -> None:
        """Migration v20: per-source health in ``news_fetch_state``.

        NBC and CBS are HTML scrapes: a markup change parses to zero items
        from a 200 response and looked like a quiet news day. Each fetch now
        records its health (ok / degraded / failing, with the reason), the
        items parsed against the items a page has, the last success and
        error, and the failure streak with the time before which the source
        is not fetched again (exponential backoff).
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news_fetch_state (
                source TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                newest_published TEXT,
                items INTEGER,
                written INTEGER,
                status TEXT,
                error TEXT
            )
            """
        )
        have = {r[1] for r in conn.execute("PRAGMA table_info(news_fetch_state)")}
        for column, decl in self.NEWS_HEALTH_COLUMNS:
            if column not in have:
                conn.execute(f"ALTER TABLE news_fetch_state ADD COLUMN {column} {decl}")
        conn.execute(
            """UPDATE news_fetch_state SET last_success_at = fetched_at
               WHERE status = 'ok' AND last_success_at IS NULL"""
        )

    def _migration_v21_signal_reviews(self, conn: sqlite3.Connection) -> None:
        """Migration v21: the weekly signal review, stored (``signal_reviews``).

        The prefetch's ``accuracy`` scope runs the review (`signal_review`)
        right after it grades a week and keeps the JSON summary here, one row
        per (season, week, scope), so reading it back is a single lookup
        rather than a bootstrap over every graded row.
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_reviews (
                season INTEGER NOT NULL,
                week INTEGER NOT NULL,
                scope TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                rows_graded INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY (season, week, scope)
            )
            """
        )

    # Kickoffs are stored in UTC; shifted by the EST offset every kickoff
    # (13:00 ET to 20:30 ET) lands on its US Eastern date.
    _KICKOFF_TO_ET_DATE = "date(s.kickoff, '-5 hours')"

    def _repair_practice_weeks(self, conn: sqlite3.Connection) -> None:
        """Give NFL.com practice rows back the week of the game they report.

        On Monday and Tuesday the next week's page still shows the last
        week's final report, and the fetch stored it under the new week --
        overwriting the week of the existing rows (618 of them in the live
        database). The game date each row carries says which week it was.
        """
        match = f"""
            FROM schedule_games s
            WHERE s.season = player_practice_status.season
              AND s.team = player_practice_status.team
              AND {self._KICKOFF_TO_ET_DATE} = player_practice_status.game_date
        """
        fixed = conn.execute(
            f"""
            UPDATE player_practice_status SET week = (SELECT s.week {match})
            WHERE source = 'nfl.com' AND game_date IS NOT NULL
              AND EXISTS (SELECT 1 {match} AND s.week != player_practice_status.week)
            """
        ).rowcount
        if fixed:
            logger.info(f"[Migration v17] Moved {fixed} practice rows back to their game's week")

    def _migration_v12_player_values(self, conn: sqlite3.Connection) -> None:
        """Migration v12: Consensus player market values (FantasyCalc) for trades & drafts.

        Stores format-aware player values keyed by (format_key, player_id) where
        player_id is the Sleeper player id so roster/draft data can be joined directly.
        format_key encodes scoring/roster settings so multiple league formats coexist.
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_values (
                format_key TEXT NOT NULL,
                player_id TEXT NOT NULL,
                name TEXT,
                position TEXT,
                team TEXT,
                value INTEGER,
                redraft_value INTEGER,
                overall_rank INTEGER,
                position_rank INTEGER,
                tier INTEGER,
                trend_30day INTEGER,
                source TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(format_key, player_id)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_player_values_rank
               ON player_values(format_key, overall_rank ASC)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_player_values_pos
               ON player_values(format_key, position, position_rank ASC)"""
        )

    def _migration_v14_real_practice_reports(self, conn: sqlite3.Connection) -> None:
        """Migration v14: practice rows from real reports, one per player and day.

        Every row in the v8 table was invented from an injury designation
        (Questionable -> LP, Out -> DNP) and keyed by ESPN athlete id, so it is
        dropped rather than migrated: keeping those rows would put the made-up
        lines back into the weekly pattern. Rows are now keyed by (normalized
        name, team, day) — the join the other injury tables use, because the
        official report carries names, not ids — with the report text, the game
        designation and a source rank so a news blurb never overwrites the
        official report for the same day. ``player_id`` stays as an optional
        key column for rows written the pre-v14 way.
        """
        conn.execute("DROP TABLE IF EXISTS player_practice_status")
        conn.execute(
            """
            CREATE TABLE player_practice_status (
                name_key TEXT NOT NULL DEFAULT '',
                team TEXT NOT NULL DEFAULT '',
                player_id TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL,
                player_name TEXT,
                position TEXT,
                status TEXT NOT NULL,
                description TEXT,
                injury TEXT,
                game_status TEXT,
                game_date TEXT,
                season INTEGER,
                week INTEGER,
                estimated INTEGER NOT NULL DEFAULT 0,
                source TEXT,
                source_rank INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(name_key, team, player_id, date)
            )
            """
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_practice_status_date
               ON player_practice_status(player_id, updated_at DESC)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_practice_status_week
               ON player_practice_status(season, week, team)"""
        )
