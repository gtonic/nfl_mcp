"""Projections must be in the league's scoring, not always full PPR.

`scoring` used to reach only the FantasyCalc *value* lookup while the points
scale stayed hard-wired to full PPR, so a half-PPR league was quoted full-PPR
numbers — and, worse, the receiver-over-runner ordering that full PPR implies.
"""
import types

import pytest

from nfl_mcp import opportunity_tools
from nfl_mcp.briefing_tools import _scoring_label, _scoring_ppr
from nfl_mcp.opportunity import project_opportunity
from nfl_mcp.projections import ProjectionEngine, base_ppg


def _engine():
    eng = ProjectionEngine.__new__(ProjectionEngine)
    eng.values = types.SimpleNamespace(lookup=lambda *a, **k: {"position_rank": 8})
    eng.defense = types.SimpleNamespace(
        get_matchup_difficulty=lambda *a, **k: {"matchup_tier": "neutral"}
    )
    eng.vegas = types.SimpleNamespace(get_game_lines=lambda *a, **k: {"is_fallback": True})
    return eng


def _games(position, weeks=4, **stats):
    base = {"targets": 0.0, "receptions": 0.0, "receiving_yards": 0.0,
            "receiving_tds": 0.0, "carries": 0.0, "rushing_yards": 0.0,
            "rushing_tds": 0.0, "attempts": 0.0, "passing_yards": 0.0,
            "passing_tds": 0.0, "interceptions": 0.0}
    return [{**base, **stats, "week": w} for w in range(1, weeks + 1)]


def _index(name, position, games):
    return opportunity_tools.build_name_index(
        {"x1": {"player_id": "x1", "name": name, "position": position,
                "team": "BUF", "games": games}}
    )


class TestOpportunityScoring:
    def test_receptions_are_worth_the_league_value(self):
        games = _games("WR", targets=9, receptions=6, receiving_yards=70)
        full = project_opportunity(games, "WR", ppr=1.0)
        half = project_opportunity(games, "WR", ppr=0.5)
        standard = project_opportunity(games, "WR", ppr=0.0)

        assert full > half > standard
        # Six catches a game: the gap between formats is the reception value.
        assert full - half == pytest.approx(3.0, abs=0.35)
        assert half - standard == pytest.approx(3.0, abs=0.35)

    def test_ordering_flips_between_formats(self):
        """The point of half PPR: a runner overtakes a possession receiver."""
        receiver = _games("WR", targets=9, receptions=7, receiving_yards=60)
        runner = _games("RB", carries=16, rushing_yards=75, rushing_tds=0.4)

        full_rec = project_opportunity(receiver, "WR", ppr=1.0)
        full_run = project_opportunity(runner, "RB", ppr=1.0)
        std_rec = project_opportunity(receiver, "WR", ppr=0.0)
        std_run = project_opportunity(runner, "RB", ppr=0.0)

        assert full_rec > full_run
        assert std_run > std_rec

    def test_qb_is_unaffected_by_reception_value(self):
        games = _games("QB", attempts=34, passing_yards=250, passing_tds=1.6,
                       interceptions=0.7, carries=4, rushing_yards=18)
        assert (project_opportunity(games, "QB", ppr=1.0)
                == project_opportunity(games, "QB", ppr=0.0))


class TestRankBucketScoring:
    def test_buckets_are_rebased(self):
        assert base_ppg("WR", 8, 1.0) > base_ppg("WR", 8, 0.5) > base_ppg("WR", 8, 0.0)
        # Defenses and kickers do not catch passes.
        assert base_ppg("K", None, 0.0) == base_ppg("K", None, 1.0)
        assert base_ppg("DST", None, 0.0) == base_ppg("DST", None, 1.0)

    def test_full_ppr_is_the_default_and_unchanged(self):
        assert base_ppg("WR", 8) == 17.0
        assert base_ppg("RB", 3) == 19.0


class TestEngineThreadsScoring:
    def test_projection_differs_by_scoring_on_the_opportunity_path(self):
        eng = _engine()
        games = _games("WR", targets=9, receptions=6, receiving_yards=70)
        idx = _index("Test Receiver", "WR", games)
        player = {"name": "Test Receiver", "position": "WR", "team": "BUF",
                  "opponent": "MIA"}

        full = eng._project_one(player, {}, {}, {}, idx, 6, 1.0)
        half = eng._project_one(player, {}, {}, {}, idx, 6, 0.5)

        assert full["breakdown"]["base_source"] == "opportunity"
        assert half["breakdown"]["base_source"] == "opportunity"
        assert full["projected_points"] > half["projected_points"]

    def test_projection_differs_by_scoring_on_the_fallback_path(self):
        eng = _engine()
        player = {"name": "Unknown Guy", "position": "WR", "team": "BUF",
                  "opponent": "MIA"}
        full = eng._project_one(player, {}, {}, {}, {}, 6, 1.0)
        half = eng._project_one(player, {}, {}, {}, {}, 6, 0.5)

        assert full["breakdown"]["base_source"] == "rank_bucket"
        assert full["projected_points"] > half["projected_points"]

    @pytest.mark.asyncio
    async def test_project_many_reports_the_scoring_it_used(self):
        eng = _engine()
        eng.values = types.SimpleNamespace(
            lookup=lambda *a, **k: None,
            get_values=_async({"source": "test"}),
        )
        eng.defense = types.SimpleNamespace(fetch_defense_rankings=_async({}),
                                            get_matchup_difficulty=lambda *a, **k: {})
        eng.vegas = types.SimpleNamespace(fetch_current_lines=_async({}),
                                          get_game_lines=lambda *a, **k: {"is_fallback": True})

        out = await eng.project_many(
            [{"name": "A", "position": "WR", "team": "BUF", "opponent": "MIA"}],
            scoring="half_ppr",
        )
        assert out["scoring"] == "half_ppr"
        assert out["ppr"] == 0.5


class TestBriefingReadsTheLeague:
    def test_exact_reception_value_is_preserved(self):
        league = {"scoring_settings": {"rec": 0.5}}
        assert _scoring_ppr(league) == 0.5
        assert _scoring_label(league) == "half_ppr"

    def test_unreadable_settings_fall_back_to_full_ppr(self):
        assert _scoring_ppr({}) == 1.0
        assert _scoring_ppr({"scoring_settings": {"rec": "nonsense"}}) == 1.0

    def test_an_unusual_value_is_not_rounded_to_the_label(self):
        # 0.6 PPR is a real Sleeper setting; the label bucket is lossy, the
        # value passed to the projection must not be.
        league = {"scoring_settings": {"rec": 0.6}}
        assert _scoring_ppr(league) == 0.6
        assert _scoring_label(league) == "half_ppr"


def _async(value):
    async def _call(*a, **k):
        return value
    return _call
