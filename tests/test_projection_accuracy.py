"""The weekly accuracy loop: signals logged, weeks graded, report built (offline)."""
import json
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nfl_mcp import data_refresh, server
from nfl_mcp import projection_accuracy as pa
from nfl_mcp.database import NFLDatabase
from nfl_mcp.projection_store import log_projections, signals_of
from nfl_mcp.scoring import league_scoring

LEAGUE = {"league_id": "L1", "name": "Test", "scoring_settings": {
    "pass_yd": 0.04, "pass_td": 4, "rush_yd": 0.1, "rush_td": 6, "rec_yd": 0.1,
    "rec_td": 6, "rec": 0.5}}


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        d = NFLDatabase(str(Path(tmp) / "acc.db"))
        yield d
        d.close()


def _proj(name, pid, points, position="WR", **kw):
    return {"player": name, "player_id": pid, "team": "BUF", "position": position,
            "projected_points": points, "floor": points * 0.6, "ceiling": points * 1.4, **kw}


class TestSignals:
    def test_signals_of_reads_the_projection(self):
        sig = signals_of({
            "projection_source": "sleeper_blend", "model_projection": 8.0,
            "sleeper_projection": 12.0, "injury_status": "Questionable",
            "practice_status": "DNP", "practice_pattern": "DNP-DNP-LP",
            "role_trend": "role_down", "news_flags": [{"flag": "week_to_week", "weight": 1}],
            "qb_context": {"applied": True, "model_mult": 0.85, "sleeper_mult": 0.8},
            "breakdown": {"returning_teammates": [{"name": "X"}], "inherited_from": {"Y": {}}},
            "matchup_tier": "tough",
        })
        assert sig == {
            "projection_source": "sleeper_blend", "model_projection": 8.0,
            "sleeper_projection": 12.0, "injury_status": "Questionable",
            "practice_status": "DNP", "practice_pattern": "DNP-DNP-LP",
            "role_trend": "role_down", "returning_teammates": 1, "inherited_volume": True,
            "qb_mult": 0.85, "qb_sleeper_mult": 0.8, "news_flags": ["week_to_week"],
            "matchup_tier": "tough",
        }
        assert signals_of({}) == {}

    def test_logged_rows_keep_their_signals(self, db):
        rows = [_proj("A", "1", 10.0, role_trend="role_up", projection_source="sleeper_blend")]
        assert log_projections(db, 2026, 5, "ppr", rows, source="project_players") == 1
        row = db.get_logged_week_rows(2026, 5)[0]
        assert json.loads(row["signals"]) == {"role_trend": "role_up",
                                              "projection_source": "sleeper_blend"}


