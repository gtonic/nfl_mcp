"""Advice built on a stale feed must say so.

Everything else in this codebase labels a guess — `is_fallback`, `stale`,
placeholder warnings. The injury and roster caches did not, so a start/sit
recommendation could be made against a day-old injury report with nothing in
the output to indicate it.
"""
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nfl_mcp.briefing_tools import (
    STALE_ATHLETES_HOURS,
    STALE_INJURY_HOURS,
    _staleness_warnings,
)
from nfl_mcp.database import NFLDatabase


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield NFLDatabase(str(Path(tmp) / "t.db"))


def _ago(hours):
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


class TestStalenessWarnings:
    def test_fresh_data_warns_about_nothing(self):
        fresh = {"injuries": {"age_hours": 0.5}, "athletes": {"age_hours": 2.0}}
        assert _staleness_warnings(fresh) == []

    def test_old_injury_report_warns(self):
        old = {"injuries": {"age_hours": STALE_INJURY_HOURS + 1},
               "athletes": {"age_hours": 1.0}}
        warnings = _staleness_warnings(old)
        assert len(warnings) == 1
        assert "injury report" in warnings[0]

    def test_old_athlete_cache_warns_separately(self):
        old = {"injuries": {"age_hours": 1.0},
               "athletes": {"age_hours": STALE_ATHLETES_HOURS + 1}}
        warnings = _staleness_warnings(old)
        assert len(warnings) == 1
        assert "roster/player data" in warnings[0]

    def test_never_fetched_is_not_treated_as_fresh(self):
        """`None` must not read as 0 hours old — the distinction is the point."""
        empty = {"injuries": {"age_hours": None}, "athletes": {"age_hours": None}}
        warnings = _staleness_warnings(empty)
        assert len(warnings) == 2
        assert all("unknown, not clear" in w or "No " in w for w in warnings)

    def test_missing_keys_are_handled(self):
        assert len(_staleness_warnings({})) == 2

    def test_exactly_at_the_limit_does_not_warn(self):
        at_limit = {"injuries": {"age_hours": STALE_INJURY_HOURS},
                    "athletes": {"age_hours": STALE_ATHLETES_HOURS}}
        assert _staleness_warnings(at_limit) == []


class TestGetDataFreshness:
    def test_empty_database_reports_none_not_zero(self, db):
        out = db.get_data_freshness()
        for feed in ("injuries", "athletes", "practice_status"):
            assert out[feed]["updated_at"] is None
            assert out[feed]["age_hours"] is None

    def test_reports_the_newest_row_per_feed(self, db):
        db.upsert_injuries([{
            "player_id": "1", "player_name": "X", "team_id": "KC", "position": "TE",
            "injury_status": "Out", "injury_type": "Knee", "severity": 4,
            "confidence": 90, "sources": ["ESPN"],
        }])
        db.upsert_athletes({"a1": {"full_name": "Y", "position": "WR", "team": "KC"}})

        out = db.get_data_freshness()
        assert out["injuries"]["age_hours"] is not None
        assert out["injuries"]["age_hours"] < 1
        assert out["athletes"]["age_hours"] < 1

    def test_age_grows_with_an_older_timestamp(self, db):
        import sqlite3
        db.upsert_injuries([{
            "player_id": "1", "player_name": "X", "team_id": "KC", "position": "TE",
            "injury_status": "Out", "sources": ["ESPN"],
        }])
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("UPDATE player_injuries SET updated_at=?", (_ago(30),))
            conn.commit()

        age = db.get_data_freshness()["injuries"]["age_hours"]
        assert 29 < age < 31

    def test_a_missing_table_does_not_raise(self, db):
        """Freshness is diagnostics; it must never be the thing that fails."""
        import sqlite3
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("DROP TABLE IF EXISTS player_practice_status")
            conn.commit()
        out = db.get_data_freshness()
        assert out["practice_status"]["age_hours"] is None
        assert "injuries" in out

    def test_column_names_match_the_real_schema(self, db):
        """Guards the column guess: a wrong name silently returns None forever."""
        import sqlite3
        with sqlite3.connect(db.db_path) as conn:
            for table in ("player_injuries", "athletes", "player_practice_status"):
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                assert "updated_at" in cols, f"{table} has no updated_at: {cols}"
