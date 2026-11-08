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
    monkeypatch.setattr(league_changes_tools, "get_shared_db", lambda *a, **k: db)
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


def _athletes():
    rows = {
        "zf": ("Zay Flowers", "WR", "BAL"), "ml": ("Makai Lemon", "WR", "PHI"),
        "ds": ("DeVonta Smith", "WR", "PHI"), "ta": ("Tyler Allgeier", "RB", "ARI"),
        "jl": ("Jordan Love", "QB", "GB"), "mw": ("Malik Willis", "QB", "GB"),
        "jb": ("Joe Burrow", "QB", "CIN"), "kb": ("Kyler Busy", "QB", "ARI"),
    }
    return {pid: {"full_name": n, "position": p, "team_id": t} for pid, (n, p, t) in rows.items()}


_ROLES = {"zf": "my_starter", "ta": "my_starter", "jb": "my_starter", "kb": "my_starter",
          "jl": "my_bench", "mw": "my_bench", "ml": "opp_starter", "ds": "opp_starter"}

# This week's real items (CBS: player / team / position fields, "<Team>'
# <Player>: <blurb>" titles; ESPN: general articles).
_CBS = [
    {"player": "Rashod Bateman", "team": "BAL", "position": "WR",
     "headline": "Ravens' Rashod Bateman: Another one-catch game in loss",
     "description": "Bateman caught one of two targets for six yards in Thursday night's "
                    "33-10 loss to the Jaguars. That's back-to-back one-catch games for Bateman "
                    "with Joe Fagnano at quarterback for the Ravens. With Zay Flowers also "
                    "healthy, Bateman has no fantasy appeal in season-long formats."},
    {"player": "DeVonta Smith", "team": "PHI", "position": "WR",
     "headline": "Eagles' DeVonta Smith: Not present at Friday's practice",
     "description": "Smith (hamstring) was not seen at Friday's practice, Jeff McLane of the "
                    "Philadelphia Inquirer reports. With Smith now absent from practice again "
                    "Friday, he's unlikely to play Sunday against the Giants, leaving Makai "
                    "Lemon, Darius Cooper and Hollywood Brown as the top wide receivers."},
    {"player": "Dontayvion Wicks", "team": "PHI", "position": "WR",
     "headline": "Eagles' Dontayvion Wicks: Still sidelined Friday",
     "description": "Wicks (ankle) is not practicing Friday. With DeVonta Smith (hamstring) "
                    "also injured, the Eagles will likely keep rolling with Makai Lemon and "
                    "Darius Cooper as their every-down wideouts."},
    {"player": "Bam Knight", "team": "ARI", "position": "RB",
     "headline": "Cardinals' Bam Knight: Ruled out for Sunday's game",
     "description": "Knight (concussion) has been ruled out for Sunday's game against Seattle, "
                    "leaving Tyler Allgeier and Evan Hull to handle the backfield work. "
                    "Kyler Busy will hand off to them."},
]
_ESPN = [
    {"headline": "NFL Week 9 preview: Picks, predictions, schedule, betting odds, fantasy tips",
     "description": "Jordan Love and the Packers visit Carolina."},
    {"headline": "Fantasy football last-minute pickups: Kaelon Black and Malik Willis "
                 "lead the way", "description": "Two adds before kickoff."},
    {"headline": "Joe Burrow makes time for art in Madrid ahead of Bengals-Falcons",
     "description": "The Bengals quarterback toured the Prado on Friday."},
]


@pytest.fixture
def feeds(monkeypatch):
    def set_feeds(cbs, espn):
        stamp = _iso(1)
        monkeypatch.setattr(cbs_fantasy_tools, "get_cbs_player_news", AsyncMock(
            return_value={"news": [{**a, "published": stamp} for a in cbs]}))
        monkeypatch.setattr(nfl_tools, "get_nfl_news", AsyncMock(
            return_value={"articles": [{**a, "published": stamp} for a in espn]}))
    return set_feeds


async def _news(feeds, cbs=(), espn=()):
    feeds(list(cbs), list(espn))
    return await league_changes_tools.news_items(NOW - timedelta(hours=24), _ROLES,
                                                 _athletes(), {})


