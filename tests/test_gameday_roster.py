"""The ESPN game-day roster: the complete inactive list per team.

Shapes are trimmed copies of the live core-API payload for 2026 week 5
(``events/401872982/competitions/401872982/competitors/4/roster``: CIN at MIA,
seven ``didNotPlay`` entries); the feed answers 404 before the roster is set.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nfl_mcp import gameday_inactives as gi
from nfl_mcp import server

KICKOFF = datetime(2026, 10, 11, 17, 0, tzinfo=UTC)
IN_WINDOW = KICKOFF - timedelta(minutes=80)
CIN_ROSTER = {"entries": [
    {"playerId": 11252, "displayName": "Flacco", "jersey": "16", "didNotPlay": True, "valid": True},
    {"playerId": 5092508, "displayName": "Young", "jersey": "86", "didNotPlay": True},
    {"playerId": 3915416, "displayName": "Burrow", "jersey": "9", "didNotPlay": False},
    {"playerId": 4362628, "displayName": "Chase", "jersey": "1", "didNotPlay": False},
]}
NOTES = {"injuries": [{"displayName": "Cincinnati Bengals", "injuries": [
    {"shortComment": "Young (knee) is inactive for Sunday's game versus the Dolphins.",
     "date": "2026-10-11T16:42Z",
     "athlete": {"id": "5092508", "displayName": "Colbie Young", "position": {"abbreviation": "WR"}}}]}]}


def test_parse_needs_a_declared_inactive():
    assert gi.parse_espn_game_roster(None) is None
    assert gi.parse_espn_game_roster({"entries": []}) is None
    # A roster set before the declaration (nobody flagged) is not published.
    assert gi.parse_espn_game_roster({"entries": [
        {"playerId": 1, "displayName": "A", "didNotPlay": False}]}) is None
    rows = gi.parse_espn_game_roster(CIN_ROSTER)
    assert [r["name"] for r in rows if r["inactive"]] == ["Flacco", "Young"]
    assert len(rows) == 4


def window_db():
    db = MagicMock()
    db.get_week_kickoffs = MagicMock(return_value={"CIN": KICKOFF.isoformat(),
                                                    "MIA": KICKOFF.isoformat()})
    db.get_week_espn_ids = MagicMock(return_value={
        "CIN": {"event_id": "401872982", "team_id": "4"},
        "MIA": {"event_id": "401872982", "team_id": "15"}})
    # Sleeper knows Flacco's and Burrow's ESPN ids; Young and Chase are
    # matched on jersey + last name.
    db.get_athletes_by_espn_ids = MagicMock(return_value={
        "11252": {"id": "S1", "full_name": "Joe Flacco", "position": "QB"},
        "3915416": {"id": "S2", "full_name": "Joe Burrow", "position": "QB"}})
    db.get_athletes_by_team = MagicMock(side_effect=lambda team: [
        {"id": "S3", "full_name": "Colbie Young", "position": "WR", "team_id": "CIN",
         "raw": {"number": 86, "last_name": "Young"}},
        {"id": "S4", "full_name": "Ja'Marr Chase", "position": "WR", "team_id": "CIN",
         "raw": {"number": 1, "last_name": "Chase"}},
    ] if team == "CIN" else [])
    return db


def client_for(calls, delay=0.0, roster_status=None):
    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "competitors/4/roster" in url:
            await asyncio.sleep(delay)
            return httpx.Response(roster_status or 200, json=CIN_ROSTER)
        if "competitors/15/roster" in url:
            return httpx.Response(404, json={"error": {"code": 404}})
        if "sleeper" in url:
            return httpx.Response(200, json={})
        return httpx.Response(200, json=NOTES)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_complete_list_pending_team_and_note_merge():
    calls: list[str] = []
    async with client_for(calls) as client:
        out = await gi.get_official_inactives(window_db(), 2026, 5, now=IN_WINDOW, client=client)
    assert out["complete_teams"] == ["CIN"]
    assert out["pending_roster_teams"] == ["MIA"]
    assert out["published_by_source"][gi.SOURCE_ESPN_ROSTER] == ["CIN"]
    assert gi.SOURCE_ESPN_ROSTER in out["sources_checked"]
    assert out["partial"] is False
    names = {r["player_name"]: r for r in out["inactives"]}
    assert set(names) == {"Joe Flacco", "Colbie Young"}  # the note joined Young's row
    assert names["Colbie Young"]["note"].startswith("Young (knee)")
    assert names["Colbie Young"]["source"] == gi.SOURCE_ESPN_ROSTER
    assert names["Colbie Young"]["player_id"] == "S3"
    assert names["Joe Flacco"]["posted"] == IN_WINDOW.isoformat()  # first seen
    assert {r["player_name"] for r in out["roster_active"]["CIN"]} == {"Joe Burrow", "Ja'Marr Chase"}


async def test_published_list_is_cached_and_pending_is_reasked():
    calls: list[str] = []
    async with client_for(calls) as client:
        await gi.get_official_inactives(window_db(), 2026, 5, now=IN_WINDOW, client=client)
        await gi.get_official_inactives(window_db(), 2026, 5, now=IN_WINDOW + timedelta(minutes=3),
                                        client=client)
    assert sum("competitors/4/roster" in c for c in calls) == 1
    assert sum("competitors/15/roster" in c for c in calls) == 2


async def test_a_slow_roster_is_reported_not_waited_for():
    calls: list[str] = []
    async with client_for(calls, delay=1.0) as client:
        out = await gi.get_official_inactives(window_db(), 2026, 5, now=IN_WINDOW, client=client,
                                              budget_seconds=0.3)
        assert gi.SOURCE_ESPN_ROSTER in out["timed_out_sources"]
        assert out["partial"] is True
        assert out["complete_teams"] == []
        # The note still answers for Young.
        assert [r["player_name"] for r in out["inactives"]] == ["Colbie Young"]
        await asyncio.sleep(1.1)
        again = await gi.get_official_inactives(window_db(), 2026, 5, now=IN_WINDOW, client=client,
                                                budget_seconds=0.3)
    assert again["complete_teams"] == ["CIN"]


async def test_a_failing_roster_feed_is_not_claimed_checked():
    calls: list[str] = []
    db = window_db()
    db.get_week_espn_ids = MagicMock(return_value={"CIN": {"event_id": "401872982", "team_id": "4"}})
    async with client_for(calls, roster_status=500) as client:
        out = await gi.get_official_inactives(db, 2026, 5, now=IN_WINDOW, client=client)
    assert gi.SOURCE_ESPN_ROSTER not in out["sources_checked"]
    assert out["complete_teams"] == []


def test_gameday_index_marks_the_rest_of_a_complete_roster_active():
    official = {"roster_active": {"CIN": [{"player_name": "Ja'Marr Chase"}]},
                "inactives": [{"player_name": "Colbie Young", "team_id": "CIN",
                               "source": gi.SOURCE_ESPN_ROSTER}]}
    index = gi.gameday_index(official)
    from nfl_mcp.opportunity_tools import norm_name
    assert index[(norm_name("Ja'Marr Chase"), "CIN")]["status"] == "active"
    assert index[(norm_name("Colbie Young"), "CIN")]["status"] == "inactive"


async def test_tool_reports_complete_and_per_source_teams():
    from nfl_mcp import tool_registry
    official = {"games": {"CIN": {"kickoff": KICKOFF.isoformat(), "phase": "inactives_window"},
                          "MIA": {"kickoff": KICKOFF.isoformat(), "phase": "inactives_window"}},
                "window_teams": ["CIN", "MIA"],
                "inactives": [{"player_name": "Joe Flacco", "team_id": "CIN",
                               "source": gi.SOURCE_ESPN_ROSTER}],
                "confirmed_active": [], "complete_teams": ["CIN"],
                "published_by_source": {gi.SOURCE_ESPN_ROSTER: ["CIN"]},
                "sources_checked": [gi.SOURCE_ESPN_ROSTER], "timed_out_sources": []}
    with patch("nfl_mcp.tools._context.get_db", return_value=MagicMock()), \
         patch("nfl_mcp.gameday_inactives.get_official_inactives", AsyncMock(return_value=official)), \
         patch("nfl_mcp.injury_service.get_injury_reports", AsyncMock(return_value=[])):
        tool = next(t for t in tool_registry.get_all_tools("full") if t.__name__ == "get_gameday_inactives")
        out = await tool(season=2026, week=5)
    assert out["official_published_teams"] == ["CIN"]
    assert out["official_complete_teams"] == ["CIN"]
    assert out["pending_teams"] == ["MIA"]
    assert out["official_published_by_source"] == {gi.SOURCE_ESPN_ROSTER: ["CIN"]}


class TestWarmPolling:
    def db(self, kickoff=KICKOFF):
        db = MagicMock()
        db.get_week_kickoffs = MagicMock(return_value={"CIN": kickoff.isoformat()})
        return db

    def test_due_from_t_minus_100_to_kickoff(self):
        db = self.db()
        assert gi.warm_due(db, 2026, 5, KICKOFF - timedelta(minutes=101)) is False
        assert gi.warm_due(db, 2026, 5, KICKOFF - timedelta(minutes=99)) is True
        assert gi.warm_due(db, 2026, 5, KICKOFF + timedelta(minutes=1)) is False
        assert gi.next_warm_at(db, 2026, 5, KICKOFF - timedelta(hours=5)) == KICKOFF - gi.WARM_LEAD
        assert gi.next_warm_at(db, 2026, 5, KICKOFF) is None

    async def test_warm_fills_the_projection_cache(self, monkeypatch):
        official = {"window_teams": ["CIN"], "complete_teams": ["CIN"], "partial": False,
                    "inactives": [], "pending_roster_teams": []}
        monkeypatch.setattr(gi, "get_official_inactives", AsyncMock(return_value=official))
        out = await gi.warm_inactives(self.db(), 2026, 5, now=IN_WINDOW)
        assert out is official
        assert gi._gameday_cache[(2026, 5)] == (IN_WINDOW, official)
        assert await gi.warm_inactives(self.db(), 2026, 5, now=KICKOFF - timedelta(hours=4)) is None

    async def test_prefetch_wait_polls_inside_the_window(self, monkeypatch):
        polls = []

        async def warm(db, season, week, now):
            polls.append(now)
            return {}
        monkeypatch.setattr(gi, "warm_due", lambda *a, **k: True)
        monkeypatch.setattr(gi, "warm_inactives", warm)
        monkeypatch.setattr(server, "PREFETCH_INACTIVES_POLL_SECONDS", 0.05)
        await asyncio.wait_for(server._wait_polling_inactives(
            MagicMock(), 2026, 5, asyncio.Event(), 0.3), timeout=2)
        assert len(polls) >= 3

    async def test_prefetch_wait_without_a_window_just_sleeps(self, monkeypatch):
        warm = AsyncMock()
        monkeypatch.setattr(gi, "warm_due", lambda *a, **k: False)
        monkeypatch.setattr(gi, "next_warm_at", lambda *a, **k: None)
        monkeypatch.setattr(gi, "warm_inactives", warm)
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(server._wait_polling_inactives(MagicMock(), 2026, 5, stop, 10), 1)
        warm.assert_not_called()


@pytest.mark.parametrize("name,expected", [("Beckham Jr.", "beckham"), ("Jak. Thomas", "thomas"),
                                           ("DeWalt IV", "dewalt"), ("", "")])
def test_last_name(name, expected):
    assert gi._last_name(name) == expected