class TestGradeWeek:
    async def _setup(self, db, monkeypatch, stats=None, matchups=None):
        scoring = league_scoring(LEAGUE)
        log_projections(db, 2026, 4, scoring, [
            _proj("A", "1", 10.0, projection_source="sleeper_blend", model_projection=8.0,
                  sleeper_projection=11.0, role_trend="role_down"),
            _proj("B", "2", 6.0, projection_source="model_only"),
            _proj("C", "3", 5.0, injury_status="Questionable"),
            _proj("D", "4", 0.0),
        ], league_id="L1", source="briefing")
        # A later pre-kickoff row for A: the one graded.
        db.record_projections(2026, 4, scoring.model.fingerprint,
                              [{"player_id": "1", "projected_points": 12.0, "name": "A",
                                "position": "WR", "team": "BUF",
                                "signals": {"projection_source": "sleeper_blend",
                                            "model_projection": 9.0,
                                            "sleeper_projection": 13.0,
                                            "role_trend": "role_down"}}],
                              league_id="L1", source="briefing",
                              now=(datetime.now(UTC) + timedelta(minutes=5)).isoformat())

        async def _stats(season, week):
            return stats if stats is not None else {
                "1": {"gp": 1, "rec": 5, "rec_yd": 50, "pts_half_ppr": 7.5},
                "2": {"gp": 1, "rec": 2, "rec_yd": 30, "pts_half_ppr": 4.0}}

        async def _league(lid):
            return LEAGUE

        async def _matchups(lid, week):
            return matchups if matchups is not None else [
                {"roster_id": 1, "players_points": {"1": 7.4}}]
        monkeypatch.setattr(pa, "_week_stats", _stats)
        monkeypatch.setattr(pa, "_league", _league)
        monkeypatch.setattr(pa, "_matchups", _matchups)
        return scoring

    async def test_grades_the_last_pre_kickoff_row(self, db, monkeypatch):
        await self._setup(db, monkeypatch)
        res = await pa.grade_week(db, 2026, 4)
        assert res["graded"] == 4
        rows = {r["player_id"]: r for r in db.get_projection_accuracy(2026)}
        a = rows["1"]
        assert a["projected"] == 12.0 and a["model_projection"] == 9.0
        # Sleeper's own league points win over the stat line.
        assert a["actual"] == 7.4 and a["actual_source"] == "league_matchup"
        assert a["actual_half_ppr"] == 7.5 and a["role_trend"] == "role_down"
        b = rows["2"]
        # Not in a matchup: the stat line priced in the league's scoring.
        assert b["actual"] == pytest.approx(2 * 0.5 + 30 * 0.1)
        assert b["actual_source"] == "stats" and b["projection_source"] == "model_only"
        c = rows["3"]
        assert c["actual"] == 0.0 and c["played"] == 0 and c["actual_source"] == "no_stat_line"
        assert c["injury_status"] == "Questionable"

    async def test_no_stats_yet_writes_nothing(self, db, monkeypatch):
        await self._setup(db, monkeypatch, stats={})
        res = await pa.grade_week(db, 2026, 4)
        assert res["graded"] == 0 and db.get_projection_accuracy(2026) == []

    async def test_unknown_scoring_is_skipped(self, db, monkeypatch):
        await self._setup(db, monkeypatch)
        db.record_projections(2026, 4, "deadbeef0000",
                              [{"player_id": "9", "projected_points": 3.0}])
        res = await pa.grade_week(db, 2026, 4)
        assert res["skipped_scoring"] == ["deadbeef0000"]

    async def test_preset_scoring_without_a_league(self, db, monkeypatch):
        await self._setup(db, monkeypatch)
        log_projections(db, 2026, 4, "half_ppr", [_proj("B", "2", 5.0)], source="project_players")
        await pa.grade_week(db, 2026, 4)
        half = [r for r in db.get_projection_accuracy(2026) if r["league_id"] is None]
        assert half and half[0]["actual"] == pytest.approx(2 * 0.5 + 30 * 0.1)

    async def test_report_and_tool(self, db, monkeypatch):
        await self._setup(db, monkeypatch)

        async def _done(_db=None):
            return {"season": 2026, "week": 4, "source": "test"}
        monkeypatch.setattr("nfl_mcp.week_context.last_completed_week", _done)
        out = await pa.get_projection_accuracy(db=db)
        assert out["success"] and out["graded_now"] == [4] and out["weeks"] == [4]
        # D (projected 0, scored 0) is left out.
        assert out["overall"]["n"] == 3 and out["excluded_zero_zero"] == 1
        assert out["by_projection_source"]["model_only"]["n"] == 1
        assert out["by_signal"]["role_down"]["with"]["n"] == 1
        assert out["by_signal"]["questionable"]["with"]["bias"] == 5.0
        assert out["components"]["model"]["mae"] == pytest.approx(1.6)
        assert out["interpretation"]
        # Graded already: not regraded on the next call.
        again = await pa.get_projection_accuracy(db=db, position="WR")
        assert again["graded_now"] == [] and again["position"] == "WR"


