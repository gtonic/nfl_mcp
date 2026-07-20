"""Unit tests for the backtest metric functions."""

import os
import sys

# The evals/ package lives at the repo root (not installed with nfl_mcp).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals.backtest import metrics as M


def test_perfect_prediction():
    actual = [1.0, 2.0, 3.0, 4.0]
    r = M.evaluate(actual, actual)
    assert r["mae"] == 0.0
    assert r["rmse"] == 0.0
    assert r["spearman"] == 1.0
    assert r["pearson"] == 1.0
    assert r["r2"] == 1.0


def test_mae_rmse_bias():
    pred = [2.0, 2.0, 2.0]
    actual = [1.0, 2.0, 4.0]  # pred-actual = +1, 0, -2
    assert M.mae(pred, actual) == 1.0            # (1+0+2)/3
    assert round(M.bias(pred, actual), 3) == round((1 + 0 - 2) / 3, 3)  # -0.333
    assert round(M.rmse(pred, actual), 4) == round((5 / 3) ** 0.5, 4)


def test_spearman_monotonic_and_reverse():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y_up = [10.0, 20.0, 30.0, 40.0, 50.0]     # perfectly monotonic (non-linear ok)
    y_down = [50.0, 40.0, 30.0, 20.0, 10.0]
    assert M.spearman(x, y_up) == 1.0
    assert M.spearman(x, y_down) == -1.0


def test_spearman_handles_ties():
    x = [1.0, 1.0, 2.0, 3.0]
    y = [5.0, 5.0, 9.0, 12.0]
    # ties shouldn't blow up; strong positive association
    assert M.spearman(x, y) > 0.9


def test_empty_is_safe():
    assert M.mae([], []) == 0.0
    assert M.evaluate([], [])["n"] == 0
