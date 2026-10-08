"""Data-layer follow-ups: ESPN return dates, one refresh path for the prefetch,
schedule/snaps freshness, practice snapshot dating, signal history.

Offline: ESPN, NFL.com and Sleeper are mocked; databases are temp files.
"""
import asyncio
import sqlite3
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nfl_mcp import data_refresh, ros, server
from nfl_mcp import practice_reports as pr
from nfl_mcp.database import CLEARED_REPORT_TEXT, NFLDatabase, season_start
from nfl_mcp.injury_match import build_injury_index
from nfl_mcp.injury_service import InjuryAggregator, parse_return_date


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield NFLDatabase(str(Path(tmp) / "t.db"))


def _inj(pid, name, team, status="Out", **kw):
    return {"player_id": pid, "player_name": name, "team_id": team, "position": "WR",
            "injury_status": status, "sources": ["ESPN"], **kw}


def _et(y, m, d, h, minute=0):
    """A UTC instant for an Eastern (EDT) wall-clock time."""
    return datetime(y, m, d, h + 4, minute, tzinfo=UTC)


# 1 ---------------------------------------------------------------------------
class TestReturnDate:
    @pytest.mark.parametrize("item,expected", [
        ({"details": {"returnDate": "2026-10-11"}}, "2026-10-11"),
        ({"details": {"returnDate": "2026-10-11T00:00Z"}}, "2026-10-11"),
        ({"returnDate": "2026-11-02"}, "2026-11-02"),
        ({"details": {"returnDate": "soon"}}, None),
        ({"details": {}}, None),
        ({}, None),
        (None, None),
    ])
    def test_parse(self, item, expected):
        assert parse_return_date(item) == expected

    @pytest.mark.asyncio
    async def test_the_crawl_keeps_it(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "status": "Injured Reserve", "date": "2026-10-03T20:02Z",
            "athlete": {"$ref": "https://sports.core.api.espn.com/v2/athletes/4430878?lang=en",
                        "displayName": "Jadarian Price"},
            "details": {"type": "Chest", "returnDate": "2026-11-02",
                        "fantasyStatus": {"abbreviation": "IR"}},
        }
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        InjuryAggregator.clear_caches()
        try:
            rep = await InjuryAggregator(http_client=client)._fetch_espn_injury_detail(
                "https://sports.core.api.espn.com/v2/injuries/1", {})
        finally:
            InjuryAggregator.clear_caches()
        assert rep.return_date == "2026-11-02"
        assert rep.to_dict()["return_date"] == "2026-11-02"

    def test_stored_and_read_back(self, db):
        db.upsert_injuries([_inj("1", "Jadarian Price", "SEA", "IR", return_date="2026-11-02")])
        rows = db.get_all_current_injuries()
        assert rows[0]["return_date"] == "2026-11-02" and "date_reported" in rows[0]
        assert db.get_team_injuries_from_cache("SEA", 1)[0]["return_date"] == "2026-11-02"
        assert db.get_injury_history("1")[0]["return_date"] == "2026-11-02"
        # A report a complete crawl no longer lists loses its return date.
        db.upsert_injuries([], prune_missing=True, complete_teams={"SEA"})
        assert db.get_all_current_injuries()[0]["return_date"] is None

    def test_reaches_the_ros_absence(self, db):
        """End to end: stored report -> injury index -> ROS input -> weeks out."""
        db.upsert_injuries([_inj("9", "Caleb Williams", "CHI", "Out", return_date="2026-10-18")])
        index = build_injury_index(db.get_all_current_injuries())
        row = {"id": "s1", "full_name": "Caleb Williams", "team_id": "CHI", "position": "QB",
               "injury_status": "Out"}
        inp = ros.ros_input(row, index)
        assert inp["injury"]["return_date"] == "2026-10-18"
        weeks, reason = ros.expected_absence(inp["injury"]["status"] or "Out",
                                             return_date=inp["injury"]["return_date"],
                                             today=date(2026, 10, 8))
        assert weeks == 2 and "return date" in reason

    def test_pup_since_preseason_has_served_the_minimum(self):
        """Zach Charbonnet, week 6: on PUP since camp, no placement recorded,
        ESPN return 10-15. The minimum is served by week 5."""
        today = date(2026, 10, 8)
        assert ros.expected_absence("PUP", "PUP-R", "2026-10-15", today)[0] == ros.IR_MIN_WEEKS
        weeks, reason = ros.expected_absence("PUP", "PUP-R", "2026-10-15", today, season_week=6)
        assert weeks == 1 and "return date" in reason
        weeks, reason = ros.expected_absence("PUP", "designated to return", None, today,
                                             season_week=6)
        assert weeks == 1 and "before week 1" in reason
        # Early in the season the rest of the minimum still holds.
        assert ros.expected_absence("PUP", None, "2026-09-14", date(2026, 9, 10),
                                    season_week=2)[0] == 3
        # IR is placed in season: the season week says nothing.
        assert ros.expected_absence("IR", None, None, today, season_week=6)[0] == ros.IR_MIN_WEEKS

    def test_projections_absence_detail_carries_it(self):
        from nfl_mcp.projections import _report_absence
        got = _report_absence({"injury_description": "hamstring", "return_date": "2026-10-18",
                               "injury_status": "Out"}, None)
        assert got["return_date"] == "2026-10-18"


