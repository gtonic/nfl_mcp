"""Calibration of the weekly blend: a shrunk multiplier per position x tier.

The weekly projection is the Sleeper-first blend (``sleeper_projections.blend``).
Graded against what players really scored (``projection_accuracy``), it can
run systematically high or low for one kind of player, for example top-12
team defenses, while being right on average. This module holds the
correction for that: one multiplier per (position, tier), applied to the
blended number after everything else (``projections._apply_sleeper_blend``,
and each later ROS week's blend in ``ros._later_week``).

**Tier** is the player's rank at his position in Sleeper's projections for
that week, priced in the league's own scoring: ``T1`` top 12 (QB, TE, K, DEF)
or top 24 (RB, WR), ``T2`` the next 12 / 24, ``T3`` the rest. A player Sleeper
does not project has no tier and is left alone.

**Fit** (``evals/backtest/weekly_calibration.py``): per cell, the ratio
``sum(actual) / sum(projected)`` over Sleeper's 2026 projections priced in
the user's leagues' scoring (every projected player, the ones who sat scored
0), shrunk toward 1 as ``1 + (ratio - 1) * n / (n + SHRINK_K)`` and capped
to ``[CAP_LOW, CAP_HIGH]``. A cell is kept only when it has at least
``MIN_N`` rows and the correction lowered the held-out MAE (fit on the
early weeks, graded on the last two). Everything else stays at 1.0. The
table below is the refit on all weeks of the cells that passed; refresh it
weekly with ``python -m evals.backtest.weekly_calibration --db nfl_data.db
--write``.

The applied multiplier is recorded on each projection
(``breakdown.calibration_mult``, ``calibration_tier``) and in the projection
log's signals, so the next fit divides it back out instead of fitting on its
own output.

**Team defenses** get one more, earlier correction, on Sleeper's stat line
itself (``def_components``, applied by ``sleeper_projections.price_stats``
to any line with ``pts_allow``): the projected count of each scoring event
(sacks, takeaways, defensive / return TDs, blocked kicks) times the shrunk
ratio of real to projected counts. Points allowed need none -- Sleeper's
projected points allowed matched the real ones in every band in 2026 (17.4 vs
17.1 below 19, 27.1 vs 26.4 above 25) and the tier pricing around them
(``scoring._tier_probability``, sd 9.5) matched the real tier points -- but
defensive TDs came in at half the projected rate, so the top-12 defenses
were projected ~14% high. Kept only when the held-out DEF MAE improved; the
tier cells for DEF are then fitted on top of it.
"""
from __future__ import annotations

from typing import Any

# Rows per tier for each position's T1 and T2.
TIER_SIZE = {"QB": 12, "TE": 12, "K": 12, "DEF": 12, "RB": 24, "WR": 24}
TIERS = ("T1", "T2", "T3")

SHRINK_K = 200
CAP_LOW, CAP_HIGH = 0.85, 1.10
MIN_N = 60
# A defense's scoring events the component correction scales (see module doc),
# and its own bounds: a rare event's ratio swings more than a tier's.
DEF_EVENTS = ("sack", "int", "fum_rec", "ff", "def_td", "safe", "blk_kick",
              "def_st_td", "def_st_fum_rec", "def_st_ff")
DEF_CAP_LOW, DEF_CAP_HIGH = 0.5, 1.5

# --- fitted table (rewritten by evals/backtest/weekly_calibration.py --write) ---
# Fitted 2026-10-29 on 2026 weeks 1-7: 4107 Sleeper-projected player-weeks in the graded leagues' scoring;
# held-out MAE gate on the last weeks; K=200, cap [0.85, 1.1], min n 60. raw = unshrunk ratio;
# def_components n = team-weeks.
CALIBRATION: dict[str, Any] = {
    "fitted": "2026-10-29",
    "season": 2026,
    "weeks": [1, 2, 3, 4, 5, 6, 7],
    "cells": {
        "QB": {
            "T2": {"mult": 0.997, "raw": 0.992, "n": 168},
        },
        "RB": {
            "T2": {"mult": 0.94, "raw": 0.904, "n": 336},
            "T3": {"mult": 0.975, "raw": 0.956, "n": 264},
        },
        "WR": {
            "T2": {"mult": 0.958, "raw": 0.933, "n": 336},
        },
        "TE": {
            "T2": {"mult": 0.972, "raw": 0.938, "n": 168},
        },
        "K": {
            "T1": {"mult": 1.027, "raw": 1.091, "n": 84},
        },
        "DEF": {
            "T1": {"mult": 0.956, "raw": 0.903, "n": 168},
            "T2": {"mult": 0.993, "raw": 0.985, "n": 168},
        },
    },
    "def_components": {
        "sack": {"mult": 0.957, "raw": 0.918, "n": 214},
        "int": {"mult": 0.993, "raw": 0.986, "n": 214},
        "fum_rec": {"mult": 0.934, "raw": 0.872, "n": 214},
        "ff": {"mult": 1.075, "raw": 1.144, "n": 214},
        "def_td": {"mult": 0.724, "raw": 0.467, "n": 214},
        "safe": {"mult": 0.86, "raw": 0.73, "n": 214},
        "blk_kick": {"mult": 1.088, "raw": 1.17, "n": 214},
    },
}
# --- end fitted table ---


