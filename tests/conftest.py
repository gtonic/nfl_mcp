"""Shared pytest configuration.

Defines the ``live`` marker used to tag tests that hit real external APIs
(Sleeper, ESPN, ...). Those are skipped by default so the unit suite runs
offline and deterministically; pass ``--run-live`` to include them. Their
coverage is otherwise provided by the data-source contracts watchdog.
"""
import socket

import pytest

_AF_UNIX = getattr(socket, "AF_UNIX", None)


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Also run tests marked @pytest.mark.live that hit real external APIs.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(
        reason="hits a live external API; pass --run-live to enable"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _ros_offline(monkeypatch):
    """Keep the rest-of-season engine off the network in the unit suite.

    ROS fetches any schedule week that is not cached and the defense rankings;
    tests that need either patch `nfl_mcp.ros` themselves.
    """
    from nfl_mcp import ros

    async def _no_schedule(*_a, **_k):
        return []

    async def _no_rankings(*_a, **_k):
        return {}

    monkeypatch.setattr(ros, "_fetch_week_schedule", _no_schedule)
    monkeypatch.setattr(ros, "_defense_rankings", _no_rankings)


@pytest.fixture(autouse=True)
def _opponent_league_offline(monkeypatch):
    """Opponent analysis reads the league's roster positions; keep it offline
    (None = assess every position). Tests of the slot filter patch it."""
    from nfl_mcp import opponent_analysis_tools

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(opponent_analysis_tools, "_league_roster_positions", _none)


@pytest.fixture(autouse=True)
def _no_unit_offense_read(monkeypatch):
    """Keep the projection engine's K/DEF offense fallback off the network.

    Without Vegas lines it reads nflverse offense rankings; tests of the
    fallback patch `projections._unit_matchup` themselves.
    """
    from nfl_mcp import projections

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(projections, "_unit_matchup", _none)


@pytest.fixture(autouse=True)
def _no_sleeper_second_opinion(monkeypatch):
    """Keep the Sleeper-projection second opinion offline in unit tests.

    It is fetched from every projection and start/sit call; without this each
    of those tests would make a real request. Tests of the second opinion
    itself patch `sleeper_projections._fetch` (or the index) explicitly.
    """
    from nfl_mcp import sleeper_projections

    async def _empty(season, week):
        return {"by_id": {}, "by_name": {}, "by_def": {}}

    monkeypatch.setattr(sleeper_projections, "_fetch", _empty)
    sleeper_projections._cache.clear()


@pytest.fixture(autouse=True)
def _clear_process_caches():
    """Short-lived in-process caches must not leak one test's mocks into the next."""
    from nfl_mcp import handcuff_tools, lineup_tools, nfl_tools, sleeper_tools, weather_tools
    sleeper_tools.clear_nfl_state_cache()
    weather_tools.clear_forecast_cache()
    nfl_tools.clear_season_stats_cache()
    handcuff_tools.clear_depth_chart_cache()
    lineup_tools.clear_usage_cache()
    yield
    sleeper_tools.clear_nfl_state_cache()
    weather_tools.clear_forecast_cache()
    nfl_tools.clear_season_stats_cache()
    handcuff_tools.clear_depth_chart_cache()
    lineup_tools.clear_usage_cache()


@pytest.fixture(autouse=True)
def _no_fantasycalc(monkeypatch):
    """Player values come from FantasyCalc; unit tests behave as if it is down.

    Tests that need values patch ``_fetch_from_fantasycalc`` on their service
    instance, which takes precedence over this class-level stub.
    """
    import httpx

    from nfl_mcp import player_values

    async def _offline(self, *_a, **_k):
        raise httpx.ConnectError("FantasyCalc is offline in unit tests")

    monkeypatch.setattr(player_values.PlayerValuesService, "_fetch_from_fantasycalc", _offline)


@pytest.fixture
def offline_sources(monkeypatch):
    """Answer the ambient reads of the projection/lineup paths as if offline.

    The NFL state (Sleeper), nflverse game logs, offense/defense rankings and
    the briefing's weather are fetched by nearly every projection, start/sit and briefing call. This
    returns exactly what those fetchers return when the network is down, so a
    test module opts in with ``pytestmark = pytest.mark.usefixtures("offline_sources")``
    and keeps its offline behavior without touching the network. Tests that
    need a real value patch the specific function themselves.
    """
    from nfl_mcp import briefing_tools, matchup_tools, opportunity_tools, sleeper_tools

    async def _no_state(*_a, **_k):
        return {"success": False, "error": "offline", "error_type": "network_error",
                "nfl_state": None}

    async def _empty(*_a, **_k):
        return {}

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(sleeper_tools, "get_nfl_state", _no_state)
    monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", _empty)
    monkeypatch.setattr(matchup_tools, "fetch_offense_rankings", _empty)
    monkeypatch.setattr(matchup_tools.DefenseRankingsAnalyzer, "_fetch_nflverse_rankings", _none)
    monkeypatch.setattr(briefing_tools, "weather_by_team", _empty)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point every default ``NFLDatabase()`` / ``get_shared_db()`` at a temp file.

    Without this, code that falls back to the default path opens ./nfl_data.db
    in the cwd — a developer's real cache — and results depend on whatever it
    holds (e.g. a real bye week in the schedule table).
    """
    from nfl_mcp import database

    monkeypatch.setenv("NFL_MCP_DB_PATH", str(tmp_path / "nfl_data.db"))
    database.reset_shared_db()
    yield
    database.reset_shared_db()


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "", None}


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    """Fail any non-``live`` test that tries to reach a real host.

    DNS lookups and socket connects to anything but loopback raise, and the
    test is failed even if the code under test swallowed the error, so a
    missing mock can't silently turn into a live request (or a slow timeout).
    """
    if "live" in request.keywords:
        yield
        return

    attempts: list[str] = []
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address):
        host = address[0] if isinstance(address, tuple) and address else address
        if isinstance(host, bytes):
            host = host.decode()
        if host in _LOOPBACK_HOSTS or (isinstance(host, str) and host.startswith("127.")):
            return
        attempts.append(str(host))
        raise OSError(f"network access blocked in unit tests (host={host!r}); "
                      "mock it or mark the test @pytest.mark.live")

    def guarded_getaddrinfo(host, *args, **kwargs):
        _check(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):
        if self.family != _AF_UNIX:
            _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if self.family != _AF_UNIX:
            _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
    if attempts:
        pytest.fail(f"test attempted network access: {sorted(set(attempts))}", pytrace=False)


@pytest.fixture
def current_week_2026(monkeypatch):
    """Pin the canonical current NFL week (2026 week 3) for tools that default to it."""
    async def _state(db=None):
        return {"season": 2026, "week": 3, "source": "nfl_state"}
    monkeypatch.setattr("nfl_mcp.week_context.current_season_week", _state)