class TestMigration:
    def test_v16_database_is_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            NFLDatabase(path).close()
            with sqlite3.connect(path) as conn:
                # Back to v16: the v17 columns and tables gone.
                conn.execute("DELETE FROM schema_version WHERE version >= 17")
                conn.execute("ALTER TABLE player_injuries DROP COLUMN return_date")
                conn.execute("ALTER TABLE injury_history DROP COLUMN return_date")
                conn.execute("ALTER TABLE schedule_games DROP COLUMN updated_at")
                conn.execute("DROP TABLE practice_report_history")
                conn.execute("DROP TABLE injury_news_history")
                conn.execute(
                    "INSERT INTO player_injuries(player_id, player_name, team_id, injury_status,"
                    " injury_description, date_reported, sources, updated_at) VALUES"
                    " ('1','A B','KC','Out','A (knee) did not practice.','2026-10-01T10:00Z',"
                    " '[\"ESPN\"]','2026-10-01T12:00:00+00:00'),"
                    " ('2','C D','KC','Active',?, NULL, '[\"ESPN\"]','2026-10-01T12:00:00+00:00')",
                    (CLEARED_REPORT_TEXT,))
                conn.execute(
                    "INSERT INTO schedule_games(season, week, team, opponent, is_home, kickoff)"
                    " VALUES (2026, 4, 'LV', 'KC', 0, '2026-10-04T20:25Z'),"
                    " (2026, 5, 'LV', 'NE', 0, '2026-10-11T17:00Z')")
                # Week 4's Friday report, re-stored under week 5 by the
                # Tuesday fetch of the week-5 page.
                conn.execute(
                    "INSERT INTO player_practice_status(name_key, team, date, status, game_date,"
                    " season, week, source, source_rank, updated_at) VALUES"
                    " ('brock bowers','LV','2026-10-02','LP','2026-10-04',2026,5,'nfl.com',2,"
                    " '2026-10-06T18:54:32+00:00')")
                conn.commit()
            NFLDatabase(path).close()
            with sqlite3.connect(path) as conn:
                assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
                    == NFLDatabase.CURRENT_SCHEMA_VERSION
                for table, col in (("player_injuries", "return_date"),
                                   ("injury_history", "return_date"),
                                   ("schedule_games", "updated_at")):
                    assert col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                assert conn.execute("SELECT week FROM player_practice_status").fetchone()[0] == 4
                # Seeded from the live tables (the cleared text is not news).
                assert conn.execute("SELECT week, status FROM practice_report_history"
                                    ).fetchall() == [(4, "LP")]
                assert conn.execute("SELECT player_id, text FROM injury_news_history"
                                    ).fetchall() == [("1", "A (knee) did not practice.")]


