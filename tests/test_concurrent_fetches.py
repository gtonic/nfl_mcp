"""The daily tools issue their independent network reads together.

A weekly briefing used to await a dozen Open-Meteo requests one after another
(each able to stall for the full client timeout), then league, rosters and
matchups in turn — ~60s a call. These pin the concurrency and the short-lived
caches that brought it down, so a later edit cannot quietly re-serialize them.
"""
import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nfl_mcp import projections as pj
from nfl_mcp import sleeper_tools
from nfl_mcp.database import NFLDatabase


class _InFlight:
    """Counts how many wrapped calls are awaiting at the same time."""

    def __init__(self):
        self.now = 0
        self.peak = 0

    def wrap(self, result):
        async def call(*_a, **_k):
            self.now += 1
            self.peak = max(self.peak, self.now)
            await asyncio.sleep(0.02)
            self.now -= 1
            return result() if callable(result) else result
        return call


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        database.upsert_schedule_games([
            {"season": 2026, "week": 5, "team": "BUF", "opponent": "NYJ",
             "is_home": 1, "kickoff": "2026-10-04T17:00Z"},
            {"season": 2026, "week": 5, "team": "NYJ", "opponent": "BUF",
             "is_home": 0, "kickoff": "2026-10-04T17:00Z"},
        ])
        database.upsert_athletes({"on": {"full_name": "Playing WR", "position": "WR",
                                         "team": "BUF"}})
        yield database


@pytest.mark.asyncio
async def test_briefing_fetches_league_rosters_matchups_and_weather_together(monkeypatch, db):
    from nfl_mcp import briefing_tools, projections, weather_tools

    flight = _InFlight()
    monkeypatch.setattr(briefing_tools, "get_shared_db", lambda *a, **k: db)
    monkeypatch.setattr(sleeper_tools, "get_league", flight.wrap(
        {"league": {"total_rosters": 10, "scoring_settings": {"rec": 1},
                    "roster_positions": ["WR", "BN"], "settings": {}}}))
    monkeypatch.setattr(sleeper_tools, "get_rosters", flight.wrap(
        {"rosters": [{"roster_id": 1, "players": ["on"], "starters": ["on"]}]}))
    monkeypatch.setattr(sleeper_tools, "get_matchups", flight.wrap(
        {"matchups": [{"roster_id": 1, "matchup_id": 1, "starters": ["on"],
                       "players_points": {}}]}))
    monkeypatch.setattr(weather_tools, "get_weather_forecast", flight.wrap({"games": []}))

    async def _project(players, **_):
        return {"projections": [
            {"player": p["name"], "position": p["position"], "team": p["team"],
             "opponent": p["opponent"], "projected_points": 10.0, "floor": 5.0,
             "ceiling": 15.0} for p in players
        ]}
    monkeypatch.setattr(projections, "project_players", _project)

    out = await briefing_tools.get_weekly_briefing("L", roster_id=1, week=5, season=2026)
    assert out["week"] == 5 and out["roster_id"] == 1
    assert flight.peak == 4


@pytest.mark.asyncio
async def test_projection_inputs_are_fetched_together():
    flight = _InFlight()
    engine = pj.ProjectionEngine.__new__(pj.ProjectionEngine)
    engine.db = None
    engine.values = Mock(get_values=flight.wrap({"list": [], "source": "stub"}))
    engine.defense = Mock(fetch_defense_rankings=flight.wrap({}))
    engine.vegas = Mock(fetch_current_lines=flight.wrap({}))
    with patch.object(pj.opportunity_tools, "_fetch_game_logs", new=flight.wrap({})):
        out = await engine.project_many(
            [{"name": "X", "position": "WR", "team": "BUF", "opponent": "NYJ"}],
            season=2026, week=5,
        )
    assert out["projections"]
    assert flight.peak == 4


@pytest.mark.asyncio
async def test_a_failed_side_input_still_degrades_to_neutral():
    engine = pj.ProjectionEngine.__new__(pj.ProjectionEngine)
    engine.db = None
    engine.values = Mock(get_values=AsyncMock(return_value={"list": [], "source": "stub"}))
    engine.defense = Mock(fetch_defense_rankings=AsyncMock(side_effect=RuntimeError("down")))
    engine.vegas = Mock()  # not even awaitable: must still land as "no lines"
    out = await engine.project_many(
        [{"name": "X", "position": "WR", "team": "BUF", "opponent": "NYJ"}], week=1,
    )
    assert out["vegas_active"] is False
    assert out["projections"]


@pytest.mark.asyncio
async def test_league_changes_reads_both_transaction_weeks_together(monkeypatch):
    from nfl_mcp import league_changes_tools

    flight = _InFlight()
    monkeypatch.setattr(sleeper_tools, "get_transactions",
                        flight.wrap({"transactions": []}))
    items = await league_changes_tools.transaction_items(
        "L", [2, 3], since_dt=datetime.now(UTC), my_rid=1, opp_rid=2, owners={},
        db=None, errors={})
    assert items == []
    assert flight.peak == 2


def _state_client(payload):
    resp = Mock(status_code=200)
    resp.json = Mock(return_value=payload)
    resp.raise_for_status = Mock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_nfl_state_is_fetched_once_per_minute():
    client = _state_client({"season": "2026", "week": 3})
    with patch("nfl_mcp.sleeper_tools.create_http_client", return_value=client):
        first = await sleeper_tools.get_nfl_state()
        first["nfl_state"]["week"] = 99  # a caller mutating its copy
        second = await sleeper_tools.get_nfl_state()
    assert client.get.await_count == 1
    assert second["nfl_state"]["week"] == 3


@pytest.mark.asyncio
async def test_a_failed_nfl_state_is_not_cached():
    bad = _state_client({})
    bad.get.side_effect = RuntimeError("down")
    good = _state_client({"season": "2026", "week": 3})
    with patch("nfl_mcp.sleeper_tools.create_http_client", side_effect=[bad, good]):
        assert (await sleeper_tools.get_nfl_state()).get("success") is False
        assert (await sleeper_tools.get_nfl_state())["nfl_state"]["week"] == 3
