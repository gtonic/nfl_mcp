"""Tests for the streaming planner (nfl_mcp.streaming_tools) and offense rankings."""
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nfl_mcp import matchup_tools
from nfl_mcp.streaming_tools import (
    _resolve_offense,
    _strength_score,
    compute_streaming_scores,
    get_streaming_options,
)


def _def_rankings(entries):
    return {
        pos: [
            {"team": t, "rank": rank, "matchup_tier": "x", "is_fallback": fb}
            for (t, rank, fb) in teams
        ]
        for pos, teams in entries.items()
    }


def _offense(entries):
    # entries: {team: rank}; points_scored_avg is cosmetic here.
    return {t: {"rank": r, "points_scored_avg": 30.0 - r} for t, r in entries.items()}


class _StubDB:
    def __init__(self, schedule):
        self._schedule = schedule

    def get_opponent(self, season, week, team):
        return self._schedule.get((season, week, team))

    def upsert_schedule_games(self, games):
        return len(games)


class _StubAnalyzer:
    def __init__(self, def_by_season, db=None):
        self._def = def_by_season
        self.db = db

    async def fetch_defense_rankings(self, season=None):
        return self._def.get(season, {})

    def get_matchup_difficulty(self, position, opponent, rankings=None):
        for t in (rankings or {}).get(position.upper(), []):
            if t["team"] == opponent.upper():
                return {"rank": t["rank"], "matchup_tier": t.get("matchup_tier"),
                        "is_fallback": t.get("is_fallback", False)}
        return {"rank": 16, "matchup_tier": "neutral", "is_fallback": True}


class TestStrengthScore:
    def test_endpoints(self):
        assert _strength_score(1) == 100.0   # strongest offense
        assert _strength_score(32) == 0.0    # weakest offense
        assert _strength_score(16.5) == 50.0


class TestComputeStreamingScores:
    def test_defense_offense_signals(self):
        analyzer = _StubAnalyzer({})
        def_rankings = _def_rankings({"QB": [("KC", 32, False), ("SF", 1, False)]})
        # KC strong offense (rank 1), SF weak offense (rank 32)
        offense = _offense({"KC": 1, "SF": 32, "BUF": 5, "MIA": 20})
        opponents = {
            "BUF": {10: "KC"},   # faces KC
            "MIA": {10: "SF"},   # faces SF
        }
        scores = compute_streaming_scores(
            opponents, ["QB", "DST", "K"], def_rankings, offense, analyzer
        )

        # QB: BUF faces KC (rank 32 = soft D) -> ease 100; MIA faces SF (rank 1) -> 0.
        assert scores["QB"]["BUF"]["stream_score"] == 100.0
        assert scores["QB"]["MIA"]["stream_score"] == 0.0
        # DST: MIA's opponent SF has a weak offense (rank 32) -> great DST stream.
        assert scores["DST"]["MIA"]["stream_score"] == 100.0
        assert scores["DST"]["BUF"]["stream_score"] == 0.0   # opp KC strong offense
        # K: own-offense strength. BUF rank 5 -> high; MIA rank 20 -> lower.
        assert scores["K"]["BUF"]["stream_score"] > scores["K"]["MIA"]["stream_score"]

    def test_missing_offense_data_omits_dst(self):
        analyzer = _StubAnalyzer({})
        scores = compute_streaming_scores(
            {"BUF": {10: "KC"}}, ["DST"], {}, {}, analyzer  # empty offense rankings
        )
        assert scores["DST"] == {}   # nothing scorable


class TestResolveOffense:
    @pytest.mark.asyncio
    async def test_prior_season_fallback(self):
        async def fake_fetch(season):
            return _offense({"KC": 1}) if season == 2025 else {}
        with patch("nfl_mcp.matchup_tools.fetch_offense_rankings", side_effect=fake_fetch):
            rankings, used, fell_back = await _resolve_offense(2026, None)
        assert used == 2025
        assert fell_back is False
        assert rankings