# 2 ---------------------------------------------------------------------------
class TestPrefetchSharesRefresh:
    def test_cycle_scopes_keep_the_cadence(self):
        assert server._cycle_scopes(5, 2) == ["schedule", "snaps", "injuries", "practice", "usage",
                                             "accuracy"]
        # No practice reports on Sunday (ET); no completed week before week 2.
        assert server._cycle_scopes(1, 6) == ["schedule", "snaps", "injuries"]

    @pytest.mark.asyncio
    async def test_the_loop_runs_the_refresh_scopes(self, monkeypatch):
        shutdown = asyncio.Event()
        calls = []

        async def _scope(scope, db, season, week, owner=data_refresh.PREFETCH_OWNER):
            calls.append((scope, season, week))
            if scope == "injuries":
                shutdown.set()
            return {"status": "ok", "fetched": 1, "written": 1}

        monkeypatch.setattr(server, "PREFETCH_ENABLED", True)
        monkeypatch.setattr(server, "PREFETCH_ATHLETES", False)
        monkeypatch.setattr("nfl_mcp.sleeper_enrichment.ADVANCED_ENRICH_ENABLED", True)
        monkeypatch.setattr("nfl_mcp.sleeper_tools.get_nfl_state", AsyncMock(
            return_value={"success": True, "nfl_state": {"season": "2026", "week": "5"}}))
        monkeypatch.setattr(data_refresh, "run_scope", _scope)
        monkeypatch.setattr(server, "_prune_db_if_due", AsyncMock())
        await asyncio.wait_for(server._prefetch_loop(MagicMock(), shutdown), timeout=5)
        scopes = [c[0] for c in calls]
        assert scopes[:3] == ["schedule", "snaps", "injuries"]
        assert all(c[1:] == (2026, 5) for c in calls)

    @pytest.mark.asyncio
    async def test_a_scope_is_not_run_twice_at_once(self, monkeypatch):
        monkeypatch.setattr(data_refresh, "_scope_owner", {"injuries": "job123"})
        out = await data_refresh.run_scope("injuries", MagicMock(), 2026, 5)
        assert out == {"status": "already_running", "job_id": "job123"}

    @pytest.mark.asyncio
    async def test_the_loop_holds_the_scope_while_running(self, monkeypatch):
        monkeypatch.setattr(data_refresh, "_scope_owner", {})
        seen = {}

        async def _usage(db, season, week):
            seen["owner"] = dict(data_refresh._scope_owner)
            return {"fetched": 0, "written": 0}
        monkeypatch.setattr(data_refresh, "_REFRESHERS", {**data_refresh._REFRESHERS, "usage": _usage})
        out = await data_refresh.run_scope("usage", MagicMock(), 2026, 5)
        assert out["status"] == "ok"
        assert seen["owner"] == {"usage": data_refresh.PREFETCH_OWNER}
        assert data_refresh._scope_owner == {}

    @pytest.mark.asyncio
    async def test_usage_scope_reads_the_last_completed_week(self, monkeypatch):
        fetched = []

        async def _fetch(season, week, force=False):
            fetched.append((season, week, force))
            return [{"player_id": "1", "season": season, "week": week, "targets": 5}]
        monkeypatch.setattr("nfl_mcp.sleeper_tools._fetch_weekly_usage_stats", _fetch)
        db = MagicMock()
        db.upsert_usage_stats.return_value = 1
        out = await data_refresh._refresh_usage(db, 2026, 5)
        assert fetched == [(2026, 4, True)] and out["written"] == 1
        assert (await data_refresh._refresh_usage(db, 2026, 1))["weeks"] == []