class TestNewsAttribution:
    @pytest.mark.asyncio
    async def test_another_players_note_is_not_his_news(self, feeds):
        # "Zay Flowers: Ravens' Rashod Bateman: Another one-catch game in loss"
        items = await _news(feeds, cbs=_CBS[:1])
        assert not any(i["player"] == "Zay Flowers" for i in items)

    @pytest.mark.asyncio
    async def test_teammate_absence_is_teammate_news(self, feeds):
        items = await _news(feeds, cbs=_CBS[3:])
        # Same team, unrelated position (a QB): not his news.
        assert not any(i["player"] == "Kyler Busy" for i in items)
        assert len(items) == 1
        item = items[0]
        assert item["kind"] == "teammate_news" and item["relation"] == "teammate"
        assert item["player"] == "Tyler Allgeier" and item["subject_player"] == "Bam Knight"
        assert item["summary"].startswith("Tyler Allgeier: teammate Bam Knight ruled out")
        assert "ruled_out" in item["flags"]
        # Below the player's own news weight (40 for a starter), above noise.
        assert 20 <= item["importance"] < 40

    @pytest.mark.asyncio
    async def test_eagles_receivers_get_labelled_teammate_news(self, feeds):
        items = await _news(feeds, cbs=_CBS[1:3])
        mates = [i for i in items if i["kind"] == "teammate_news"]
        assert sorted((i["player"], i["subject_player"]) for i in mates) == [
            ("DeVonta Smith", "Dontayvion Wicks"), ("Makai Lemon", "DeVonta Smith"),
            ("Makai Lemon", "Dontayvion Wicks")]
        summaries = {i["summary"].split(" (")[0] for i in mates}
        assert "Makai Lemon: teammate DeVonta Smith unlikely to play" in summaries
        assert "DeVonta Smith: teammate Dontayvion Wicks sidelined" in summaries
        assert all(i["importance"] < 15 for i in mates)
        # Smith's own note stays his news, as news about himself.
        own = [i for i in items if i["kind"] == "news"]
        assert [(i["player"], i["relation"], i["signal"]) for i in own] == [
            ("DeVonta Smith", "self", "absence")]

    @pytest.mark.asyncio
    async def test_subject_parsed_from_title_without_player_field(self, feeds, db):
        db.upsert_athletes({"bk": {"full_name": "Bam Knight", "position": "RB", "team": "ARI"}})
        bare = {k: v for k, v in _CBS[3].items() if k not in ("player", "team", "position")}
        feeds([], [bare])
        items = await league_changes_tools.news_items(
            NOW - timedelta(hours=24), _ROLES, _athletes(), {}, db)
        assert [(i["kind"], i["player"], i["subject_player"]) for i in items] == [
            ("teammate_news", "Tyler Allgeier", "Bam Knight")]
        # Position unknown and not found: nobody inherits anything.
        assert await _news(feeds, espn=[bare]) == []

    @pytest.mark.asyncio
    async def test_roundups_are_not_per_player_news(self, feeds):
        items = await _news(feeds, espn=_ESPN[:2])
        assert items == []

    @pytest.mark.asyncio
    async def test_no_fantasy_signal_is_low_importance(self, feeds):
        items = await _news(feeds, espn=_ESPN[2:])
        assert len(items) == 1
        assert items[0]["kind"] == "news" and items[0]["signal"] == "none"
        assert items[0]["importance"] <= 10

    @pytest.mark.asyncio
    async def test_own_news_ranked_by_signal(self, feeds):
        own = [
            {"player": "Tyler Allgeier", "team": "ARI", "position": "RB",
             "headline": "Cardinals' Tyler Allgeier: Ruled out for Sunday's game",
             "description": "Allgeier (ankle) has been ruled out."},
            {"player": "Joe Burrow", "team": "CIN", "position": "QB",
             "headline": "Bengals' Joe Burrow: Four touchdowns in win",
             "description": "Burrow completed 24 of 29 passes for 271 yards and three "
                            "touchdowns."},
        ]
        items = {i["player"]: i for i in await _news(feeds, cbs=own)}
        assert items["Tyler Allgeier"]["kind"] == "news"
        assert items["Tyler Allgeier"]["signal"] == "absence"
        assert items["Tyler Allgeier"]["relation"] == "self"
        assert items["Joe Burrow"]["signal"] == "performance"
        assert items["Tyler Allgeier"]["importance"] > items["Joe Burrow"]["importance"] > 10

    def test_signal_reads(self):
        sig = league_changes_tools.news_signal
        assert sig("Smith was suspended three games")[0] == "absence"
        assert sig("Wicks (ankle) is listed as out for Sunday's game")[0] == "absence"
        assert sig("Love was placed on injured reserve")[0] == "absence"
        assert sig("Lemon was a full participant in practice")[0] == "availability"
        assert sig("Joe Burrow makes time for art in Madrid")[0] == "none"
