"""Tests for strength-of-schedule tools (nfl_mcp.sos_tools)."""
from unittest.mock import patch

import pytest

from nfl_mcp import sos_tools
from nfl_mcp.sos_tools import (
    _ease_score,
    _resolve_rankings,
    compute_team_sos,
    get_strength_of_schedule,
)


def _rankings(entries):
    """Build a rankings dict: {position: [{team, rank, matchup_tier, is_fallback}]}."""
    out = {}
    for pos, teams in entries.items():
        out[pos] = [
            {"team": t, "rank": rank, "matchup_tier": "x", "is_fallback": fb}
            for (t, rank, fb) in teams
        ]
    return out


class _StubDB:
    def __init__(self, schedule):
        # schedule: {(season, week, team): opponent}
        self._schedule = schedule
        self.upserts = []

    def get_opponent(self, season, week, team):
        return self._schedule.get((season, week, team))

    def upsert_schedule_games(self, games):
        self.upserts.append(games)
        return len(games)


class _StubAnalyzer:
    def __init__(self, rankings_by_season, db=None):
        self._rankings_by_season = rankings_by_season
        self.db = db
        self.fetch_calls = []

    async def fetch_defense_rankings(self, season=None):
        self.fetch_calls.append(season)
        return self._rankings_by_season.get(season, {})

    def get_matchup_difficulty(self, position, opponent, rankings=None):
        for t in (rankings or {}).get(position.upper(), []):
            if t["team"] == opponent.upper():
                return {
                    "rank": t["rank"],
                    "matchup_tier": t.get("matchup_tier"),
                    "is_fallback": t.get("is_fallback", False),
                }
        return {"rank": 16, "matchup_tier": "neutral", "is_fallback": True}


class TestEaseScore:
    def test_endpoints_and_midpoint(self):
        assert _ease_score(1) == 0.0        # toughest schedule
        assert _ease_score(32) == 100.0     # easiest schedule
        assert _ease_score(16.5) == 50.0    # average
        # clamps out-of-range input
        assert _ease_score(0) == 0.0
        assert _ease_score(99) == 100.0


class TestComputeTeamSos:
    def test_avg_rank_ease_and_bye(self):
        rankings = _rankings({"RB": [("KC", 32, False), ("SF", 1, False)]})
        analyzer = _StubAnalyzer({})
        opponents = {"BUF": {15: "KC", 16: "SF", 17: None}}  # week 17 = bye/missing

        sos = compute_team_sos(opponents, rankings, ["RB"], analyzer)

        buf = sos["BUF"]
        assert buf["games"] == 2
        assert buf["bye_or_missing_weeks"] == [17]
        rb = buf["positions"]["RB"]
        assert rb["avg_opponent_defense_rank"] == 16.5  # (32 + 1) / 2
        assert rb["ease_score"] == 50.0
        assert rb["is_fallback"] is False
        assert [w["week"] for w in rb["weeks"]] == [15, 16]
        assert buf["overall_ease_score"] == 50.0

    def test_fallback_propagates_when_opponent_unknown(self):
        # Opponent "XXX" is not in rankings -> stub returns rank 16, is_fallback True.
        rankings = _rankings({"RB": [("KC", 32, False)]})
        analyzer = _StubAnalyzer({})
        sos = compute_team_sos({"BUF": {15: "XXX"}}, rankings, ["RB"], analyzer)
        assert sos["BUF"]["positions"]["RB"]["is_fallback"] is True