def tier_for(position: str | None, rank: int | None) -> str | None:
    """``T1`` / ``T2`` / ``T3`` for a positional rank; None without one."""
    pos = (position or "").upper()
    if pos == "DST":
        pos = "DEF"
    size = TIER_SIZE.get(pos)
    if size is None or not rank or rank < 1:
        return None
    if rank <= size:
        return "T1"
    if rank <= 2 * size:
        return "T2"
    return "T3"


def _position_of(row: dict) -> str | None:
    pos = (row.get("position") or "").upper() or None
    pid = str(row.get("player_id") or "")
    if pos is None and pid.isalpha():
        pos = "DEF"
    return "DEF" if pos == "DST" else pos


# (id(index), fingerprint) -> (index, {(position, key): rank}); one week's
# ranks are computed once per index object and scoring. The index is kept and
# checked by identity: an id can be reused once its object is gone.
_rank_cache: dict[tuple[int, str], tuple[dict, dict[tuple[str, str], int]]] = {}
_RANK_CACHE_MAX = 64


def _key(position: str, row: dict) -> str:
    if position == "DEF":
        return str(row.get("team") or row.get("player_id") or "")
    return str(row.get("player_id") or "")


def position_ranks(index: dict, model) -> dict[tuple[str, str], int]:
    """``{(position, player_id | team code for DEF): rank}`` for one Sleeper week,
    ranked by projected points in `model`'s scoring (players at 0 unranked)."""
    from .sleeper_projections import price_stats
    cache_key = (id(index), getattr(model, "fingerprint", ""))
    if (hit := _rank_cache.get(cache_key)) is not None and hit[0] is index:
        return hit[1]
    by_pos: dict[str, list[tuple[float, str]]] = {}
    seen: set[tuple[str, str]] = set()
    rows = list((index.get("by_id") or {}).values()) + list((index.get("by_def") or {}).values())
    for row in rows:
        pos = _position_of(row)
        if pos not in TIER_SIZE:
            continue
        key = _key(pos, row)
        if not key or (pos, key) in seen:
            continue
        seen.add((pos, key))
        pts = price_stats(row.get("stats") or {}, model)
        if pts > 0:
            by_pos.setdefault(pos, []).append((pts, key))
    ranks: dict[tuple[str, str], int] = {}
    for pos, items in by_pos.items():
        items.sort(key=lambda t: -t[0])
        for i, (_, key) in enumerate(items):
            ranks[(pos, key)] = i + 1
    if len(_rank_cache) >= _RANK_CACHE_MAX:
        _rank_cache.clear()
    _rank_cache[cache_key] = (index, ranks)
    return ranks


def rank_of(index: dict, model, *, player_id: str | None = None, name: str | None = None,
            team: str | None = None, position: str | None = None) -> int | None:
    """The player's positional rank in Sleeper's week (None when not projected)."""
    from .sleeper_projections import lookup
    if not (index or {}).get("by_id"):
        return None
    row = lookup(index, player_id=player_id, name=name, team=team, position=position)
    if not row:
        return None
    pos = _position_of({**row, "position": row.get("position") or position})
    if pos not in TIER_SIZE:
        return None
    return position_ranks(index, model).get((pos, _key(pos, row)))


def multiplier(position: str | None, rank: int | None,
               table: dict | None = None) -> tuple[float, str | None]:
    """``(multiplier, tier)`` for a player; 1.0 for a cell with no correction."""
    tier = tier_for(position, rank)
    if tier is None:
        return 1.0, None
    pos = (position or "").upper()
    pos = "DEF" if pos == "DST" else pos
    cells = (table if table is not None else CALIBRATION).get("cells") or {}
    cell = (cells.get(pos) or {}).get(tier) or {}
    try:
        mult = float(cell.get("mult", 1.0))
    except (TypeError, ValueError):
        mult = 1.0
    return max(CAP_LOW, min(CAP_HIGH, mult)), tier


def calibrate(points: float, position: str | None, rank: int | None,
              table: dict | None = None) -> tuple[float, float, str | None]:
    """``(calibrated points, multiplier, tier)``."""
    mult, tier = multiplier(position, rank, table)
    return (round(points * mult, 1) if mult != 1.0 else points), mult, tier


def defense_line(stats: dict, table: dict | None = None) -> dict:
    """A Sleeper DEF stat line with each scoring event's projected count
    times its fitted ratio (``def_components``); any other line unchanged."""
    if "pts_allow" not in stats:
        return stats
    comps = (table if table is not None else CALIBRATION).get("def_components") or {}
    if not comps:
        return stats
    out = dict(stats)
    for key, cell in comps.items():
        value = out.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            mult = max(DEF_CAP_LOW, min(DEF_CAP_HIGH, float(cell.get("mult", 1.0))))
            out[key] = value * mult
    return out


def summary() -> dict:
    """What the active table is, for a tool's notes."""
    cells = {pos: {t: c.get("mult") for t, c in tiers.items()}
             for pos, tiers in (CALIBRATION.get("cells") or {}).items()}
    events = {e: c.get("mult") for e, c in (CALIBRATION.get("def_components") or {}).items()}
    return {"fitted": CALIBRATION.get("fitted"), "season": CALIBRATION.get("season"),
            "weeks": CALIBRATION.get("weeks"), "multipliers": cells,
            "def_event_multipliers": events,
            "source": "evals/backtest/weekly_calibration.py"}
