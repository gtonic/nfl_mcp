"""Regression tests: a failed write must not leave the database locked, and
pruning runs on a clock rather than a restart-sensitive cycle count."""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nfl_mcp.database import NFLDatabase


@pytest.fixture
def db(tmp_path):
    database = NFLDatabase(str(tmp_path / "test.db"))
    # Short busy timeout so a regression fails in well under a second instead
    # of stalling for the production 30s.
    for conn in list(database._pool._pool.queue):
        conn.execute("PRAGMA busy_timeout=300")
    yield database
    database.close()


def _injury(player_id, sources=None):
    return {
        "player_id": player_id, "player_name": player_id, "team_id": "KC",
        "injury_status": "Out", "sources": sources or ["ESPN"],
    }


def _injury_count(db):
    with db._pool.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM player_injuries").fetchone()[0]


class TestPoolRollback:
    def test_failed_write_does_not_lock_next_writer(self, db):
        # Row 1 is written, row 2 fails to serialise: the batch aborts mid-transaction.
        bad = [_injury("p1"), _injury("p2", sources=[object()])]
        assert db.upsert_injuries(bad) == 0

        start = time.monotonic()
        # Every pooled connection must be able to write straight away.
        assert db.add_injury_history("p9", "KC", "Out") is True
        assert db.add_injury_history("p9", "KC", "Questionable") is True
        assert db.upsert_injuries([_injury("p3")]) == 1
        assert time.monotonic() - start < 0.3

    def test_half_written_batch_is_never_committed_later(self, db):
        db.upsert_injuries([_injury("p1"), _injury("p2", sources=[object()])])
        # Later commits on any pooled connection must not persist row p1.
        for _ in range(3):
            db.add_injury_history("p9", "KC", "Out")
        with db._pool.get_connection() as conn:
            ids = [r[0] for r in conn.execute("SELECT player_id FROM player_injuries")]
        assert ids == []

    def test_uncommitted_borrow_is_rolled_back_on_return(self, db):
        with db._pool.get_connection() as conn:
            conn.execute(
                "INSERT INTO injury_history(player_id, team_id, injury_status, recorded_at)"
                " VALUES('x','KC','Out','2026-01-01')"
            )
            assert conn.in_transaction
        for conn in list(db._pool._pool.queue):
            assert not conn.in_transaction
        assert db.get_injury_history("x") == []

    def test_connection_that_cannot_roll_back_is_discarded(self, db):
        broken = MagicMock()
        broken.in_transaction = True
        broken.rollback.side_effect = RuntimeError("disk I/O error")
        before = db._pool._total_connections
        assert db._pool._reset_connection(broken) is False
        broken.close.assert_called_once()
        assert db._pool._total_connections == before - 1


class TestPruneOldData:
    def test_prunes_old_rows_and_keeps_history_anchor(self, db):
        now = datetime.now(UTC)
        old = (now - timedelta(days=200)).isoformat()
        older = (now - timedelta(days=300)).isoformat()
        recent = (now - timedelta(days=1)).isoformat()
        with db._pool.get_connection() as conn:
            conn.executemany(
                "INSERT INTO roster_snapshots (league_id, payload_json, fetched_at) VALUES (?,?,?)",
                [("L", "[]", old), ("L", "[]", recent)],
            )
            conn.executemany(
                "INSERT INTO player_practice_status(player_id, date, status, updated_at)"
                " VALUES(?,?,?,?)",
                [("p1", "2026-01-01", "DNP", old), ("p1", "2026-09-20", "FP", recent)],
            )
            conn.executemany(
                "INSERT INTO injury_history(player_id, team_id, injury_status, recorded_at)"
                " VALUES(?,?,?,?)",
                [
                    ("p1", "KC", "Out", older),         # pruned
                    ("p1", "KC", "Questionable", old),  # anchor: newest before cutoff
                    ("p1", "KC", "Active", recent),
                ],
            )
            conn.commit()

        deleted = db.prune_old_data()

        assert deleted["roster_snapshots"] == 1
        assert deleted["player_practice_status"] == 1
        assert deleted["injury_history"] == 1
        # The anchor keeps the recent change reported as a recovery, not "new".
        changes = db.get_injury_status_changes(since=(now - timedelta(days=7)).isoformat())
        assert [(c["previous_status"], c["injury_status"]) for c in changes] == [
            ("Questionable", "Active")
        ]


class TestPruneSchedule:
    @pytest.mark.asyncio
    async def test_prune_is_time_based(self, monkeypatch):
        from nfl_mcp import server

        clock = [1000.0]
        monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(server, "_last_prune_at", None)
        monkeypatch.setattr(server, "DB_PRUNE_INTERVAL_SECONDS", 86400)
        nfl_db = MagicMock()
        nfl_db.prune_old_data.return_value = {"roster_snapshots": 0}

        assert await server._prune_db_if_due(nfl_db) is True  # first call (startup)
        clock[0] += 3600
        assert await server._prune_db_if_due(nfl_db) is False
        clock[0] += 86400
        assert await server._prune_db_if_due(nfl_db) is True
        assert nfl_db.prune_old_data.call_count == 2

    @pytest.mark.asyncio
    async def test_startup_prunes_even_without_prefetch(self, monkeypatch):
        from nfl_mcp import server

        monkeypatch.setattr(server, "PREFETCH_ENABLED", False)
        monkeypatch.setattr(server, "_last_prune_at", None)
        monkeypatch.setattr(server, "_prefetch_task", None)
        monkeypatch.setattr(server, "_shutdown_event", None)
        prune = AsyncMock(return_value=True)
        monkeypatch.setattr(server, "_prune_db_if_due", prune)

        async with server._create_prefetch_lifespan(MagicMock())(MagicMock()):
            pass

        prune.assert_awaited_once()
        assert prune.await_args.kwargs.get("tag") == "Startup"