class TestFetchOffenseRankings:
    @pytest.mark.asyncio
    async def test_ranks_by_points_and_maps_team_abbrevs(self):
        csv_text = (
            "season_type,position,team,week,fantasy_points_ppr\n"
            "REG,QB,KC,1,25.0\n"
            "REG,RB,KC,1,15.0\n"
            "REG,QB,KC,2,20.0\n"
            "REG,QB,BUF,1,10.0\n"
            "REG,QB,BUF,2,8.0\n"
            "REG,WR,LA,1,12.0\n"      # LA -> LAR via team fix
            "POST,QB,KC,3,99.0\n"     # ignored: not REG
        )
        resp = Mock()
        resp.status_code = 200
        resp.text = csv_text
        resp.raise_for_status = Mock()
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        matchup_tools._offense_rankings_cache.pop(2099, None)
        with patch("nfl_mcp.matchup_tools.create_http_client", return_value=client):
            rankings = await matchup_tools.fetch_offense_rankings(2099)

        # KC: (25+15)+20 over 2 weeks = 30.0/gm; LAR: 12.0; BUF: 18/2 = 9.0
        assert rankings["KC"]["rank"] == 1
        assert rankings["KC"]["points_scored_avg"] == 30.0
        assert rankings["LAR"]["rank"] == 2       # team-fix applied
        assert rankings["BUF"]["rank"] == 3
        matchup_tools._offense_rankings_cache.pop(2099, None)

    @pytest.mark.asyncio
    async def test_returns_empty_on_404(self):
        resp = Mock()
        resp.status_code = 404
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        matchup_tools._offense_rankings_cache.pop(1999, None)
        with patch("nfl_mcp.matchup_tools.create_http_client", return_value=client):
            rankings = await matchup_tools.fetch_offense_rankings(1999)
        assert rankings == {}


class TestGetStreamingOptions:
    def _setup(self):
        def_by_season = {2026: _def_rankings({
            "QB": [("KC", 32, False), ("SF", 1, False)],
            "TE": [("KC", 30, False), ("SF", 3, False)],
        })}
        schedule = {
            (2026, 10, "BUF"): "KC", (2026, 11, "BUF"): "KC", (2026, 12, "BUF"): "KC",
            (2026, 10, "MIA"): "SF", (2026, 11, "MIA"): "SF", (2026, 12, "MIA"): "SF",
        }
        analyzer = _StubAnalyzer(def_by_season, db=_StubDB(schedule))
        offense = _offense({"KC": 1, "SF": 30, "BUF": 5, "MIA": 20})
        return analyzer, offense

    @pytest.mark.asyncio
    async def test_ranks_streams_and_reports_sources(self):
        analyzer, offense = self._setup()
        async def fake_offense(season):
            return offense if season == 2026 else {}
        with patch("nfl_mcp.matchup_tools.get_defense_analyzer", return_value=analyzer), \
                patch("nfl_mcp.matchup_tools.fetch_offense_rankings", side_effect=fake_offense):
            result = await get_streaming_options(
                season=2026, start_week=10, weeks_ahead=3, positions=["QB", "DST", "K"]
            )

        assert result["success"] is True
        assert result["weeks"] == [10, 11, 12]
        assert result["defense_source_season"] == 2026
        assert result["offense_source_season"] == 2026
        assert result["defense_is_fallback"] is False
        assert result["offense_is_fallback"] is False

        opts = result["streaming_options"]
        assert opts["QB"][0]["team"] == "BUF" and opts["QB"][0]["stream_rank"] == 1
        assert opts["DST"][0]["team"] == "MIA"     # faces SF's weak offense
        assert opts["K"][0]["team"] == "BUF"       # stronger own offense

    @pytest.mark.asyncio
    async def test_top_n_limits_results(self):
        analyzer, offense = self._setup()
        async def fake_offense(season):
            return offense if season == 2026 else {}
        with patch("nfl_mcp.matchup_tools.get_defense_analyzer", return_value=analyzer), \
                patch("nfl_mcp.matchup_tools.fetch_offense_rankings", side_effect=fake_offense):
            result = await get_streaming_options(
                season=2026, start_week=10, weeks_ahead=1, positions=["QB"], top_n=1
            )
        assert len(result["streaming_options"]["QB"]) == 1

    @pytest.mark.asyncio
    async def test_invalid_params_rejected(self):
        analyzer, offense = self._setup()
        with patch("nfl_mcp.matchup_tools.get_defense_analyzer", return_value=analyzer):
            bad_week = await get_streaming_options(season=2026, start_week=25, weeks_ahead=3)
            bad_ahead = await get_streaming_options(season=2026, start_week=10, weeks_ahead=9)
        assert bad_week["success"] is False and "start_week" in bad_week["error"]
        assert bad_ahead["success"] is False and "weeks_ahead" in bad_ahead["error"]
