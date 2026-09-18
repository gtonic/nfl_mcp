"""Error/correlation metrics for backtests — pure stdlib, no numpy/pandas.

All functions take two equal-length sequences of floats: ``pred`` (what the model
said) and ``actual`` (what really happened). Higher-is-better metrics and
lower-is-better metrics are documented per function.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def mae(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Mean Absolute Error (lower is better). Average size of the miss, in points."""
    n = len(pred)
    return sum(abs(p - a) for p, a in zip(pred, actual, strict=False)) / n if n else 0.0


def rmse(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Root Mean Squared Error (lower is better). Penalises big misses more."""
    n = len(pred)
    return math.sqrt(sum((p - a) ** 2 for p, a in zip(pred, actual, strict=False)) / n) if n else 0.0


def bias(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Mean signed error pred-actual. >0 = we over-predict, <0 = we under-predict."""
    n = len(pred)
    return sum(p - a for p, a in zip(pred, actual, strict=False)) / n if n else 0.0


def pearson(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Pearson correlation (higher is better, -1..1). Linear agreement."""
    n = len(pred)
    if n < 2:
        return 0.0
    mp = sum(pred) / n
    ma = sum(actual) / n
    cov = sum((p - mp) * (a - ma) for p, a in zip(pred, actual, strict=False))
    vp = sum((p - mp) ** 2 for p in pred)
    va = sum((a - ma) ** 2 for a in actual)
    denom = math.sqrt(vp * va)
    return cov / denom if denom else 0.0


def _ranks(values: Sequence[float]) -> list[float]:
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
    ss_res = sum((a - p) ** 2 for p, a in zip(pred, actual, strict=False))
    return 1 - ss_res / ss_tot if ss_tot else 0.0


def brier(pred: Sequence[float], outcome: Sequence[float]) -> float:
    """Brier score for probabilistic predictions (lower is better, 0..1).

    ``outcome`` is 1.0 when the predicted event happened, 0.0 otherwise. Always
    predicting the base rate scores ``p(1-p)`` — roughly 0.25 for a coin flip —
    so a win-probability model that cannot beat 0.25 is adding nothing.
    """
    n = len(pred)
    return sum((p - o) ** 2 for p, o in zip(pred, outcome, strict=False)) / n if n else 0.0


def reliability(
    pred: Sequence[float], outcome: Sequence[float], bins: int = 10
) -> list[dict[str, float]]:
    """Reliability table: predicted probability vs the rate actually observed.

    Calibration is the property that of everything called 70%, about 70% happens.
    A model can rank perfectly and still be badly calibrated — which is the
    failure that matters when the number is shown to someone as a probability.
    """
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for p, o in zip(pred, outcome, strict=False):
        idx = min(bins - 1, max(0, int(p * bins)))
        buckets[idx].append((p, o))
    table = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        table.append({
            "bin_low": round(i / bins, 2),
            "bin_high": round((i + 1) / bins, 2),
            "n": len(bucket),
            "predicted": round(sum(p for p, _ in bucket) / len(bucket), 4),
            "observed": round(sum(o for _, o in bucket) / len(bucket), 4),
        })
    return table


def evaluate(pred: Sequence[float], actual: Sequence[float]) -> dict[str, float]:
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
