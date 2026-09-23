"""Grading a finished week: projections logged before kickoff, hindsight lineup.

The retro is only honest if "projected" means what was projected before the
games, so most of this is about the log: first pre-kickoff value wins, nothing
is written once a game has started, and a week that was never logged is
re-projected and says so.
"""
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import projections as pj
from nfl_mcp import retro_tools, sleeper_tools
from nfl_mcp import week_context as wc
from nfl_mcp.database import NFLDatabase
from nfl_mcp.projection_store import log_projections, scoring_key

ROSTER_POSITIONS = ["QB", "RB", "WR", "FLEX", "BN", "BN", "BN"]
SCORING = {"rec": 0.5}
# The key the test league's projections are filed under.
KEY = scoring_key({"scoring_settings": SCORING})


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        database.upsert_athletes({
            "qb": {"full_name": "Q Back", "position": "QB", "team": "BUF"},
            "rb1": {"full_name": "Run One", "position": "RB", "team": "BUF"},
            "rb2": {"full_name": "Run Two", "position": "RB", "team": "NYJ"},
            "wr1": {"full_name": "Wide One", "position": "WR", "team": "NYJ"},
            "wr2": {"full_name": "Wide Two", "position": "WR", "team": "BUF"},
            "te": {"full_name": "Tight End", "position": "TE", "team": "NYJ"},
            "o1": {"full_name": "Opp Guy", "position": "QB", "team": "MIA"},
        })
        database.upsert_schedule_games([
            {"season": 2026, "week": 2, "team": "BUF", "opponent": "NYJ", "is_home": 1,
             "kickoff": "2026-09-14T17:00Z"},
            {"season": 2026, "week": 2, "team": "NYJ", "opponent": "BUF", "is_home": 0,
             "kickoff": "2026-09-14T17:00Z"},
        ])
        yield database


def _matchups():
    return [
        {"roster_id": 7, "matchup_id": 1, "points": 60.0, "custom_points": None,
         "starters": ["qb", "rb1", "wr1", "te"],
         "players": ["qb", "rb1", "rb2", "wr1", "wr2", "te"],
         "players_points": {"qb": 20.0, "rb1": 5.0, "rb2": 18.0, "wr1": 25.0,
                            "wr2": 1.0, "te": 10.0}},
        {"roster_id": 3, "matchup_id": 1, "points": 70.0, "custom_points": None,
         "starters": ["o1"], "players": ["o1"], "players_points": {"o1": 70.0}},
    ]


@pytest.fixture
def league(monkeypatch, db):
    monkeypatch.setattr(retro_tools, "NFLDatabase", lambda *a, **k: db)
    monkeypatch.setattr(sleeper_tools, "get_league", AsyncMock(return_value={"league": {
        "name": "Test", "total_rosters": 10, "scoring_settings": SCORING,
        "roster_positions": ROSTER_POSITIONS}}))
    monkeypatch.setattr(sleeper_tools, "get_rosters", AsyncMock(return_value={"rosters": [
        {"roster_id": 7, "owner_id": "u7", "players": ["qb", "rb1", "rb2", "wr1", "wr2", "te"],
         "starters": ["qb", "rb1", "wr1", "te"]},
        {"roster_id": 3, "owner_id": "u3", "players": ["o1"], "starters": ["o1"]},
    ]}))
    monkeypatch.setattr(sleeper_tools, "get_matchups",
                        AsyncMock(return_value={"matchups": _matchups()}))


def _log(db, points: dict[str, float], week=2, now="2026-09-13T12:00:00+00:00", key=KEY):
    rows = [{"player_id": pid, "projected_points": p, "floor": p * 0.6,
             "ceiling": p * 1.4} for pid, p in points.items()]
    return db.record_projections(2026, week, key, rows, league_id="L", source="briefing", now=now)


