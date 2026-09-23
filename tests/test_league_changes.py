""""What changed since I last looked", for one roster.

Every source is mocked; what is under test is the diff: only what is newer
than the last check, only what touches this roster or this week's opponent,
the most important first, and the check time advancing only when asked.
"""
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import (
    cbs_fantasy_tools,
    league_changes_tools,
    nfl_tools,
    sleeper_tools,
    weather_tools,
)
from nfl_mcp import projections as pj
from nfl_mcp.database import NFLDatabase
from nfl_mcp.projection_store import scoring_key

NOW = datetime.now(UTC)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        database.upsert_athletes({
            "rb1": {"full_name": "Star Back", "position": "RB", "team": "BUF",
                    "depth_chart_position": "RB", "depth_chart_order": 1},
            "rb9": {"full_name": "Backup Back", "position": "RB", "team": "BUF",
                    "depth_chart_position": "RB", "depth_chart_order": 2},
            "wr1": {"full_name": "Bench Wide", "position": "WR", "team": "NYJ"},
            "oqb": {"full_name": "Their Passer", "position": "QB", "team": "MIA"},
            "x1": {"full_name": "Waiver Guy", "position": "WR", "team": "NE"},
        })
        database.upsert_schedule_games([
            {"season": 2026, "week": 3, "team": t, "opponent": o, "is_home": 1,
             "kickoff": "2099-09-27T17:00Z"}
            for t, o in (("BUF", "NYJ"), ("NYJ", "BUF"), ("MIA", "NE"), ("NE", "MIA"))
        ])
        database.upsert_injuries([
            {"player_id": "e1", "player_name": "Star Back", "team_id": "BUF",
             "position": "RB", "injury_status": "Out"},
            {"player_id": "e2", "player_name": "Their Passer", "team_id": "MIA",
             "position": "QB", "injury_status": "Questionable"},
        ])
        with database._pool.get_connection() as conn:
            conn.execute("DELETE FROM injury_history")
            rows = [("e1", "BUF", "Questionable", _iso(72)), ("e1", "BUF", "Out", _iso(2)),
                    ("e2", "MIA", "Active", _iso(80)), ("e2", "MIA", "Questionable", _iso(72))]
            for pid, team, status, at in rows:
                conn.execute(
                    "INSERT INTO injury_history(player_id, team_id, injury_status, recorded_at)"
                    " VALUES (?,?,?,?)", (pid, team, status, at))
            conn.commit()
        yield database


@pytest.fixture
def league(monkeypatch, db):
    monkeypatch.setattr(league_changes_tools, "NFLDatabase", lambda *a, **k: db)
    monkeypatch.setattr(league_changes_tools, "current_season_week", AsyncMock(
        return_value={"season": 2026, "week": 3, "source": "nfl_state"}))
    monkeypatch.setattr(sleeper_tools, "get_league", AsyncMock(return_value={"league": {
        "total_rosters": 10, "scoring_settings": {"rec": 0.5},
        "roster_positions": ["RB", "WR", "BN"]}}))
    monkeypatch.setattr(sleeper_tools, "get_rosters", AsyncMock(return_value={"rosters": [
        {"roster_id": 1, "owner_id": "me", "players": ["rb1", "wr1"], "starters": ["rb1"]},
        {"roster_id": 2, "owner_id": "them", "players": ["oqb"], "starters": ["oqb"]},
    ]}))
    monkeypatch.setattr(sleeper_tools, "get_matchups", AsyncMock(return_value={"matchups": [
        {"roster_id": 1, "matchup_id": 4, "starters": ["rb1"]},
        {"roster_id": 2, "matchup_id": 4, "starters": ["oqb"]},
    ]}))
    monkeypatch.setattr(sleeper_tools, "get_league_users", AsyncMock(return_value={"users": [
        {"user_id": "me", "display_name": "Me"}, {"user_id": "them", "display_name": "Rival"}]}))
    ms = lambda h: int((NOW - timedelta(hours=h)).timestamp() * 1000)  # noqa: E731
    monkeypatch.setattr(sleeper_tools, "get_transactions", AsyncMock(return_value={
        "transactions": [
            {"transaction_id": "t-new", "type": "free_agent", "status": "complete",
             "status_updated": ms(3), "roster_ids": [2], "adds": {"x1": 2}, "drops": None},
            {"transaction_id": "t-old", "type": "waiver", "status": "complete",
             "status_updated": ms(100), "roster_ids": [2], "adds": {"x1": 2}, "drops": None},
        ]}))
    monkeypatch.setattr(sleeper_tools, "get_trending_players", AsyncMock(return_value={
        "trending_players": [{"player_id": "rb9", "count": 12000},
                             {"player_id": "x1", "count": 9000}]}))
    monkeypatch.setattr(nfl_tools, "get_nfl_news", AsyncMock(return_value={"articles": [
        {"headline": "Star Back ruled out", "description": "", "published": _iso(1)},
        {"headline": "Star Back old news", "description": "", "published": _iso(90)},
        {"headline": "Somebody else", "description": "", "published": _iso(1)},
    ]}))
    monkeypatch.setattr(cbs_fantasy_tools, "get_cbs_player_news",
                        AsyncMock(return_value={"news": []}))
    monkeypatch.setattr(weather_tools, "get_weather_forecast",
                        AsyncMock(return_value={"games": []}))

    async def fake_project(players, **_):
        return {"projections": [
            {"player": p["name"], "team": p["team"], "position": p["position"],
             "projected_points": 0.0, "floor": 0.0, "ceiling": 0.0} for p in players]}

    monkeypatch.setattr(pj, "project_players", fake_project)


