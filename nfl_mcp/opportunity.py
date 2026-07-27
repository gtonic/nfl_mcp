"""Opportunity-based weekly projection.

The projection lever isn't more multipliers — the backtest shows those barely
move accuracy — it's a better *baseline*. Volume (targets, carries, pass
attempts) is far more stable week to week than fantasy points, whose variance is
dominated by touchdowns and yardage spikes. So instead of projecting a player's
trailing points, we project their trailing *opportunity* and convert it to points
via an efficiency rate that is shrunk toward a position prior (so a small sample
of hot/cold weeks doesn't dominate).

    expected_points = exp_targets · ppt + exp_carries · ppc          (RB/WR/TE)
    expected_points = exp_attempts · ppa + exp_carries · ppc         (QB)

where exp_* are recency-weighted trailing volumes and pp* are the player's own
points-per-opportunity shrunk toward a position prior. All PPR.

This module is pure and unit-testable; whether it becomes the live baseline is
decided by the backtest (see ``evals/backtest``), not asserted.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Standard PPR scoring weights (match nflverse `fantasy_points_ppr`).
PASS_YD, PASS_TD, INT = 0.04, 4.0, -2.0
RUSH_YD, RUSH_TD = 0.1, 6.0
REC, REC_YD, REC_TD = 1.0, 0.1, 6.0

# Position priors: PPR points per opportunity (per target / carry / pass attempt).
_PRIORS: Dict[str, Dict[str, float]] = {
    "WR": {"ppt": 1.55, "ppc": 0.50},
    "TE": {"ppt": 1.35, "ppc": 0.50},
    "RB": {"ppt": 1.45, "ppc": 0.62},
    "QB": {"ppa": 0.45, "ppc": 0.75},
}
# Shrinkage strength, in opportunity units: a player needs ~this many targets/
# carries/attempts before their own efficiency outweighs the position prior.
_K_TARGETS, _K_CARRIES, _K_ATTEMPTS = 20.0, 25.0, 60.0

DEFAULT_LOOKBACK = 6
OPPORTUNITY_POSITIONS = ("QB", "RB", "WR", "TE")


def rec_points(g: Dict) -> float:
    return (g.get("receptions", 0.0) * REC
            + g.get("receiving_yards", 0.0) * REC_YD
            + g.get("receiving_tds", 0.0) * REC_TD)


def rush_points(g: Dict) -> float:
    return g.get("rushing_yards", 0.0) * RUSH_YD + g.get("rushing_tds", 0.0) * RUSH_TD


def pass_points(g: Dict) -> float:
    return (g.get("passing_yards", 0.0) * PASS_YD
            + g.get("passing_tds", 0.0) * PASS_TD
            + g.get("interceptions", 0.0) * INT)


def _weighted_mean(values: List[float], weights: List[float]) -> float:
    tw = sum(weights)
    return sum(v * w for v, w in zip(weights, values, strict=False)) / tw if tw else 0.0


def _shrunk_rate(total_points: float, total_volume: float, prior: float, k: float) -> float:
    """Player's points-per-opportunity, shrunk toward the position prior."""
    return (total_points + k * prior) / (total_volume + k)


def project_opportunity(
    prior_games: List[Dict],
    position: str,
    lookback: int = DEFAULT_LOOKBACK,
) -> Optional[float]:
    """Expected PPR points for the next game from trailing opportunity.

    Args:
        prior_games: the player's earlier weekly stat dicts (each with targets,
            carries, attempts, receptions, *_yards, *_tds, interceptions, week).
            MUST contain only games before the one being predicted (leak-free).
        position: QB/RB/WR/TE.
        lookback: how many most-recent games to weight (recency-weighted linearly).

    Returns expected points, or None if the position/data can't be projected.
    """
    pos = position.upper()
    priors = _PRIORS.get(pos)
    if priors is None or not prior_games:
        return None

    games = sorted(prior_games, key=lambda g: g.get("week", 0))[-lookback:]
    n = len(games)
    # Recency weights: oldest .. newest -> 1 .. n.
    weights = list(range(1, n + 1))

    exp_carries = _weighted_mean([g.get("carries", 0.0) for g in games], weights)
    tot_carries = sum(g.get("carries", 0.0) for g in games)
    tot_rush_pts = sum(rush_points(g) for g in games)
    ppc = _shrunk_rate(tot_rush_pts, tot_carries, priors["ppc"], _K_CARRIES)

    if pos == "QB":
        exp_attempts = _weighted_mean([g.get("attempts", 0.0) for g in games], weights)
        tot_attempts = sum(g.get("attempts", 0.0) for g in games)
        tot_pass_pts = sum(pass_points(g) for g in games)
        ppa = _shrunk_rate(tot_pass_pts, tot_attempts, priors["ppa"], _K_ATTEMPTS)
        return max(0.0, exp_attempts * ppa + exp_carries * ppc)

    exp_targets = _weighted_mean([g.get("targets", 0.0) for g in games], weights)
    tot_targets = sum(g.get("targets", 0.0) for g in games)
    tot_rec_pts = sum(rec_points(g) for g in games)
    ppt = _shrunk_rate(tot_rec_pts, tot_targets, priors["ppt"], _K_TARGETS)
    return max(0.0, exp_targets * ppt + exp_carries * ppc)
