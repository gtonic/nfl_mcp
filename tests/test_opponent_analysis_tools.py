"""Tests for opponent_analysis_tools module."""
from unittest.mock import patch

import pytest

from nfl_mcp.opponent_analysis_tools import OpponentAnalyzer, analyze_opponent


@pytest.fixture(autouse=True)
def _offline_week(monkeypatch):
    """analyze_opponent reads the current week and its matchup; keep both offline."""
    from nfl_mcp import opponent_analysis_tools as oat

    async def _now(db=None):
        return {"season": 2026, "week": 3}

    async def _no_matchups(league_id, week):
        return {"success": True, "matchups": []}

    monkeypatch.setattr(oat, "_season_week", _now)
    monkeypatch.setattr(oat, "get_matchups", _no_matchups)


class TestOpponentAnalyzer:
    """Test OpponentAnalyzer class."""

    @pytest.fixture
    def analyzer(self):
        return OpponentAnalyzer()

    def test_position_strength_empty_roster(self, analyzer):
        """Test position assessment with empty roster."""
        result = analyzer._assess_position_strength([], "RB")

        assert result["strength_score"] == 0
        assert result["depth_count"] == 0
        assert result["weakness_level"] == "critical"
        assert "No players at position" in result["concerns"][0]

    def test_position_strength_strong_roster(self, analyzer):
        """Test position assessment with strong roster."""
        players = [
            {"snap_pct": 85, "practice_status": "E", "usage_trend_overall": "stable"},
            {"snap_pct": 75, "practice_status": "E", "usage_trend_overall": "stable"},
            {"snap_pct": 60, "practice_status": "Q", "usage_trend_overall": "up"},
            {"snap_pct": 40, "practice_status": "E", "usage_trend_overall": "stable"},
        ]

        result = analyzer._assess_position_strength(players, "RB")

        assert result["strength_score"] >= 50
        assert result["depth_count"] == 4
        assert result["weakness_level"] == "strong"

    def test_position_strength_weak_roster(self, analyzer):
        """Test position assessment with weak roster."""
        players = [
            {"snap_pct": 30, "practice_status": "DNP", "usage_trend_overall": "down"}
        ]

        result = analyzer._assess_position_strength(players, "RB")

        assert result["weakness_level"] in ["weak", "critical"]
        assert result["injury_concerns"] == 1

    def test_position_strength_injury_concerns(self, analyzer):
        """Test injury concern detection."""
        players = [
            {"snap_pct": 70, "practice_status": "DNP", "usage_trend_overall": "stable"},
            {"snap_pct": 60, "practice_status": "LP", "usage_trend_overall": "stable"},
        ]

        result = analyzer._assess_position_strength(players, "WR")

        assert result["injury_concerns"] == 2
        assert any("injury" in c.lower() for c in result["concerns"])

    def test_identify_starter_weaknesses(self, analyzer):
        """Test starter weakness identification."""
        starters = [
            {
                "player_id": "1",
                "full_name": "John Smith",
                "position": "RB",
                "practice_status": "DNP",
                "usage_trend_overall": "down",
                "snap_pct": 45.0
            }
        ]

        weaknesses = analyzer._identify_starter_weaknesses(starters)

        assert len(weaknesses) == 1
        assert weaknesses[0]["player_name"] == "John Smith"
        assert any("DNP" in w for w in weaknesses[0]["weaknesses"])
        assert weaknesses[0]["severity"] == "high"

    def test_identify_starter_weaknesses_clean(self, analyzer):
        """Test starter weakness identification with no issues."""
        starters = [
            {
                "player_id": "1",
                "full_name": "Healthy Player",
                "position": "RB",
                "practice_status": "E",
                "usage_trend_overall": "stable",
                "snap_pct": 90.0
            }
        ]

        weaknesses = analyzer._identify_starter_weaknesses(starters)

        assert len(weaknesses) == 0

    def test_generate_exploitation_strategies(self, analyzer):
        """Test strategy generation."""
        position_assessments = {
            "RB": {
                "strength_score": 20,
                "weakness_level": "weak",
                "concerns": ["Shallow depth"]
            }
        }
        starter_weaknesses = []

        strategies = analyzer._generate_exploitation_strategies(position_assessments, starter_weaknesses)

        assert len(strategies) > 0
        assert strategies[0]["category"] == "position_weakness"
        assert strategies[0]["priority"] == "critical"

    def test_analyze_opponent_roster(self, analyzer):
        """Test comprehensive roster analysis."""
        roster = {
            "roster_id": "123",
            "owner_id": "owner1",
            "players_enriched": [
                {"player_id": "1", "full_name": "P1", "position": "RB", "snap_pct": 80, "practice_status": "E", "usage_trend_overall": "stable"},
                {"player_id": "2", "full_name": "P2", "position": "QB", "snap_pct": 30, "practice_status": "DNP", "usage_trend_overall": "down"},
            ],
            "starters_enriched": [
                {"player_id": "1", "full_name": "P1", "position": "RB"},
                {"player_id": "2", "full_name": "P2", "position": "QB"},
            ]
        }

        result = analyzer.analyze_opponent_roster(roster)

        assert "vulnerability_score" in result
        assert "vulnerability_level" in result
        assert "position_assessments" in result
        assert "exploitation_strategies" in result
        assert result["roster_id"] == "123"


