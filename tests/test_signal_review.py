"""The weekly signal review (schema v21) on synthetic graded rows (offline)."""
import random
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nfl_mcp import projection_accuracy as pa
from nfl_mcp import signal_review as sr
from nfl_mcp.database import NFLDatabase
from nfl_mcp.projection_store import signals_of
from nfl_mcp.projections import QUESTIONABLE_REALISED


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        d = NFLDatabase(str(Path(tmp) / "rev.db"))
        yield d
        d.close()


_rng = random.Random(7)
_pid = iter(range(1, 100000))


def _row(week, projected, actual, league_id="L1", **sig):
    """A graded row as `grade_week` writes it (signals unpacked + JSON)."""
    sig = {"projection_source": "sleeper_blend", **sig}
    return {
        "season": 2026, "week": week, "scoring_key": "k", "player_id": str(next(_pid)),
        "league_id": league_id, "player_name": sig.pop("name", "P"), "position": "WR",
        "team": "BUF", "projected": projected, "actual": actual,
        "model_projection": sig.get("model_projection"),
        "sleeper_projection": sig.get("sleeper_projection"),
        "played": 1, "projection_source": sig.get("projection_source"),
        "injury_status": sig.get("injury_status"),
        "practice_status": sig.get("practice_status"),
        "practice_pattern": sig.get("practice_pattern"),
        "role_trend": sig.get("role_trend"),
        "returning_teammates": sig.get("returning_teammates") or 0,
        "qb_mult": sig.get("qb_mult"), "news_flags": sig.get("news_flags") or [],
        "signals": sig, "log_source": "briefing", "graded_at": datetime.now(UTC).isoformat(),
    }


def _healthy(week, n, ratio=1.0, projected=10.0):
    return [_row(week, projected, round(projected * ratio * _rng.uniform(0.6, 1.4), 2))
            for _ in range(n)]


def _q_lp(week, n, ratio):
    return [_row(week, 7.2, round(7.2 * ratio * _rng.uniform(0.85, 1.15), 2),
                 injury_status="Questionable", practice_status="LP",
                 practice_pattern="DNP-LP-LP") for _ in range(n)]


def _entry(review, name):
    return next(e for e in review["signals"] if e["signal"] == name)