# 3 ---------------------------------------------------------------------------
class TestScheduleAndSnapsFreshness:
    def test_reported(self, db):
        out = db.get_data_freshness()
        assert out["schedule"]["age_hours"] is None and out["snaps"]["age_hours"] is None
        db.upsert_schedule_games([{"season": 2026, "week": 5, "team": "LV", "opponent": "NE",
                                   "is_home": 0, "kickoff": "2026-10-11T17:00Z"}])
        db.upsert_player_week_stats([{"player_id": "1", "season": 2026, "week": 4,
                                      "snaps_offense": 50, "snaps_team_offense": 60}])
        out = db.get_data_freshness()
        assert out["schedule"]["age_hours"] < 1 and out["snaps"]["age_hours"] < 1

    def test_health_check_carries_freshness(self, db):
        health = db.health_check()
        assert set(health["data_freshness"]) >= {"injuries", "athletes", "practice_status",
                                                 "schedule", "snaps"}

    def test_refresh_reads_schedule_and_snaps_ages(self):
        assert data_refresh._FRESHNESS_FEED["schedule"] == "schedule"
        assert data_refresh._FRESHNESS_FEED["snaps"] == "snaps"


# 4 ---------------------------------------------------------------------------
def _page(team, game_date, statuses):
    return [{"team": team, "game_date": game_date, "player_name": name, "position": "TE",
             "injury": "Knee", "practice_description": s, "practice_status": s,
             "game_status": None} for name, s in statuses.items()]


class TestPracticeSnapshotDating:
    # LV played Sunday 2026-09-27; reports Wed 09-23 .. Fri 09-25.
    GAME = "2026-09-27"
    GAME_DATES = {"LV": date(2026, 9, 27)}

    def test_early_publication_does_not_overwrite_yesterday(self, db):
        """Brock Bowers, 2026-09-26: Friday's report, published before the
        16:00 ET cutoff, replaced Thursday's LP with Friday's DNP."""
        thursday = pr.nfl_com_reports(_page("LV", self.GAME, {"Brock Bowers": "LP"}),
                                      2026, 3, now=_et(2026, 9, 24, 17),
                                      stored={}, game_dates=self.GAME_DATES)
        assert [r["date"] for r in thursday] == ["2026-09-24"]
        db.upsert_practice_status(thursday)

        friday = pr.nfl_com_reports(_page("LV", self.GAME, {"Brock Bowers": "DNP"}),
                                    2026, 3, now=_et(2026, 9, 25, 15, 26),
                                    stored=db.get_team_practice_snapshots(2026, 3, "nfl.com"),
                                    game_dates=self.GAME_DATES)
        assert [(r["date"], r["status"]) for r in friday] == [("2026-09-25", "DNP")]
        db.upsert_practice_status(friday)
        rows = db.get_practice_reports("Brock Bowers", "LV", season=2026, week=3)
        assert [(r["date"], r["status"]) for r in rows] == [("2026-09-24", "LP"),
                                                            ("2026-09-25", "DNP")]

    def test_yesterdays_report_still_up_keeps_yesterdays_date(self, db):
        db.upsert_practice_status(pr.nfl_com_reports(
            _page("LV", self.GAME, {"Brock Bowers": "LP"}), 2026, 3,
            now=_et(2026, 9, 24, 17), game_dates=self.GAME_DATES))
        out = pr.nfl_com_reports(_page("LV", self.GAME, {"Brock Bowers": "LP"}), 2026, 3,
                                 now=_et(2026, 9, 25, 10),
                                 stored=db.get_team_practice_snapshots(2026, 3, "nfl.com"),
                                 game_dates=self.GAME_DATES)
        assert [r["date"] for r in out] == ["2026-09-24"]

    def test_first_report_of_the_week_published_early(self):
        out = pr.nfl_com_reports(_page("LV", self.GAME, {"Brock Bowers": "DNP"}), 2026, 3,
                                 now=_et(2026, 9, 23, 14), stored={},
                                 game_dates=self.GAME_DATES)
        assert [r["date"] for r in out] == ["2026-09-23"]

    def test_saturday_snapshot_still_lands_on_the_final_report(self, db):
        out = pr.nfl_com_reports(_page("LV", self.GAME, {"Brock Bowers": "DNP"}), 2026, 3,
                                 now=_et(2026, 9, 26, 10),
                                 stored={"LV": {"2026-09-25": {"brock bowers": "LP"}}},
                                 game_dates=self.GAME_DATES)
        assert [r["date"] for r in out] == ["2026-09-25"]

    def test_next_weeks_page_showing_last_weeks_game_is_skipped(self):
        """Tuesday 09-29: the week-4 page still shows week 3's final report."""
        page = _page("LV", self.GAME, {"Brock Bowers": "DNP"})
        now = _et(2026, 9, 29, 15)
        assert pr.nfl_com_reports(page, 2026, 4, now=now,
                                  game_dates={"LV": date(2026, 10, 4)}) == []
        # Without a schedule: a game already played is not this week's report.
        assert pr.nfl_com_reports(page, 2026, 4, now=now) == []

    @pytest.mark.asyncio
    async def test_fetch_passes_the_stored_days_and_game_dates(self, db):
        db.upsert_schedule_games([{"season": 2026, "week": 3, "team": "LV", "opponent": "NO",
                                   "is_home": 0, "kickoff": "2026-09-27T20:25Z"}])
        db.upsert_practice_status([{"player_name": "Brock Bowers", "team": "LV",
                                    "date": "2026-09-24", "status": "LP", "season": 2026,
                                    "week": 3, "source": "nfl.com"}])
        html = """
        <h2 class="d3-o-section-title">SUNDAY, SEPTEMBER 27TH</h2>
        <div class="d3-o-section-sub-title"><span>Raiders</span></div>
        <table class="d3-o-reports--detailed"><tbody>
        <tr><td><a> Brock Bowers </a></td><td>TE</td><td>Knee</td>
        <td>Did Not Participate In Practice</td><td></td></tr></tbody></table>"""
        nfl = MagicMock(status_code=200, text=html)
        espn = MagicMock(status_code=500)
        client = MagicMock()
        client.get = AsyncMock(side_effect=[nfl, espn])
        rows = await pr.fetch_practice_reports(2026, 3, db=db, client=client,
                                               now=_et(2026, 9, 25, 15, 26))
        assert [(r["date"], r["status"]) for r in rows] == [("2026-09-25", "DNP")]