class TestReport:
    def _row(self, week, projected, actual, **kw):
        return {"week": week, "position": kw.pop("position", "WR"), "projected": projected,
                "actual": actual, "signals": json.dumps(kw.pop("signals", {"x": 1})), **kw}

    def test_signal_gap_is_called_out(self):
        rows = [self._row(5, 15.0, 10.0, role_trend="role_up") for _ in range(12)]
        rows += [self._row(5, 10.0, 10.0) for _ in range(12)]
        rep = pa.accuracy_report(rows)
        s = rep["by_signal"]["role_up"]
        assert s["with"]["bias"] == 5.0 and s["without"]["bias"] == 0.0
        assert s["bias_gap"] == 5.0 and not s["small_sample"]
        assert any(line.startswith("role_up:") for line in rep["interpretation"])

    def test_news_flags_become_buckets_and_trend_is_read(self):
        rows = []
        for w, err in ((3, 6.0), (4, 4.0), (5, 2.0)):
            rows += [self._row(w, 10.0 + err, 10.0, news_flags=json.dumps(["benched"]))
                     for _ in range(10)]
        rep = pa.accuracy_report(rows)
        assert rep["by_signal"]["news:benched"]["with"]["n"] == 30
        assert [t["mae"] for t in rep["trend"]] == [6.0, 4.0, 2.0]
        assert any("improved" in line for line in rep["interpretation"])

    def test_rows_without_signals_count_only_in_totals(self):
        rows = [self._row(3, 10.0, 8.0, signals=None)]
        rep = pa.accuracy_report(rows)
        assert rep["overall"]["n"] == 1 and rep["rows_with_signals"] == 0
        assert rep["by_signal"] == {}

    def test_empty(self):
        rep = pa.accuracy_report([])
        assert rep["overall"]["n"] == 0
        assert rep["interpretation"] == ["No graded projections yet for these weeks."]

    def test_price_actual_uses_real_tier_flags(self):
        model = league_scoring(LEAGUE).model
        assert pa.price_actual({"rec": 4, "rec_yd": 40, "gp": 1}, model) == 6.0
        assert pa.price_actual(None, model) == 0.0


class TestWeeksToGrade:
    def test_ungraded_and_stat_correction_regrade(self, db):
        log_projections(db, 2026, 3, "ppr", [_proj("A", "1", 10.0)])
        log_projections(db, 2026, 4, "ppr", [_proj("A", "1", 10.0)])
        log_projections(db, 2026, 5, "ppr", [_proj("A", "1", 10.0)])
        last = datetime(2026, 9, 29, 0, 15, tzinfo=UTC)
        db.upsert_schedule_games([{"season": 2026, "week": 3, "team": "BUF",
                                   "opponent": "MIA", "is_home": 1,
                                   "kickoff": last.isoformat()}])
        db.upsert_projection_accuracy([{
            "season": 2026, "week": 3, "scoring_key": "k", "player_id": "1",
            "projected": 10.0, "actual": 9.0,
            "graded_at": (last + timedelta(hours=4)).isoformat()}])
        # Week 3 graded 4h after its last game: regraded once corrections are in.
        assert pa.weeks_to_grade(db, 2026, 4, now=last + timedelta(hours=10)) == [4]
        assert pa.weeks_to_grade(db, 2026, 4, now=last + timedelta(days=2)) == [3, 4]
        # Week 5 is not finished.
        assert 5 not in pa.weeks_to_grade(db, 2026, 4, now=last + timedelta(days=2))


class TestRefreshScope:
    def test_accuracy_is_a_refresh_scope_and_runs_in_the_prefetch(self):
        assert "accuracy" in data_refresh.REFRESH_SCOPES
        assert "accuracy" in server._cycle_scopes(5, 2)
        assert "accuracy" not in server._cycle_scopes(1, 2)

    async def test_scope_runs_the_grading(self, db, monkeypatch):
        seen = {}

        async def _refresh(_db, season=None, weeks=None):
            seen["season"] = season
            return {"fetched": 1, "written": 7, "weeks": [4], "season": season}
        monkeypatch.setattr(pa, "refresh_accuracy", _refresh)
        out = await data_refresh.run_scope("accuracy", db, 2026, 5)
        assert out["status"] == "ok" and out["written"] == 7 and seen["season"] == 2026


class TestMigration:
    def test_v17_database_gets_the_accuracy_table_and_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            NFLDatabase(path).close()
            with sqlite3.connect(path) as conn:
                conn.execute("DELETE FROM schema_version WHERE version >= 18")
                conn.execute("DROP TABLE projection_accuracy")
                conn.execute("ALTER TABLE projection_log DROP COLUMN signals")
                conn.commit()
            NFLDatabase(path).close()
            with sqlite3.connect(path) as conn:
                assert (conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                        == NFLDatabase.CURRENT_SCHEMA_VERSION)
                assert "signals" in {r[1] for r in conn.execute("PRAGMA table_info(projection_log)")}
                assert conn.execute("SELECT COUNT(*) FROM projection_accuracy").fetchone()[0] == 0