class TestReview:
    def test_too_low_multiplier_is_a_watch_below_the_sample_rule(self):
        rows = _healthy(5, 200) + _q_lp(5, 23, 1.22)
        rev = sr.review_rows(rows, 5)
        e = _entry(rev, "q_lp")
        assert e["cumulative"]["n"] == 23 and e["current"] == QUESTIONABLE_REALISED["LP"]
        assert e["cumulative"]["relative_ratio"] == pytest.approx(1.22, abs=0.08)
        lo, hi = e["cumulative"]["relative_ci"]
        assert lo > 1.0 and lo < e["cumulative"]["relative_ratio"] < hi
        assert e["implied"] == pytest.approx(0.72 * e["cumulative"]["relative_ratio"], abs=0.01)
        assert e["verdict"] == "watch"
        assert re.search(r"multiplier 0.72 looks too low: realised 0\.\d\d \[0\.\d\d, ", e["recommendation"])
        assert "n=23" in e["recommendation"] and "keep until n≥60" in e["recommendation"]
        assert e["recommendation"] in rev["recommendations"]
        # The baseline of an injury bucket is the healthy rows only.
        assert e["baseline"] == "healthy" and e["cumulative"]["baseline_n"] == 200

    def test_enough_rows_over_two_weeks_is_a_review(self):
        rows = (_healthy(4, 150) + _healthy(5, 150)
                + _q_lp(4, 40, 1.3) + _q_lp(5, 40, 1.3))
        e = _entry(sr.review_rows(rows, 5), "q_lp")
        assert e["verdict"] == "review" and e["cumulative"]["weeks"] == [4, 5]
        assert "QUESTIONABLE_REALISED['LP']" in e["recommendation"]
        assert "backtest" in e["recommendation"]
        # The week alone is reported next to the cumulative read.
        assert e["week"]["n"] == 40

    def test_one_week_is_never_enough_to_act(self):
        rows = _healthy(5, 300) + _q_lp(5, 80, 1.3)
        e = _entry(sr.review_rows(rows, 5), "q_lp")
        assert e["verdict"] == "watch" and "≥2 weeks" in e["recommendation"]

    def test_calibrated_signal_keeps(self):
        rows = _healthy(5, 200) + _q_lp(5, 30, 1.0)
        e = _entry(sr.review_rows(rows, 5), "q_lp")
        assert e["verdict"] == "calibrated"
        assert "holds" in e["recommendation"] and "keep" in e["recommendation"]

    def test_small_sample_is_not_read(self):
        rows = _healthy(5, 50) + _q_lp(5, 4, 2.0)
        e = _entry(sr.review_rows(rows, 5), "q_lp")
        assert e["verdict"] == "insufficient" and "too few" in e["recommendation"]
        assert e["recommendation"] not in sr.review_rows(rows, 5)["recommendations"]

    def test_signal_without_a_constant_reads_as_a_ratio(self):
        rows = _healthy(5, 100) + [_row(5, 10.0, 6.0, role_trend="role_up")
                                   for _ in range(15)]
        e = _entry(sr.review_rows(rows, 5), "role_up")
        assert "current" not in e and e["cumulative"]["relative_ratio"] < 0.8
        assert "over-rates" in e["recommendation"]

    def test_logged_multiplier_is_the_current_value(self):
        rows = _healthy(5, 100) + [_row(5, 8.0, 8.0, role_trend="role_down", role_mult=0.85)
                                   for _ in range(12)]
        e = _entry(sr.review_rows(rows, 5), "role_down")
        assert e["current"] == 0.85

    def test_buckets_news_qb_and_gameday(self):
        rows = _healthy(5, 60)
        rows += [_row(5, 5.0, 4.0, news_flags=["benched"]) for _ in range(3)]
        rows += [_row(5, 9.0, 8.0, qb_mult=0.9, qb_sit_basis="gameday") for _ in range(3)]
        rows += [_row(5, 9.0, 9.0, injury_status="Questionable", gameday_status="active")
                 for _ in range(3)]
        rows += [_row(5, 3.0, 2.0, injury_status="Questionable", practice_status="DNP",
                      practice_pattern="DNP") for _ in range(2)]
        names = {e["signal"] for e in sr.review_rows(rows, 5)["signals"]}
        assert {"news:benched", "qb_coupling", "qb_coupling:gameday", "gameday_active_q",
                "q_dnp_single", "source:sleeper_blend"} <= names
        # Confirmed active is its own bucket, not Q with no practice line.
        assert "q_no_practice" not in names

    def test_later_weeks_and_rows_without_signals_are_left_out(self):
        rows = _healthy(5, 20) + _healthy(6, 20)
        rows.append({**_row(5, 10.0, 3.0), "signals": None})
        rev = sr.review_rows(rows, 5)
        assert rev["rows_cumulative"] == 20 and rev["rows_without_signals"] == 1
        assert rev["weeks_cumulative"] == [5]

    def test_bootstrap_is_deterministic(self):
        hit = _q_lp(5, 20, 1.2)
        base = _healthy(5, 100)
        assert sr.bootstrap_relative(hit, base, "x") == sr.bootstrap_relative(hit, base, "x")
        assert sr.bootstrap_relative(hit[:1], base, "x") is None


class TestMisses:
    def test_roster_filter_and_signals_in_words(self):
        rows = [_row(5, 15.0, 2.0, name="Busted", injury_status="Questionable",
                     practice_pattern="LP-LP", injury_mult=0.72, practice_blend_mult=0.78),
                _row(5, 10.0, 25.0, name="Boom", role_trend="role_up", role_mult=1.1,
                     role_gain_priced=True),
                _row(5, 10.0, 11.0, name="Fine"),
                _row(5, 0.0, 0.0, name="Bye")]
        mine = {rows[0]["player_id"], rows[2]["player_id"], rows[3]["player_id"]}
        out = sr.biggest_misses(rows, 5, mine)
        assert [m["player"] for m in out] == ["Busted", "Fine"]
        assert out[0]["diff"] == -13.0
        assert out[0]["signals"] == ["Questionable (LP-LP) ×0.72 on ours, ×0.78 on Sleeper's"]
        every = sr.biggest_misses(rows, 5)
        assert every[0]["player"] == "Boom"
        assert every[0]["signals"] == ["role_up ×1.1 (gain priced)"]

    def test_describe_logged_signals(self):
        words = sr.describe_signals({
            "qb_mult": 0.88, "qb_sit_basis": "gameday", "news_flags": ["committee"],
            "news_model_mult": 0.95, "gameday_status": "active", "injury_exit": True,
            "projection_source": "model_only", "model_projection": 12.0,
            "sleeper_projection": 6.0})
        assert words == ["gameday: active", "injury-shortened game excluded",
                         "backup QB ×0.88 (gameday)", "news: committee ×0.95",
                         "source: model_only", "ours 12 vs Sleeper 6"]