# 5 ---------------------------------------------------------------------------
class TestSignalHistory:
    def test_every_distinct_practice_row_is_kept(self, db):
        base = {"player_name": "Brock Bowers", "team": "LV", "date": "2026-09-24",
                "season": 2026, "week": 3}
        db.upsert_practice_status([{**base, "status": "LP", "source": "nfl.com"}])
        db.upsert_practice_status([{**base, "status": "LP", "source": "nfl.com"}])  # dup
        db.upsert_practice_status([{**base, "status": "DNP", "source": "nfl.com"}])
        # Refused by the live table (lower rank), kept in the history.
        assert db.upsert_practice_status([{**base, "status": "FP", "source": "espn_news"}]) == 0
        with sqlite3.connect(db.db_path) as conn:
            got = conn.execute("SELECT date, status, source FROM practice_report_history"
                               " ORDER BY id").fetchall()
        assert got == [("2026-09-24", "LP", "nfl.com"), ("2026-09-24", "DNP", "nfl.com"),
                       ("2026-09-24", "FP", "espn_news")]

    def test_every_distinct_blurb_is_kept(self, db):
        a = _inj("1", "Brock Bowers", "LV", "Questionable",
                 injury_description="Bowers (knee) was limited Wednesday.",
                 date_reported="2026-10-07T21:54Z", return_date="2026-10-11")
        db.upsert_injuries([a])
        db.upsert_injuries([a])  # the same blurb on the next crawl
        db.upsert_injuries([{**a, "injury_description": "Bowers (knee) did not practice Thursday.",
                             "date_reported": "2026-10-08T21:00Z"}])
        db.upsert_injuries([], prune_missing=True, complete_teams={"LV"})  # cleared: not news
        with sqlite3.connect(db.db_path) as conn:
            got = conn.execute("SELECT text, date_reported, return_date, source"
                               " FROM injury_news_history ORDER BY id").fetchall()
        assert got == [
            ("Bowers (knee) was limited Wednesday.", "2026-10-07T21:54Z", "2026-10-11", '["ESPN"]'),
            ("Bowers (knee) did not practice Thursday.", "2026-10-08T21:00Z", "2026-10-11",
             '["ESPN"]'),
        ]

    def test_season_start(self):
        assert season_start(datetime(2026, 10, 8, tzinfo=UTC)) == datetime(2026, 7, 1, tzinfo=UTC)
        assert season_start(datetime(2027, 1, 20, tzinfo=UTC)) == datetime(2026, 7, 1, tzinfo=UTC)

    def test_prune_keeps_the_current_season(self, db):
        now = datetime.now(UTC)
        old = (now - timedelta(days=400)).isoformat()
        this_season = max(season_start(now), now - timedelta(days=200)).isoformat()
        with sqlite3.connect(db.db_path) as conn:
            for i, at in enumerate((old, this_season)):
                conn.execute("INSERT INTO practice_report_history(name_key, team, date, status,"
                             " source, recorded_at) VALUES ('a','LV',?, 'LP','nfl.com',?)",
                             (f"2025-09-2{i}", at))
                conn.execute("INSERT INTO injury_news_history(content_hash, player_id, text,"
                             " recorded_at) VALUES (?, '1', 'x', ?)", (f"h{i}", at))
            conn.commit()
        deleted = db.prune_old_data()
        assert deleted["practice_report_history"] == 1
        assert deleted["injury_news_history"] == 1
        with sqlite3.connect(db.db_path) as conn:
            assert conn.execute("SELECT recorded_at FROM practice_report_history"
                                ).fetchall() == [(this_season,)]


