"""Injury status history: recorded on change, queryable as a trend."""
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nfl_mcp.database import NFLDatabase


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield NFLDatabase(str(Path(tmp) / "test.db"))


def _injury(status, injury_type="Hamstring", player_id="1", team="BUF"):
    return {
        "player_id": player_id,
        "player_name": "Ty Johnson",
        "team_id": team,
        "position": "RB",
        "injury_status": status,
        "injury_type": injury_type,
    }


def _count(db):
    with sqlite3.connect(db.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM injury_history").fetchone()[0]


class TestInjuryHistoryRecording:
    def test_first_sighting_is_recorded(self, db):
        db.upsert_injuries([_injury("Questionable")])
        assert _count(db) == 1

    def test_unchanged_report_is_not_recorded_again(self, db):
        db.upsert_injuries([_injury("Questionable")])
        # The prefetch loop re-sends the identical feed every cycle; without
        # change detection this table would grow without bound.
        db.upsert_injuries([_injury("Questionable")])
        db.upsert_injuries([_injury("Questionable")])
        assert _count(db) == 1

    def test_status_change_is_recorded(self, db):
        db.upsert_injuries([_injury("Questionable")])
        db.upsert_injuries([_injury("Out")])
        assert _count(db) == 2

        history = db.get_injury_history("1")
        assert [h["injury_status"] for h in history] == ["Out", "Questionable"]

    def test_injury_type_change_is_recorded(self, db):
        db.upsert_injuries([_injury("Out", injury_type="Hamstring")])
        db.upsert_injuries([_injury("Out", injury_type="Knee")])
        assert _count(db) == 2

    def test_current_snapshot_still_upserts(self, db):
        db.upsert_injuries([_injury("Questionable")])
        db.upsert_injuries([_injury("Out")])
        current = db.get_team_injuries_from_cache("BUF")
        assert len(current) == 1
        assert current[0]["injury_status"] == "Out"


class TestInjuryStatusChanges:
    def test_previous_status_and_filtering(self, db):
        db.upsert_injuries([_injury("Questionable"), _injury("Active", player_id="2", team="KC")])
        db.upsert_injuries([_injury("Out"), _injury("Out", player_id="2", team="KC")])

        since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        changes = db.get_injury_status_changes(since=since)
        assert len(changes) == 4

        latest = {(c["player_id"], c["injury_status"]): c for c in changes}
        assert latest[("1", "Out")]["previous_status"] == "Questionable"
        assert latest[("1", "Questionable")]["previous_status"] is None
        assert latest[("1", "Out")]["player_name"] == "Ty Johnson"

        only_kc = db.get_injury_status_changes(since=since, teams=["KC"])
        assert {c["team_id"] for c in only_kc} == {"KC"}

    def test_window_excludes_older_entries(self, db):
        db.upsert_injuries([_injury("Questionable")])
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        assert db.get_injury_status_changes(since=future) == []

    def test_player_filter_applies_before_the_limit(self, db):
        """A bulk backfill must not push the player of interest off the page."""
        from nfl_mcp.injury_service import STATUS_SEVERITY

        db.upsert_injuries([_injury("Questionable", player_id="mine")])
        # 50 other players show up afterwards, newer than the row we want.
        db.upsert_injuries([
            _injury("Questionable", player_id=f"other{i}", team="KC") for i in range(50)
        ])
        since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

        assert len(db.get_injury_status_changes(since=since, limit=10)) == 10
        mine = db.get_injury_status_changes(since=since, limit=10, player_ids=["mine"])
        assert [c["player_id"] for c in mine] == ["mine"]

        # Same for direction: the newest rows are all first sightings, so a
        # post-filter would report zero downgrades despite there being one.
        db.upsert_injuries([_injury("Out", player_id="mine")])
        db.upsert_injuries([
            _injury("Questionable", player_id=f"late{i}", team="KC") for i in range(50)
        ])
        worse = db.get_injury_status_changes(
            since=since, limit=10, direction="worse", severity_map=STATUS_SEVERITY
        )
        assert [(c["player_id"], c["injury_status"]) for c in worse] == [("mine", "Out")]

    def test_unknown_direction_is_ignored_rather_than_emptying_the_result(self, db):
        from nfl_mcp.injury_service import STATUS_SEVERITY

        db.upsert_injuries([_injury("Questionable")])
        since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        rows = db.get_injury_status_changes(
            since=since, direction="sideways", severity_map=STATUS_SEVERITY
        )
        assert len(rows) == 1


class TestInjuryTrendsTool:
    @pytest.mark.asyncio
    async def test_direction_is_derived_from_severity(self, db, monkeypatch):
        from nfl_mcp import tool_registry

        monkeypatch.setattr(tool_registry, "get_db", lambda: db)

        db.upsert_injuries([_injury("Questionable")])
        db.upsert_injuries([_injury("Out")])          # worse
        db.upsert_injuries([_injury("Questionable")])  # better

        result = await tool_registry.get_injury_trends(lookback_hours=1)
        assert result["success"] is True
        directions = [c["direction"] for c in result["changes"]]
        assert directions == ["better", "worse", "new"]

        worse = await tool_registry.get_injury_trends(lookback_hours=1, direction="worse")
        assert worse["total_changes"] == 1
        assert worse["changes"][0]["severity_delta"] == 2

    @pytest.mark.asyncio
    async def test_limit_counts_matching_changes_not_scanned_rows(self, db, monkeypatch):
        """`limit` is a budget for results, not for rows the filter throws away."""
        from nfl_mcp import tool_registry

        monkeypatch.setattr(tool_registry, "get_db", lambda: db)

        db.upsert_injuries([_injury("Questionable", player_id="hurt")])
        db.upsert_injuries([_injury("Out", player_id="hurt")])  # the one downgrade
        # A feed backfill lands afterwards: 30 first sightings, all newer.
        db.upsert_injuries([
            _injury("Questionable", player_id=f"bulk{i}", team="KC") for i in range(30)
        ])

        worse = await tool_registry.get_injury_trends(
            lookback_hours=1, direction="worse", limit=5
        )
        assert worse["total_changes"] == 1
        assert worse["changes"][0]["player_id"] == "hurt"
