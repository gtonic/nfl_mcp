"""Offline guards for the uncertainty-calibration eval.

The eval itself needs real nflverse data and runs on a schedule; these keep its
machinery honest on every PR, without a network call.
"""

import os
import sys

import pytest

# The evals/ package lives at the repo root (not installed with nfl_mcp).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals.backtest import metrics as M
from evals.backtest.calibration import (
    actual_points,
    build_matchups,
    build_samples,
    coverage,
)


class TestBrier:
    def test_perfect_confident_prediction_scores_zero(self):
        assert M.brier([1.0, 0.0, 1.0], [1.0, 0.0, 1.0]) == 0.0

    def test_always_half_scores_a_quarter(self):
        assert M.brier([0.5] * 4, [1.0, 0.0, 1.0, 0.0]) == 0.25

    def test_confidently_wrong_is_the_worst_case(self):
        assert M.brier([1.0], [0.0]) == 1.0


class TestReliability:
    def test_bins_by_predicted_probability(self):
        pred = [0.05, 0.15, 0.95, 0.96]
        outcome = [0.0, 0.0, 1.0, 0.0]
        table = M.reliability(pred, outcome, bins=10)

        assert [row["bin_low"] for row in table] == [0.0, 0.1, 0.9]
        top = table[-1]
        assert top["n"] == 2
        assert top["predicted"] == pytest.approx(0.955)
        assert top["observed"] == 0.5     # called ~96%, happened half the time

    def test_empty_bins_are_omitted_not_reported_as_zero(self):
        assert M.reliability([0.05], [1.0], bins=10) == [
            {"bin_low": 0.0, "bin_high": 0.1, "n": 1, "predicted": 0.05, "observed": 1.0}
        ]


class TestActualPoints:
    def test_half_ppr_backs_the_reception_value_out_of_the_nflverse_total(self):
        record = {"ppr": 20.0, "receptions": 6.0}
        assert actual_points(record, 1.0) == 20.0
        assert actual_points(record, 0.5) == 17.0
        assert actual_points(record, 0.0) == 14.0


def _records(n_weeks=6, ppr_points=15.0):
    """Synthetic logs. Players differ so head-to-heads are not all ties."""
    return [
        {"player_id": f"p{p}", "position": "WR", "season": 2024, "week": w,
         "team": "BUF", "opponent": "MIA",
         "ppr": ppr_points + p, "receptions": 5.0,
         "targets": 8.0 + p, "carries": 0.0, "attempts": 0.0,
         "receiving_yards": 70.0 + 5 * p, "receiving_tds": 0.5,
         "rushing_yards": 0.0, "rushing_tds": 0.0, "passing_yards": 0.0,
         "passing_tds": 0.0, "interceptions": 0.0, "touches": 8.0 + p}
        for p in range(20) for w in range(1, n_weeks + 1)
    ]


class TestSamples:
    def test_projection_uses_only_prior_weeks(self):
        records = _records()
        # Make the predicted week an outlier: if it leaked into its own
        # projection, the mean would move toward it.
        for r in records:
            if r["week"] == 5:
                r["ppr"] = 100.0
                r["targets"] = 40.0

        samples = build_samples(records, start_week=5, min_prior=3, min_trailing=1.0)
        week5 = [s for s in samples if s["week"] == 5]
        assert week5
        assert all(s["mean"] < 40 for s in week5)

    def test_band_is_symmetric_around_the_mean(self):
        samples = build_samples(_records(), start_week=4, min_prior=3, min_trailing=1.0)
        assert samples
        for s in samples:
            assert s["ceiling"] - s["mean"] == pytest.approx(s["mean"] - s["floor"], abs=0.11)
            assert s["sd"] > 0


class TestCoverage:
    def test_counts_which_side_the_miss_is_on(self):
        samples = [
            {"mean": 10.0, "sd": 2.0, "actual": 10.0},   # inside
            {"mean": 10.0, "sd": 2.0, "actual": 3.0},    # below
            {"mean": 10.0, "sd": 2.0, "actual": 20.0},   # above
            {"mean": 10.0, "sd": 2.0, "actual": 11.0},   # inside
        ]
        result = coverage(samples)
        assert result["inside"] == 0.5
        assert result["below_floor"] == 0.25
        assert result["above_ceiling"] == 0.25

    def test_a_wider_scale_covers_more(self):
        samples = [{"mean": 10.0, "sd": 2.0, "actual": 15.0}]
        assert coverage(samples, 1.0)["inside"] == 0.0
        assert coverage(samples, 3.0)["inside"] == 1.0

    def test_empty_input_does_not_divide_by_zero(self):
        assert coverage([])["n"] == 0


class TestMatchups:
    def test_matchups_pair_players_from_the_same_week(self):
        samples = build_samples(_records(), start_week=4, min_prior=3, min_trailing=1.0)
        matchups = build_matchups(samples, lineup_size=2, repeats=2)
        assert matchups
        assert all(0.0 <= m["p_win"] <= 1.0 for m in matchups)
        assert all(m["won"] in (0.0, 1.0) for m in matchups)

    def test_is_deterministic_for_a_fixed_seed(self):
        samples = build_samples(_records(), start_week=4, min_prior=3, min_trailing=1.0)
        first = build_matchups(samples, lineup_size=2, seed=1, repeats=2)
        second = build_matchups(samples, lineup_size=2, seed=1, repeats=2)
        assert [m["p_win"] for m in first] == [m["p_win"] for m in second]

    def test_too_few_players_in_a_week_yields_no_matchups(self):
        samples = build_samples(_records(), start_week=4, min_prior=3, min_trailing=1.0)
        assert build_matchups(samples, lineup_size=500) == []
