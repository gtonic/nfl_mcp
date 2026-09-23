"""Gameday inactives: the published list once it is out, the old severity
filter (clearly labelled) before that. Notes are trimmed copies of the live
ESPN week-2 payload."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nfl_mcp import gameday_inactives as gi
from nfl_mcp import tool_registry

KICKOFF = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
KICKOFFS = {"JAX": KICKOFF.isoformat(), "DEN": KICKOFF.isoformat(),
            "KC": (KICKOFF + timedelta(hours=3, minutes=25)).isoformat()}

PAYLOAD = {"injuries": [
    {"displayName": "Jacksonville Jaguars", "injuries": [
        {"shortComment": "Koziol (coach's decision) is inactive for Sunday's game against the Broncos.",
         "date": "2026-09-20T15:27Z", "athlete": {"displayName": "Tanner Koziol",
                                                  "position": {"abbreviation": "TE"}}},
        {"shortComment": "Etienne (ankle) is active for Sunday's game against the Broncos.",
         "date": "2026-09-20T15:30Z", "athlete": {"displayName": "Travis Etienne Jr."}},
        # Last week's note: outside this game's window.
        {"shortComment": "Old Guy (knee) is inactive for Sunday's game.",
         "date": "2026-09-13T15:30Z", "athlete": {"displayName": "Old Guy"}},
    ]},
]}


def test_game_phase():
    assert gi.game_phase(KICKOFF.isoformat(), KICKOFF - timedelta(hours=3)) == "upcoming"
    assert gi.game_phase(KICKOFF.isoformat(), KICKOFF - timedelta(minutes=80)) == "inactives_window"
    assert gi.game_phase(KICKOFF.isoformat(), KICKOFF + timedelta(hours=1)) == "in_progress"
    assert gi.game_phase(KICKOFF.isoformat(), KICKOFF + timedelta(hours=5)) == "final"
    assert gi.game_phase(None) == "unknown"


def test_notes_are_split_and_windowed():
    inactive, active = gi.parse_espn_gameday_notes(PAYLOAD, KICKOFFS, {"JAX"})
    assert [r["player_name"] for r in inactive] == ["Tanner Koziol"]
    assert inactive[0]["official"] is True and inactive[0]["source"] == gi.SOURCE_ESPN_NOTE
    assert [r["player_name"] for r in active] == ["Travis Etienne Jr."]


def test_sleeper_inactive_status():
    players = {"1": {"full_name": "A B", "team": "JAX", "injury_status": "Inactive"},
               "2": {"full_name": "C D", "team": "JAX", "injury_status": "Questionable"},
               "3": {"full_name": "E F", "team": "KC", "injury_status": "Inactive"}}
    rows = gi.parse_sleeper_inactives(players, {"JAX"})
    assert [r["player_name"] for r in rows] == ["A B"]


async def test_nothing_is_fetched_before_any_window_opens():
    db = MagicMock()
    db.get_week_kickoffs = MagicMock(return_value=KICKOFFS)
    client = MagicMock()
    client.get = AsyncMock()
    out = await gi.get_official_inactives(db, 2026, 2, now=KICKOFF - timedelta(hours=5), client=client)
    assert out["window_teams"] == []
    client.get.assert_not_called()


async def test_official_list_inside_the_window():
    db = MagicMock()
    db.get_week_kickoffs = MagicMock(return_value=KICKOFFS)

    async def _get(url, **_):
        r = MagicMock(status_code=200)
        r.json = MagicMock(return_value=PAYLOAD if "espn" in url else {})
        return r

    client = MagicMock()
    client.get = AsyncMock(side_effect=_get)
    out = await gi.get_official_inactives(db, 2026, 2, now=KICKOFF - timedelta(minutes=60), client=client)
    assert out["window_teams"] == ["DEN", "JAX"]
    assert out["games"]["KC"]["phase"] == "upcoming"
    assert [r["player_name"] for r in out["inactives"]] == ["Tanner Koziol"]


INJURIES = [
    {"player_id": "9", "player_name": "Hurt Chief", "team_id": "KC", "injury_status": "Out",
     "severity": 4, "confidence": 90},
    {"player_id": "8", "player_name": "Hurt Jag", "team_id": "JAX", "injury_status": "Out",
     "severity": 4, "confidence": 90},
]


@pytest.fixture
def patched():
    official = {
        "games": {"JAX": {"kickoff": KICKOFFS["JAX"], "phase": "inactives_window"},
                  "KC": {"kickoff": KICKOFFS["KC"], "phase": "upcoming"}},
        "window_teams": ["JAX"],
        "inactives": [{"player_name": "Tanner Koziol", "team_id": "JAX", "position": "TE",
                       "status": "Inactive", "note": "is inactive", "posted": "x",
                       "source": gi.SOURCE_ESPN_NOTE, "official": True}],
        "confirmed_active": [{"player_name": "Travis Etienne Jr.", "team_id": "JAX", "note": "is active"}],
    }
    db = MagicMock()
    db.get_athletes_by_ids = MagicMock(return_value={
        "s1": {"full_name": "Tanner Koziol", "team_id": "JAX"},
        "s2": {"full_name": "Hurt Chief", "team_id": "KC"},
        "s3": {"full_name": "Travis Etienne Jr.", "team_id": "JAX"},
    })
    with patch.object(tool_registry, "get_db", return_value=db), \
         patch("nfl_mcp.gameday_inactives.get_official_inactives", AsyncMock(return_value=official)), \
         patch("nfl_mcp.injury_service.get_injury_reports", AsyncMock(return_value=INJURIES)), \
         patch.object(tool_registry.sleeper_tools, "get_matchups",
                      AsyncMock(return_value={"matchups": [{"roster_id": 7, "starters": ["s1", "s2", "s3"]}]})):
        yield


async def test_tool_mixes_official_and_labelled_fallback(patched):
    out = await tool_registry.get_gameday_inactives(season=2026, week=2)
    assert out["success"] is True
    assert out["mode"] == "mixed"
    assert out["official_published_teams"] == ["JAX"]
    names = {r["player_name"]: r for r in out["inactives"]}
    assert names["Tanner Koziol"]["official"] is True
    assert names["Hurt Chief"]["official"] is False
    assert names["Hurt Chief"]["basis"] == "injury_report_severity"
    # A published team's list replaces its severity guesses.
    assert "Hurt Jag" not in names


async def test_tool_flags_my_starters(patched):
    out = await tool_registry.get_gameday_inactives(
        season=2026, week=2, league_id="1312017357782155264", roster_id=7)
    mine = out["my_starters"]
    assert [p["player"] for p in mine["inactive"]] == ["Tanner Koziol"]
    assert [p["player"] for p in mine["at_risk"]] == ["Hurt Chief"]
    assert [p["player"] for p in mine["confirmed_active"]] == ["Travis Etienne Jr."]


async def test_fallback_only_before_lists_are_out():
    official = {"games": {}, "window_teams": [], "inactives": [], "confirmed_active": []}
    with patch.object(tool_registry, "get_db", return_value=MagicMock()), \
         patch("nfl_mcp.gameday_inactives.get_official_inactives", AsyncMock(return_value=official)), \
         patch("nfl_mcp.injury_service.get_injury_reports", AsyncMock(return_value=INJURIES)):
        out = await tool_registry.get_gameday_inactives(season=2026, week=3)
    assert out["mode"] == "fallback_injury_report"
    assert "fallback_note" in out
    assert out["total_inactives"] == 2
    assert all(r["official"] is False for r in out["inactives"])