class TestProjectionLog:
    def test_first_value_is_kept_and_unchanged_values_are_not_rewritten(self, db):
        assert _log(db, {"qb": 18.0}) == 1
        assert _log(db, {"qb": 18.0}, now="2026-09-13T13:00:00+00:00") == 0
        assert _log(db, {"qb": 21.0}, now="2026-09-13T14:00:00+00:00") == 1
        first = db.get_logged_projections(2026, 2, KEY, which="first")
        latest = db.get_logged_projections(2026, 2, KEY, which="latest")
        assert first["qb"]["projected_points"] == 18.0
        assert latest["qb"]["projected_points"] == 21.0
        as_of = db.get_logged_projections(2026, 2, KEY, which="latest",
                                          as_of="2026-09-13T13:30:00+00:00")
        assert as_of["qb"]["projected_points"] == 18.0

    def test_nothing_is_logged_after_kickoff(self, db):
        rows = [{"player_id": "qb", "player": "Q Back", "team": "BUF",
                 "projected_points": 30.0, "floor": 20.0, "ceiling": 40.0}]
        # Week 2 kicked off in 2026-09; a projection made now is post-game.
        assert log_projections(db, 2026, 2, SCORING, rows, source="briefing") == 0
        future = {"BUF": {"kickoff": "2099-01-01T00:00Z"}}
        assert log_projections(db, 2026, 2, SCORING, rows, source="briefing", games=future) == 1
        assert db.get_logged_projections(2026, 2, KEY)["qb"]["ppr"] == 0.5

    def test_scoring_formats_do_not_mix(self, db):
        _log(db, {"qb": 18.0})
        assert db.get_logged_projections(2026, 2, scoring_key("ppr")) == {}

    def test_same_reception_value_different_scoring_is_a_different_key(self):
        # VLBG and Ropeway are both 0.5 PPR but price passing differently; a
        # QB projected under one must not be graded under the other.
        base = {"pass_yd": 0.04, "pass_td": 4, "rush_yd": 0.1, "rush_td": 6,
                "rec_yd": 0.1, "rec_td": 6, "rec": 0.5}
        vlbg = {**base, "pass_yd": 0.05, "pass_int": -2}
        ropeway = {**base, "pass_sack": -1}
        keys = {scoring_key({"scoring_settings": s}) for s in (base, vlbg, ropeway)}
        assert len(keys) == 3
        # Stable, and blind to an explicit zero vs an omitted key.
        assert scoring_key({"scoring_settings": vlbg}) == scoring_key(
            {"scoring_settings": {**vlbg, "fum": 0}})


