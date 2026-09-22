"""Start/sit must agree with the briefing about scoring and health.

Four start/sit tools defaulted to full PPR and 12 teams and knew an injury only
if the caller typed it in. The briefing reads both from the league and the
injury tables, so the same player in the same week came back as a starter from
one tool and 0 points from the other (Brock Bowers, Out, 2026 week 3).
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp.database import NFLDatabase


def _defense():
    from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
    analyzer = DefenseRankingsAnalyzer(db=None)
    analyzer.fetch_defense_rankings = AsyncMock(return_value={})
    return analyzer


class _Engine:
    """Records what the projection engine was asked, players included."""

    def __init__(self):
        self.calls = []

    async def project_many(self, players, **kwargs):
        self.calls.append({"players": players, **kwargs})
        return {"projections": [
            {"projected_points": 10.0, "floor": 4.0, "ceiling": 16.0} for _ in players
        ]}


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        database.upsert_injuries([{
            "player_id": "4432665", "player_name": "Brock Bowers", "team_id": "LV",
            "position": "TE", "injury_status": "Out", "sources": ["ESPN"],
        }])
        database.upsert_athletes({"11566": {
            "full_name": "Jayden Daniels", "position": "QB", "team": "WAS",
            "injury_status": "Out",
        }})
        yield database


def _league(rec=0.5, teams=10):
    return {"league": {"scoring_settings": {"rec": rec}, "total_rosters": teams}}


class TestLeagueScoring:
    @pytest.mark.asyncio
    async def test_league_supplies_scoring_and_size(self):
        with patch("nfl_mcp.sleeper_tools.get_league", AsyncMock(return_value=_league())):
            assert await lo._league_scoring("123", None) == ("0.5", 10, "league")

    @pytest.mark.asyncio
    async def test_explicit_scoring_wins_but_keeps_the_league_size(self):
        with patch("nfl_mcp.sleeper_tools.get_league", AsyncMock(return_value=_league())):
            assert await lo._league_scoring("123", "ppr") == ("ppr", 10, "caller")

    @pytest.mark.asyncio
    async def test_neither_is_labelled_as_an_assumption(self):
        assert await lo._league_scoring(None, None) == ("ppr", 12, "default")

    @pytest.mark.asyncio
    async def test_a_failed_league_lookup_falls_back_to_the_default(self):
        with patch("nfl_mcp.sleeper_tools.get_league", AsyncMock(side_effect=RuntimeError)):
            assert await lo._league_scoring("123", None) == ("ppr", 12, "default")

    @pytest.mark.asyncio
    async def test_start_sit_projects_in_the_league_scoring(self, db):
        engine = _Engine()
        optimizer = lo.LineupOptimizer(db=db, defense_analyzer=_defense())
        with patch("nfl_mcp.sleeper_tools.get_league", AsyncMock(return_value=_league())), \
             patch("nfl_mcp.projections.get_projection_engine", return_value=engine), \
             patch.object(lo, "get_lineup_optimizer", return_value=optimizer):
            out = await lo.get_start_sit_recommendation(
                player_name="Some WR", position="WR", team="MIA", opponent="NE",
                league_id="123", season=2026, week=3)
        assert engine.calls[0]["scoring"] == "0.5"
        assert engine.calls[0]["num_teams"] == 10
        assert out["scoring"] == "0.5"
        assert out["scoring_source"] == "league"


class TestInjuryLookup:
    async def _analyze(self, db, name, team, injury_data=None):
        engine = _Engine()
        optimizer = lo.LineupOptimizer(db=db, defense_analyzer=_defense())
        with patch("nfl_mcp.projections.get_projection_engine", return_value=engine):
            analysis = await optimizer.analyze_player(
                player_name=name, player_id="", position="TE", team=team,
                opponent="NO", injury_data=injury_data, season=2026, week=3)
        return analysis, engine

    @pytest.mark.asyncio
    async def test_report_status_is_used_when_the_caller_passes_none(self, db):
        analysis, engine = await self._analyze(db, "Brock Bowers", "LV")
        assert analysis.injury_status == "Out"
        assert analysis.injury_source == "report"
        assert analysis.decision == "must_sit"
        # The projection sees it too, not just the health score.
        assert engine.calls[0]["players"][0]["injury"] == {"status": "Out"}

    @pytest.mark.asyncio
    async def test_sleeper_status_is_used_without_a_report(self, db):
        analysis, _ = await self._analyze(db, "Jayden Daniels", "WAS")
        assert analysis.injury_status == "Out"
        assert analysis.injury_source == "sleeper"

    @pytest.mark.asyncio
    async def test_the_callers_status_is_not_overridden(self, db):
        analysis, _ = await self._analyze(
            db, "Brock Bowers", "LV", injury_data={"status": "Questionable"})
        assert analysis.injury_status == "Questionable"
        assert analysis.injury_source == "caller"

    @pytest.mark.asyncio
    async def test_a_null_status_does_not_crash(self, db):
        analysis, _ = await self._analyze(db, "Healthy Guy", "KC", injury_data={"status": None})
        assert analysis.injury_status == "healthy"

    @pytest.mark.asyncio
    async def test_unknown_player_stays_healthy(self, db):
        analysis, _ = await self._analyze(db, "Healthy Guy", "KC")
        assert analysis.injury_status == "healthy"
        assert analysis.injury_source is None


def test_enrichment_practice_codes_are_scored():
    assert lo.PRACTICE_STATUS_SCORES["fp"] == lo.PRACTICE_STATUS_SCORES["full"]
    assert lo.PRACTICE_STATUS_SCORES["lp"] == lo.PRACTICE_STATUS_SCORES["limited"]


def test_athlete_fixture_carries_the_sleeper_status(db):
    # Guards the fixture: upsert_athletes must keep the raw payload the
    # Sleeper-status lookup reads.
    row = db.search_athletes_by_name("Jayden Daniels")[0]
    assert json.loads(row["raw"])["injury_status"] == "Out"