class TestResolveRankings:
    @pytest.mark.asyncio
    async def test_uses_target_season_when_live(self):
        analyzer = _StubAnalyzer({2026: _rankings({"RB": [("KC", 32, False)]})})
        rankings, used, fell_back = await _resolve_rankings(analyzer, 2026, None)
        assert used == 2026
        assert fell_back is False
        assert analyzer.fetch_calls == [2026]

    @pytest.mark.asyncio
    async def test_falls_back_to_prior_season_in_preseason(self):
        analyzer = _StubAnalyzer({
            2026: _rankings({"RB": [("KC", 32, True)]}),   # all placeholder
            2025: _rankings({"RB": [("KC", 30, False)]}),  # real prior-season data
        })
        rankings, used, fell_back = await _resolve_rankings(analyzer, 2026, None)
        assert used == 2025
        assert fell_back is False
        assert analyzer.fetch_calls == [2026, 2025]

    @pytest.mark.asyncio
    async def test_explicit_strength_season_is_respected(self):
        analyzer = _StubAnalyzer({2024: _rankings({"RB": [("KC", 32, False)]})})
        _, used, _ = await _resolve_rankings(analyzer, 2026, 2024)
        assert used == 2024
        assert analyzer.fetch_calls == [2024]  # no fallback probe


class TestGetStrengthOfSchedule:
    def _analyzer_with_schedule(self):
        rankings = _rankings({
            "RB": [("KC", 32, False), ("SF", 1, False)],
            "WR": [("KC", 30, False), ("SF", 3, False)],
        })
        schedule = {
            (2026, 15, "BUF"): "KC", (2026, 16, "BUF"): "KC", (2026, 17, "BUF"): "KC",
            (2026, 15, "MIA"): "SF", (2026, 16, "MIA"): "SF", (2026, 17, "MIA"): "SF",
        }
        db = _StubDB(schedule)
        return _StubAnalyzer({2026: rankings}, db=db)

    @pytest.mark.asyncio
    async def test_ranks_teams_easiest_first(self):
        analyzer = self._analyzer_with_schedule()
        with patch("nfl_mcp.matchup_tools.get_defense_analyzer", return_value=analyzer):
            result = await get_strength_of_schedule(
                season=2026, start_week=15, end_week=17, positions=["RB", "WR"]
            )

        assert result["success"] is True
        assert result["strength_source_season"] == 2026
        assert result["strength_is_fallback"] is False
        assert result["weeks"] == [15, 16, 17]

        # BUF faces KC (weak D) every week -> easiest; MIA faces SF (tough) -> hardest.
        rb = result["by_position"]["RB"]
        assert rb[0]["team"] == "BUF" and rb[0]["sos_rank"] == 1
        assert rb[-1]["team"] == "MIA"
        assert rb[0]["ease_score"] > rb[-1]["ease_score"]

        overall = result["overall"]
        assert overall[0]["team"] == "BUF" and overall[0]["sos_rank"] == 1

    @pytest.mark.asyncio
    async def test_playoff_wrapper_uses_weeks_15_17(self):
        analyzer = self._analyzer_with_schedule()
        with patch("nfl_mcp.matchup_tools.get_defense_analyzer", return_value=analyzer):
            result = await sos_tools.get_playoff_sos(season=2026, positions=["RB"])
        assert result["success"] is True
        assert result["weeks"] == [15, 16, 17]

    @pytest.mark.asyncio
    async def test_no_network_when_schedule_cached(self):
        analyzer = self._analyzer_with_schedule()
        with patch("nfl_mcp.matchup_tools.get_defense_analyzer", return_value=analyzer), \
                patch("nfl_mcp.sleeper_tools._fetch_week_schedule") as mock_fetch:
            await get_strength_of_schedule(season=2026, start_week=15, end_week=17)
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_week_range_rejected(self):
        analyzer = self._analyzer_with_schedule()
        with patch("nfl_mcp.matchup_tools.get_defense_analyzer", return_value=analyzer):
            bad_high = await get_strength_of_schedule(season=2026, start_week=1, end_week=25)
            reversed_range = await get_strength_of_schedule(season=2026, start_week=17, end_week=15)
        assert bad_high["success"] is False
        assert "between 1 and 18" in bad_high["error"]
        assert reversed_range["success"] is False
        assert "start_week must be <= end_week" in reversed_range["error"]
