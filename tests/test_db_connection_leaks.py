"""The database layer closes what it opens.

Run with ResourceWarning as an error: an "unclosed database" from this layer
fails the module instead of scrolling past in the warnings summary.
"""
import gc
import sqlite3
import threading
import time
import warnings

import pytest

from nfl_mcp import database
from nfl_mcp.database import ConnectionPoolConfig, NFLDatabase

pytestmark = pytest.mark.filterwarnings("error::ResourceWarning")


def _resource_warnings(fn) -> list:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        fn()
        gc.collect()
    return [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_a_closed_database_leaves_nothing_open(tmp_path):
    pools = []

    def use():
        db = NFLDatabase(str(tmp_path / "a.db"))
        pools.append(db._pool)
        db.get_athletes_by_team("KC")
        assert db._pool.open_connections > 0
        assert database.open_connection_count() >= db._pool.open_connections
        db.close()

    assert _resource_warnings(use) == []
    assert pools[0].open_connections == 0


def test_the_check_would_catch_a_leak(tmp_path):
    """Control: a connection dropped unclosed does warn, so the checks here
    mean something."""
    db = NFLDatabase(str(tmp_path / "b.db"))

    def leak():
        db._pool._create_connection()  # opened outside the pool's bookkeeping

    assert _resource_warnings(leak)
    db.close()


def test_a_dropped_database_closes_its_connections(tmp_path):
    """No close() at all: the pool's finalizer closes the idle connections
    instead of leaving them to the garbage collector one by one."""
    def drop():
        db = NFLDatabase(str(tmp_path / "g.db"))
        db.get_athletes_by_team("KC")

    assert _resource_warnings(drop) == []


def test_context_manager_closes(tmp_path):
    def use():
        with NFLDatabase(str(tmp_path / "c.db")) as db:
            db.get_athletes_by_team("KC")

    assert _resource_warnings(use) == []


def test_a_connection_borrowed_during_close_is_closed_on_return(tmp_path):
    db = NFLDatabase(str(tmp_path / "d.db"))
    pool = db._pool
    with pool.get_connection() as conn:
        db.close()
        conn.execute("SELECT 1")  # still usable by its borrower
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # closed on return, not pooled
    assert pool.open_connections == 0
    # The pool reopens on demand after close().
    assert db.get_athletes_by_team("KC") == []
    db.close()
    assert pool.open_connections == 0


def test_borrowing_past_the_idle_connections_does_not_stall(tmp_path):
    """The pool starts with two idle connections. A third borrower used to
    block 5 s on the empty queue before opening the connection the limit
    allowed -- on the event loop's thread."""
    db = NFLDatabase(str(tmp_path / "e.db"), ConnectionPoolConfig(max_connections=5))
    t0 = time.perf_counter()
    with db._pool.get_connection(), db._pool.get_connection(), db._pool.get_connection() as c3:
        c3.execute("SELECT 1")
    assert time.perf_counter() - t0 < 1.0
    db.close()


def test_threads_share_the_pool_without_leaking(tmp_path):
    db = NFLDatabase(str(tmp_path / "f.db"))

    def work():
        for _ in range(20):
            db.get_athletes_by_team("KC")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert db._pool.open_connections <= db.pool_config.max_connections
    assert _resource_warnings(db.close) == []
    assert db._pool.open_connections == 0