class TestSignalBacktestStub:
    def test_practice_is_rebuilt_as_known_at_kickoff(self):
        from evals.backtest import signal_history as sh
        ko = datetime(2026, 9, 27, 20, 25, tzinfo=UTC)
        rows = [
            {"week": 3, "team": "LV", "name_key": "brock bowers", "date": "2026-09-24",
             "status": "LP", "source": "nfl.com", "game_status": None,
             "recorded_at": "2026-09-24T21:00:00+00:00"},
            {"week": 3, "team": "LV", "name_key": "brock bowers", "date": "2026-09-25",
             "status": "DNP", "source": "nfl.com", "game_status": "Questionable",
             "recorded_at": "2026-09-25T21:00:00+00:00"},
            # After kickoff: unknown at decision time.
            {"week": 3, "team": "LV", "name_key": "brock bowers", "date": "2026-09-25",
             "status": "FP", "source": "nfl.com", "game_status": "Questionable",
             "recorded_at": "2026-09-28T12:00:00+00:00"},
        ]
        got = sh.practice_at_kickoff(rows, {(3, "LV"): ko})
        assert got[(3, "LV", "brock bowers")] == {"pattern": "LP-DNP", "latest": "DNP",
                                                  "game_status": "Questionable"}
        buckets = sh.practice_buckets(got, {(3, "LV", "brock bowers"): 0.6})
        assert buckets == {"mult=0.75": [0.6]}

    def test_ratios_score_a_missed_game_as_zero(self):
        from evals.backtest import signal_history as sh
        recs = [{"player": "Brock Bowers", "team": "LV", "week": w, "ppr": 10.0} for w in (1, 2, 4)]
        got = sh.ratios(recs)
        assert got[(3, "LV", "brock bowers")] == 0.0
        assert got[(4, "LV", "brock bowers")] == 1.0