class TestAnalyzeOpponent:
    """Test analyze_opponent async function."""

    @pytest.mark.asyncio
    async def test_analyze_opponent_missing_league_id(self):
        """Test with missing league_id."""
        result = await analyze_opponent("", 1)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_opponent_missing_roster_id(self):
        """Test with missing opponent_roster_id."""
        result = await analyze_opponent("league1", None)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_opponent_roster_not_found(self):
        """Test with non-existent roster."""
        mock_result = {"success": True, "rosters": [{"roster_id": "2"}]}

        async def mock_get_rosets(league_id):
            return mock_result

        with patch('nfl_mcp.opponent_analysis_tools.get_rosters', side_effect=mock_get_rosets):
            result = await analyze_opponent("league1", 999)
            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_opponent_success(self):
        """Test successful analysis."""
        mock_roster = {
            "roster_id": 1,
            "owner_id": "owner1",
            "players_enriched": [],
            "starters_enriched": []
        }
        mock_users = {"success": True, "users": [{"user_id": "owner1", "display_name": "Test User"}]}

        async def mock_get_rosters(league_id):
            return {"success": True, "rosters": [mock_roster]}

        async def mock_get_league_users(league_id):
            return mock_users

        with patch('nfl_mcp.opponent_analysis_tools.get_rosters', side_effect=mock_get_rosters):
            with patch('nfl_mcp.opponent_analysis_tools.get_league_users', side_effect=mock_get_league_users):
                result = await analyze_opponent("league1", 1)

                assert result["success"] is True
                assert result["opponent_name"] == "Test User"
                assert "vulnerability_score" in result


class TestKickerAndDefense:
    """One K and one DEF is the whole position, not a critical weakness."""

    def test_single_kicker_is_not_critical(self):
        analyzer = OpponentAnalyzer()
        for pos in ("K", "DEF"):
            res = analyzer._assess_position_strength(
                [{"full_name": "Unit", "position": pos}], pos)
            assert res["weakness_level"] == "moderate", pos
            assert not any("depth" in c.lower() or "snap" in c.lower() for c in res["concerns"])

    def test_missing_kicker_is_still_critical(self):
        res = OpponentAnalyzer()._assess_position_strength([], "K")
        assert res["weakness_level"] == "critical"

    def test_a_lone_rb_is_still_penalised(self):
        res = OpponentAnalyzer()._assess_position_strength(
            [{"full_name": "RB", "position": "RB", "snap_pct": 30}], "RB")
        assert res["weakness_level"] == "critical"


class _AthleteDB:
    def get_athletes_by_ids(self, ids):
        rows = {
            "qb1": {"full_name": "This Week QB", "position": "QB", "team_id": "BUF"},
            "rb1": {"full_name": "Old Starter", "position": "RB", "team_id": "SF"},
            "SF": {"full_name": "San Francisco", "position": "DEF", "team_id": "SF"},
        }
        return {i: rows[i] for i in ids if i in rows}


class TestThisWeeksMatchup:
    ROSTER = {
        "roster_id": 2, "owner_id": "o2",
        "players": ["qb1", "rb1", "SF"],
        "players_enriched": [
            {"player_id": "qb1", "full_name": "This Week QB", "position": "QB",
             "practice_status": "DNP"},
            {"player_id": "rb1", "full_name": "Old Starter", "position": "RB"},
            {"player_id": "SF", "full_name": "San Francisco", "position": "DEF"},
        ],
        # Last saved lineup: stale.
        "starters_enriched": [{"player_id": "rb1", "full_name": "Old Starter",
                               "position": "RB", "practice_status": "DNP"}],
    }

    async def _run(self, monkeypatch, matchup):
        from nfl_mcp import opponent_analysis_tools as oat
        from nfl_mcp import projections

        seen = {}

        async def _rosters(_):
            return {"success": True, "rosters": [self.ROSTER]}

        async def _users(_):
            return {"success": True, "users": []}

        async def _matchups(_, week):
            seen["week"] = week
            return {"success": True, "matchups": [matchup]}

        async def _now(db=None):
            return {"season": 2026, "week": 3}

        async def _project(players, **kw):
            seen["projected"] = [p["name"] for p in players]
            seen["league_id"] = kw.get("league_id")
            return {"success": True, "projections": [
                {"player": p["name"], "position": p["position"],
                 "projected_points": {"QB": 20.0, "DEF": 7.5}.get(p["position"], 10.0)}
                for p in players]}

        monkeypatch.setattr(oat, "get_rosters", _rosters)
        monkeypatch.setattr(oat, "get_league_users", _users)
        monkeypatch.setattr(oat, "get_matchups", _matchups)
        monkeypatch.setattr(oat, "_season_week", _now)
        monkeypatch.setattr(projections, "project_players", _project)
        res = await oat.analyze_opponent("L1", 2, db=_AthleteDB())
        return res, seen

    @pytest.mark.asyncio
    async def test_uses_matchup_starters_and_projects_them(self, monkeypatch):
        matchup = {"roster_id": 2, "matchup_id": 4, "points": 0.0, "custom_points": None,
                   "starters": ["qb1", "SF", "0"]}
        res, seen = await self._run(monkeypatch, matchup)
        assert res["success"] is True
        assert seen["week"] == 3                      # defaulted to the current week
        assert res["starters_source"] == "matchup"
        flagged = {w["player_name"] for w in res["starter_weaknesses"]}
        assert flagged == {"This Week QB"}            # not the stale roster starter
        ctx = res["matchup_context"]
        assert ctx["projected_points"] == 27.5        # ours, not custom_points
        assert seen["projected"] == ["This Week QB", "SF"]
        assert seen["league_id"] == "L1"              # league scoring

    @pytest.mark.asyncio
    async def test_falls_back_to_roster_starters_without_a_lineup(self, monkeypatch):
        res, _ = await self._run(monkeypatch, {"roster_id": 2, "matchup_id": 4})
        assert res["starters_source"] == "roster"
        assert res["matchup_context"]["projected_points"] is None