class TestSignalsLogged:
    def test_applied_multipliers_and_gameday_are_logged(self):
        sig = signals_of({
            "gameday_status": "active", "role_gain": {"priced": True, "multiplier": 1.1},
            "breakdown": {"role_mult": 1.1, "practice_blend_mult": 1.0, "injury_mult": 0.72,
                          "news_model_mult": 0.95, "injury_exit_weeks": [3]},
            "qb_context": {"applied": True, "model_mult": 0.9, "sleeper_mult": 0.85,
                           "starter_sit_weight": 0.5, "starter_sit_basis": "practice"},
        })
        assert sig["gameday_status"] == "active" and sig["role_gain_priced"] is True
        assert sig["role_mult"] == 1.1 and sig["injury_mult"] == 0.72
        assert sig["news_model_mult"] == 0.95 and sig["injury_exit"] is True
        # A multiplier of 1.0 moved nothing and is not logged.
        assert "practice_blend_mult" not in sig
        assert sig["qb_sit_weight"] == 0.5 and sig["qb_sit_basis"] == "practice"


class TestStoreAndTool:
    def _grade(self, db, rows):
        db.upsert_projection_accuracy(rows)

    async def test_refresh_stores_the_review_and_the_tool_reads_it(self, db, monkeypatch):
        rows = _healthy(5, 40) + _q_lp(5, 12, 1.2)
        rows[0]["player_name"] = "Mine"

        async def _grade_week(_db, season, week):
            _db.upsert_projection_accuracy(rows)
            return {"week": week, "graded": len(rows)}
        monkeypatch.setattr(pa, "grade_week", _grade_week)

        async def _done(_db=None):
            return {"season": 2026, "week": 5, "source": "test"}
        monkeypatch.setattr("nfl_mcp.week_context.last_completed_week", _done)
        out = await pa.refresh_accuracy(db, 2026, weeks=[5])
        assert out["reviewed"] == [5]
        stored = db.get_signal_review(2026, 5)
        assert stored["rows_graded"] == 52
        assert any(e["signal"] == "q_lp" for e in stored["payload"]["signals"])
        assert db.get_signal_review(2026) is not None  # newest week

        res = await sr.get_weekly_signal_review(db=db)
        assert res["success"] and res["week"] == 5 and res["source"] == "stored"
        assert res["biggest_misses"] and res["rules"]["min_n_act"] == sr.MIN_N_ACT

    async def test_league_filter_and_roster_misses(self, db, monkeypatch):
        rows = _healthy(5, 30) + _healthy(5, 10)
        for r in rows[30:]:
            r["league_id"] = "L2"
        mine = rows[0]
        mine["actual"], mine["projected"], mine["player_name"] = 30.0, 10.0, "Mine"
        db.upsert_projection_accuracy(rows)

        async def _done(_db=None):
            return {"season": 2026, "week": 5, "source": "test"}
        monkeypatch.setattr("nfl_mcp.week_context.last_completed_week", _done)

        async def _ids(league_id, week, roster_id, user_id):
            return {mine["player_id"]}, 7, None
        monkeypatch.setattr(sr, "_roster_player_ids", _ids)
        res = await sr.get_weekly_signal_review(league_id="L1", roster_id=7, db=db)
        assert res["source"] == "computed" and res["rows_week"] == 30
        assert [m["player"] for m in res["biggest_misses"]] == ["Mine"]
        assert res["misses_scope"] == "roster 7 in league L1"
        # A league-filtered review is not stored as the all-leagues one.
        assert db.get_signal_review(2026, 5) is None

    async def test_no_graded_week(self, db, monkeypatch):
        async def _done(_db=None):
            return {"season": 2026, "week": 0, "source": "test"}
        monkeypatch.setattr("nfl_mcp.week_context.last_completed_week", _done)
        res = await sr.get_weekly_signal_review(db=db)
        assert res["success"] is False and "No graded week" in res["error"]

    async def test_stale_stored_review_is_recomputed(self, db, monkeypatch):
        rows = _healthy(5, 20)
        db.upsert_projection_accuracy(rows)
        db.save_signal_review(2026, 5, sr.SCOPE_ALL, {"signals": [], "stale": True})
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("UPDATE signal_reviews SET created_at='2000-01-01'")
        rev = await sr.weekly_signal_review(db, 2026, 5)
        assert rev["source"] == "computed" and "stale" not in rev


class TestMigration:
    def test_v20_database_gets_the_review_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            NFLDatabase(path).close()
            with sqlite3.connect(path) as conn:
                conn.execute("DELETE FROM schema_version WHERE version >= 21")
                conn.execute("DROP TABLE signal_reviews")
            d = NFLDatabase(path)
            assert d.save_signal_review(2026, 5, "all", {"a": 1}, 3)
            assert d.get_signal_review(2026, 5)["payload"] == {"a": 1}
            d.close()
