"""Shared pytest configuration.

Defines the ``live`` marker used to tag tests that hit real external APIs
(Sleeper, ESPN, ...). Those are skipped by default so the unit suite runs
offline and deterministically; pass ``--run-live`` to include them. Their
coverage is otherwise provided by the data-source contracts watchdog.
"""
import pytest


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
    from nfl_mcp import sleeper_tools, weather_tools
    sleeper_tools.clear_nfl_state_cache()
    weather_tools.clear_forecast_cache()
    yield
    sleeper_tools.clear_nfl_state_cache()
    weather_tools.clear_forecast_cache()