class TestRetro:
    @pytest.mark.asyncio
    async def test_stored_projections_and_hindsight_lineup(self, league, db):
        _log(db, {"qb": 18.0, "rb1": 15.0, "wr1": 12.0, "te": 8.0,
                  "rb2": 9.0, "wr2": 7.0, "o1": 20.0})
        out = await retro_tools.get_weekly_retro("L", roster_id=7, week=2, season=2026)
        assert out["success"] is True
        assert out["projection_source"] == "stored"
        assert out["result"] == {"points": 60.0, "opponent_points": 70.0,
                                 "outcome": "loss", "margin": -10.0}
        # rb2 (18) belonged at RB over rb1 (5); te (10) still beats both
        # leftovers for the FLEX.
        h = out["hindsight"]
        assert h["points_left_on_bench"] == 13.0
        assert h["optimal_points"] == 73.0
        assert [s["player"] for s in h["should_have_started"]] == ["Run Two"]
        assert [s["player"] for s in h["should_have_sat"]] == ["Run One"]
        # 73 would have beaten 70.
        assert h["optimal_outcome"] == "win"
        assert h["would_have_flipped"] is True
        assert out["projected_total"] == 53.0
        assert out["biggest_misses"][0]["player"] == "Run One"
        assert out["biggest_misses"][0]["diff"] == -10.0
        assert out["biggest_hits"][0]["player"] == "Wide One"
        assert out["opponent"]["projected"] == 20.0

    @pytest.mark.asyncio
    async def test_no_flip_when_even_the_best_lineup_loses(self, league, db, monkeypatch):
        m = _matchups()
        m[1]["points"] = 80.0
        monkeypatch.setattr(sleeper_tools, "get_matchups", AsyncMock(return_value={"matchups": m}))
        _log(db, {"qb": 18.0, "rb1": 15.0, "wr1": 12.0, "te": 8.0})
        out = await retro_tools.get_weekly_retro("L", roster_id=7, week=2, season=2026)
        assert out["result"]["outcome"] == "loss"
        assert out["hindsight"]["optimal_outcome"] == "loss"
        assert out["hindsight"]["would_have_flipped"] is False

    @pytest.mark.asyncio
    async def test_unlogged_week_is_recomputed_and_labelled(self, league, db, monkeypatch):
        calls = []

        async def fake_project(players, **kw):
            calls.append(kw)
            return {"projections": [
                {"player": p["name"], "team": p["team"], "position": p["position"],
                 "projected_points": 10.0, "floor": 6.0, "ceiling": 14.0} for p in players]}

        # Hurt since: today's status must not zero the week he played.
        db.upsert_athletes({"qb": {"full_name": "Q Back", "position": "QB", "team": "BUF",
                                   "injury_status": "Out"}})
        seen = []

        async def spy(players, **kw):
            seen.extend(players)
            return await fake_project(players, **kw)

        monkeypatch.setattr(pj, "project_players", spy)
        out = await retro_tools.get_weekly_retro("L", roster_id=7, week=2, season=2026)
        assert seen and not any("injury" in p for p in seen)
        assert out["projection_source"] == "recomputed"
        assert all(s["projection_source"] == "recomputed" for s in out["starters"])
        assert out["notes"]
        # After-the-fact numbers never reach the log.
        assert "db" not in calls[0]
        assert db.get_logged_projections(2026, 2, KEY) == {}

    @pytest.mark.asyncio
    async def test_another_scorings_log_is_not_read(self, league, db, monkeypatch):
        async def fake_project(players, **kw):
            # The re-projection is priced in the league's own scoring.
            assert kw["scoring"].model.fingerprint == KEY
            return {"projections": [
                {"player": p["name"], "team": p["team"], "position": p["position"],
                 "projected_points": 10.0, "floor": 6.0, "ceiling": 14.0} for p in players]}

        monkeypatch.setattr(pj, "project_players", fake_project)
        _log(db, {"qb": 18.0, "rb1": 15.0, "wr1": 12.0, "te": 8.0}, key=scoring_key("ppr"))
        out = await retro_tools.get_weekly_retro("L", roster_id=7, week=2, season=2026)
        assert out["projection_source"] == "recomputed"
        assert out["league"]["scoring_key"] == KEY

    @pytest.mark.asyncio
    async def test_calibration_over_logged_weeks(self, league, db):
        _log(db, {"qb": 18.0, "rb1": 15.0, "wr1": 12.0, "te": 8.0})
        out = await retro_tools.get_weekly_retro("L", roster_id=7, week=2, season=2026)
        cal = out["calibration"]
        assert cal["weeks"] == [2]
        assert cal["n"] == 4
        # errors: +2, -10, +13, +2
        assert cal["mean_error"] == 1.75
        assert cal["mean_abs_error"] == 6.75
        # floor/ceiling at 0.6x/1.4x: qb, te within; rb1, wr1 outside
        assert cal["within_range_share"] == 0.5

    @pytest.mark.asyncio
    async def test_default_week_is_the_last_completed_one(self, league, db, monkeypatch):
        monkeypatch.setattr(retro_tools, "last_completed_week",
                            AsyncMock(return_value={"season": 2026, "week": 2,
                                                    "source": "nfl_state"}))
        _log(db, {"qb": 18.0})
        out = await retro_tools.get_weekly_retro("L", user_id="u7", include_calibration=False)
        assert (out["season"], out["week"], out["roster_id"]) == (2026, 2, 7)
        assert out["calibration"] is None

    @pytest.mark.asyncio
    async def test_no_completed_week(self, league, monkeypatch):
        monkeypatch.setattr(retro_tools, "last_completed_week",
                            AsyncMock(return_value={"season": 2026, "week": 0,
                                                    "source": "nfl_state"}))
        out = await retro_tools.get_weekly_retro("L", roster_id=7)
        assert out["success"] is False


