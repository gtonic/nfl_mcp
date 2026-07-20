"""Error/correlation metrics for backtests — pure stdlib, no numpy/pandas.

All functions take two equal-length sequences of floats: ``pred`` (what the model
said) and ``actual`` (what really happened). Higher-is-better metrics and
lower-is-better metrics are documented per function.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple


def mae(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Mean Absolute Error (lower is better). Average size of the miss, in points."""
    n = len(pred)
    return sum(abs(p - a) for p, a in zip(pred, actual)) / n if n else 0.0


def rmse(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Root Mean Squared Error (lower is better). Penalises big misses more."""
    n = len(pred)
    return math.sqrt(sum((p - a) ** 2 for p, a in zip(pred, actual)) / n) if n else 0.0


def bias(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Mean signed error pred-actual. >0 = we over-predict, <0 = we under-predict."""
    n = len(pred)
    return sum(p - a for p, a in zip(pred, actual)) / n if n else 0.0


def pearson(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Pearson correlation (higher is better, -1..1). Linear agreement."""
    n = len(pred)
    if n < 2:
        return 0.0
    mp = sum(pred) / n
    ma = sum(actual) / n
    cov = sum((p - mp) * (a - ma) for p, a in zip(pred, actual))
    vp = sum((p - mp) ** 2 for p in pred)
    va = sum((a - ma) ** 2 for a in actual)
    denom = math.sqrt(vp * va)
    return cov / denom if denom else 0.0


def _ranks(values: Sequence[float]) -> List[float]:
    """Fractional ranks (ties share the average rank)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based average rank for the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Spearman rank correlation (higher is better, -1..1).

    Do we order players the same way reality did? This is the metric that matters
    most for start/sit and rankings (getting the *order* right).
    """
    return pearson(_ranks(pred), _ranks(actual))


def r2(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Coefficient of determination (higher is better, ≤1). Variance explained."""
    n = len(actual)
    if n < 2:
        return 0.0
    ma = sum(actual) / n
    ss_tot = sum((a - ma) ** 2 for a in actual)
    ss_res = sum((a - p) ** 2 for p, a in zip(pred, actual))
    return 1 - ss_res / ss_tot if ss_tot else 0.0


def evaluate(pred: Sequence[float], actual: Sequence[float]) -> Dict[str, float]:
    """Return the full metric bundle for a prediction series."""
    return {
        "n": len(pred),
        "mae": round(mae(pred, actual), 3),
        "rmse": round(rmse(pred, actual), 3),
        "bias": round(bias(pred, actual), 3),
        "pearson": round(pearson(pred, actual), 4),
        "spearman": round(spearman(pred, actual), 4),
        "r2": round(r2(pred, actual), 4),
    }
