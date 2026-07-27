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