class TestSeasonWeekFallback:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(wc, "_last_state", None)

    @pytest.mark.asyncio
    async def test_live_state_is_used_and_remembered(self, monkeypatch):
        monkeypatch.setattr(sleeper_tools, "get_nfl_state", AsyncMock(
            return_value={"success": True, "nfl_state": {"season": "2026", "week": 3}}))
        assert await wc.current_season_week() == {"season": 2026, "week": 3,
                                                  "source": "nfl_state"}
        monkeypatch.setattr(sleeper_tools, "get_nfl_state", AsyncMock(
            return_value={"success": False, "nfl_state": None}))
        assert await wc.current_season_week() == {"season": 2026, "week": 3,
                                                  "source": "cached_state"}

    @pytest.mark.asyncio
    async def test_outage_falls_back_to_the_schedule_never_season_zero(self, monkeypatch, db):
        monkeypatch.setattr(sleeper_tools, "get_nfl_state", AsyncMock(side_effect=OSError("down")))
        db.upsert_schedule_games([
            {"season": 2026, "week": 3, "team": "BUF", "opponent": "NYJ", "is_home": 1,
             "kickoff": "2099-09-21T17:00Z"},
        ])
        got = await wc.current_season_week(db)
        # Week 2 is over (2026-09-14); week 3's game is still to come.
        assert got == {"season": 2026, "week": 3, "source": "schedule"}

    @pytest.mark.asyncio
    async def test_no_schedule_uses_the_calendar(self, monkeypatch):
        monkeypatch.setattr(sleeper_tools, "get_nfl_state", AsyncMock(
            return_value={"success": False, "nfl_state": None}))
        got = await wc.current_season_week(None)
        assert got["source"] == "calendar" and got["season"] >= 2026 and got["week"] >= 1

    def test_calendar_weeks(self):
        from datetime import UTC, datetime
        # 2026: Labor Day is Sept 7, the season opens Sept 9/10.
        assert wc.infer_from_calendar(datetime(2026, 8, 20, tzinfo=UTC)) == (2026, 1)
        assert wc.infer_from_calendar(datetime(2026, 9, 12, tzinfo=UTC)) == (2026, 1)
        assert wc.infer_from_calendar(datetime(2026, 9, 23, tzinfo=UTC)) == (2026, 3)
        assert wc.infer_from_calendar(datetime(2027, 1, 20, tzinfo=UTC)) == (2026, 18)

    @pytest.mark.asyncio
    async def test_last_completed_week_steps_back_while_the_week_is_on(self, monkeypatch, db):
        monkeypatch.setattr(sleeper_tools, "get_nfl_state", AsyncMock(
            return_value={"success": True, "nfl_state": {"season": "2026", "week": 2}}))
        # Week 2's only cached game was in 2026-09 — final.
        assert (await wc.last_completed_week(db))["week"] == 2
        db.upsert_schedule_games([
            {"season": 2026, "week": 2, "team": "MIA", "opponent": "NE", "is_home": 1,
             "kickoff": "2099-01-01T00:00Z"},
        ])
        assert (await wc.last_completed_week(db))["week"] == 1

    @pytest.mark.asyncio
    async def test_briefing_survives_an_nfl_state_outage(self, monkeypatch, db):
        from nfl_mcp import briefing_tools
        monkeypatch.setattr(briefing_tools, "NFLDatabase", lambda *a, **k: db)
        monkeypatch.setattr(sleeper_tools, "get_nfl_state", AsyncMock(side_effect=OSError("down")))
        monkeypatch.setattr(sleeper_tools, "get_league", AsyncMock(return_value={"league": {}}))
        monkeypatch.setattr(sleeper_tools, "get_rosters", AsyncMock(return_value={"rosters": []}))
        out = await briefing_tools.get_weekly_briefing("L", roster_id=1)
        # It stops at the missing roster, but only after resolving the week.
        assert out["success"] is False
        got = await wc.current_season_week(db)
        assert got["season"] == 2026 and got["source"] == "schedule"


class TestMigrationV15:
    def test_a_database_at_v14_gains_the_v15_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.db")
            db = NFLDatabase(path)
            with db._pool.get_connection() as conn:
                conn.execute("DROP TABLE projection_log")
                conn.execute("DROP TABLE league_checks")
                conn.execute("DELETE FROM schema_version WHERE version = 15")
                conn.commit()
            db.close()
            again = NFLDatabase(path)
            with again._pool.get_connection() as conn:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                versions = [r[0] for r in conn.execute(
                    "SELECT version FROM schema_version WHERE version >= 14 ORDER BY version")]
            assert {"projection_log", "league_checks"} <= names
            assert versions == [14, 15]