class TestLeagueChanges:
    @pytest.mark.asyncio
    async def test_first_check_reports_the_last_day_ranked(self, league, db):
        key = scoring_key({"scoring_settings": {"rec": 0.5}})
        db.record_projections(2026, 3, key, [{"player_id": "rb1", "projected_points": 14.0,
                                              "floor": 9.0, "ceiling": 19.0}], now=_iso(30))
        out = await league_changes_tools.get_league_changes("L", roster_id=1)
        assert out["success"] is True
        assert out["since_source"] == "default_24h"
        kinds = [c["kind"] for c in out["changes"]]
        # The starter ruled out comes first.
        top = out["changes"][0]
        assert top["kind"] == "injury" and top["player"] == "Star Back"
        assert (top["from_status"], top["to_status"]) == ("Questionable", "Out")
        # The opponent QB's move is older than a day: not repeated.
        assert not any(c.get("player") == "Their Passer" for c in out["changes"])
        assert "news" in kinds and sum(k == "news" for k in kinds) == 1
        tx = [c for c in out["changes"] if c["kind"] == "transaction"]
        assert len(tx) == 1 and "Rival: +Waiver Guy" in tx[0]["summary"]
        backups = [c for c in out["changes"] if c["kind"] == "trending_backup"]
        assert [b["player"] for b in backups] == ["Backup Back"]
        assert backups[0]["availability"] == "free_agent"
        proj = [c for c in out["changes"] if c["kind"] == "projection"]
        assert proj and proj[0]["delta"] == -14.0
        importances = [c["importance"] for c in out["changes"]]
        assert importances == sorted(importances, reverse=True)
        assert out["marked_seen"] is True
        assert db.get_league_last_check("L", 1) == out["checked_at"]

    @pytest.mark.asyncio
    async def test_second_check_starts_from_the_first(self, league, db):
        await league_changes_tools.get_league_changes("L", roster_id=1)
        out = await league_changes_tools.get_league_changes("L", roster_id=1)
        assert out["since_source"] == "last_check"
        assert not any(c["kind"] in ("injury", "news", "transaction") for c in out["changes"])

    @pytest.mark.asyncio
    async def test_peek_does_not_advance_the_check(self, league, db):
        out = await league_changes_tools.get_league_changes("L", user_id="me", mark_seen=False)
        assert out["roster_id"] == 1 and out["marked_seen"] is False
        assert db.get_league_last_check("L", 1) is None

    @pytest.mark.asyncio
    async def test_explicit_since_reaches_further_back(self, league, db):
        out = await league_changes_tools.get_league_changes(
            "L", roster_id=1, since=_iso(200), mark_seen=False)
        assert out["since_source"] == "caller"
        opp = [c for c in out["changes"] if c.get("player") == "Their Passer"]
        assert opp and opp[0]["role"] == "opp_starter"
        assert opp[0]["to_status"] == "Questionable"
        tx = [c for c in out["changes"] if c["kind"] == "transaction"]
        assert len(tx) == 2

    @pytest.mark.asyncio
    async def test_bad_since_is_refused(self, league):
        out = await league_changes_tools.get_league_changes("L", roster_id=1, since="yesterday")
        assert out["success"] is False

    @pytest.mark.asyncio
    async def test_a_failing_source_is_reported_not_fatal(self, league, monkeypatch):
        monkeypatch.setattr(nfl_tools, "get_nfl_news", AsyncMock(side_effect=OSError("down")))
        out = await league_changes_tools.get_league_changes("L", roster_id=1, mark_seen=False)
        assert out["success"] is True
        assert "espn_news" in out["errors"]
        assert out["changes"]

    @pytest.mark.asyncio
    async def test_limit_truncates_and_counts_the_rest(self, league):
        out = await league_changes_tools.get_league_changes(
            "L", roster_id=1, mark_seen=False, limit=1)
        assert len(out["changes"]) == 1
        assert out["omitted"] == sum(out["counts"].values()) - 1


class TestParseTime:
    def test_formats(self):
        p = league_changes_tools._parse_time
        assert p("2026-09-22T10:00:00Z") == datetime(2026, 9, 22, 10, tzinfo=UTC)
        assert p(1758535200000) == datetime.fromtimestamp(1758535200, UTC)
        assert abs((p("3 hours ago") - (datetime.now(UTC) - timedelta(hours=3))).total_seconds()) < 5
        assert p("last week") is None
