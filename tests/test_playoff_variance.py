"""Playoff odds must reflect each team's own scoring spread.

Every team used to share one hard-coded weekly sd of 25. That constant decides
how often the weaker team wins, so a boom/bust roster and a metronome at equal
points-per-game came out with identical odds — and nothing had ever checked the
25 against a real league.
"""
import math
import random

import pytest

from nfl_mcp import playoff_tools
from nfl_mcp.playoff_tools import (
    DEFAULT_SCORE_SD,
    _fetch_weekly_scores,
    _simulate,
    league_prior_sd,
    shrunk_sd,
)


class TestShrunkSd:
    def test_too_few_games_leaves_the_prior_alone(self):
        assert shrunk_sd([], 25.0) == 25.0
        assert shrunk_sd([100.0], 25.0) == 25.0

    def test_a_steady_team_pulls_below_the_prior(self):
        steady = [100.0, 101.0, 99.0, 100.5, 100.0, 99.5]
        assert shrunk_sd(steady, 25.0) < 25.0

    def test_a_volatile_team_pulls_above_the_prior(self):
        volatile = [60.0, 150.0, 70.0, 145.0, 65.0, 155.0]
        assert shrunk_sd(volatile, 25.0) > 25.0

    def test_shrinkage_keeps_a_short_sample_near_the_prior(self):
        """Two suspiciously identical weeks must not make a team a metronome."""
        two_games = [100.0, 100.0]
        many_games = [100.0] * 20

        assert shrunk_sd(two_games, 25.0) > shrunk_sd(many_games, 25.0)
        # Still a long way from claiming zero variance.
        assert shrunk_sd(two_games, 25.0) > 15.0

    def test_more_evidence_moves_further_from_the_prior(self):
        short = shrunk_sd([60.0, 140.0], 25.0)
        long = shrunk_sd([60.0, 140.0] * 10, 25.0)
        assert long > short


class TestLeaguePrior:
    def test_measures_week_to_week_volatility_not_league_inequality(self):
        """Two teams far apart in strength, each perfectly consistent."""
        scores = {1: [80.0, 80.0, 80.0, 80.0], 2: [140.0, 140.0, 140.0, 140.0]}
        assert league_prior_sd(scores) == pytest.approx(0.0, abs=1e-9)

    def test_falls_back_when_there_is_nothing_to_measure(self):
        assert league_prior_sd({}) == DEFAULT_SCORE_SD
        assert league_prior_sd({1: [100.0]}) == DEFAULT_SCORE_SD

    def test_two_games_are_not_under_counted(self):
        # Two weeks ±20 around each team's mean: the unbiased spread is
        # sqrt(2 × 400 / 1) = 28.3, not the population figure of 20.
        scores = {1: [80.0, 120.0], 2: [90.0, 130.0]}
        assert league_prior_sd(scores) == pytest.approx(math.sqrt(800.0), rel=1e-6)

    def test_picks_up_real_volatility(self):
        scores = {1: [60.0, 140.0, 60.0, 140.0], 2: [70.0, 130.0, 70.0, 130.0]}
        assert league_prior_sd(scores) > 25.0


class TestSimulateAcceptsPerTeamSd:
    def _teams(self):
        return [
            {"roster_id": 1, "wins": 0, "points": 0.0, "mean": 100.0},
            {"roster_id": 2, "wins": 0, "points": 0.0, "mean": 110.0},
        ]

    def test_variance_helps_the_underdog(self):
        """The whole point of a per-team sd: it changes who wins how often."""
        schedule = [(1, 2)] * 5

        steady = _simulate(self._teams(), schedule, 1, 4000,
                           {1: 5.0, 2: 5.0}, random.Random(7))
        swingy = _simulate(self._teams(), schedule, 1, 4000,
                           {1: 45.0, 2: 5.0}, random.Random(7))

        # Roster 1 is the weaker team; giving only it a wide spread must
        # improve its chances of sneaking the seed.
        assert swingy[1]["playoff_pct"] > steady[1]["playoff_pct"]

    def test_a_scalar_still_works(self):
        out = _simulate(self._teams(), [(1, 2)], 1, 500, 25.0, random.Random(1))
        assert set(out) == {1, 2}


class TestFetchWeeklyScores:
    @pytest.mark.asyncio
    async def test_collects_points_per_roster(self, monkeypatch):
        async def _matchups(league_id, week):
            return {"success": True, "matchups": [
                {"roster_id": 1, "points": 100.0 + week},
                {"roster_id": 2, "points": 90.0},
            ]}
        monkeypatch.setattr(playoff_tools, "get_matchups", _matchups)

        scores = await _fetch_weekly_scores("L", [1, 2, 3])
        assert scores[1] == [101.0, 102.0, 103.0]
        assert scores[2] == [90.0, 90.0, 90.0]

    @pytest.mark.asyncio
    async def test_an_unplayed_week_is_skipped_not_recorded_as_zero(self, monkeypatch):
        """Sleeper sends 0.0 for an unpublished week; that is not a real score."""
        async def _matchups(league_id, week):
            return {"success": True, "matchups": [
                {"roster_id": 1, "points": 0.0 if week > 1 else 110.0},
            ]}
        monkeypatch.setattr(playoff_tools, "get_matchups", _matchups)

        assert await _fetch_weekly_scores("L", [1, 2, 3]) == {1: [110.0]}

    @pytest.mark.asyncio
    async def test_survives_a_failing_or_malformed_week(self, monkeypatch):
        async def _matchups(league_id, week):
            if week == 2:
                raise RuntimeError("sleeper down")
            if week == 3:
                return {"success": False}
            return {"success": True, "matchups": [
                {"roster_id": 1, "points": 100.0},
                {"roster_id": 2, "points": None},
                {"roster_id": None, "points": 50.0},
            ]}
        monkeypatch.setattr(playoff_tools, "get_matchups", _matchups)

        assert await _fetch_weekly_scores("L", [1, 2, 3]) == {1: [100.0]}
