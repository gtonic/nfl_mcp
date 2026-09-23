"""
Rank-bucket calibration: what do players at a positional rank score per game?

QUESTION
    ``projections.base_ppg`` prices a player by his positional rank before he
    has usage, and ``ros.py`` regresses the opportunity base toward it. Both
    read it as points per game *played*. Is each tier at that level?

METHOD
    The live rank is the FantasyCalc market rank; the historical proxy is the
    player's rank by PPR points per game in the previous season (at least four
    games, as ``ros_backtest`` does). For every ranked QB/RB/WR/TE, per tier:
        season = mean points per game played over weeks 1-17 (>= 4 games)
        ros    = mean points per game played over weeks W+1..17 for players
                 with >= 2 games before W and >= 3 after (W = 3..6 pooled) —
                 the population the ROS prior is used on
    in full PPR against the bucket, and in half PPR against the bucket rebased
    by ``_RECEPTION_SHARE`` (the reference the rebasing is checked on).

    Unranked players are reported apart: in the proxy they are mostly rookies
    (no previous season), whom the market does rank, so they say nothing about
    the bottom bucket.

RUN
    python -m evals.backtest.bucket_calibration --seasons 2023 2024
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from statistics import mean

from nfl_mcp.projections import _RANK_BUCKETS, base_ppg

from .data import load_season
from .ros_backtest import _prior_ranks

_POSITIONS = ("QB", "RB", "WR", "TE")


def half_ppr(record: dict) -> float:
    return record["ppr"] - 0.5 * record.get("receptions", 0.0)


def tier_index(position: str, rank: int | None) -> int | None:
    """Index into ``_RANK_BUCKETS[position]`` for a rank; None when unranked."""
    if rank is None:
        return None
    for i, (last, _) in enumerate(_RANK_BUCKETS[position]):
        if last is None or rank <= last:
            return i
    return len(_RANK_BUCKETS[position]) - 1


def tier_label(position: str, index: int) -> str:
    tiers = _RANK_BUCKETS[position]
    lo = tiers[index - 1][0] + 1 if index else 1
    last = tiers[index][0]
    return f"{lo}-{last}" if last is not None else f"{lo}+"


def season_rows(records: list[dict], ranks: dict[str, int], min_games: int = 4,
                last_week: int = 17) -> list[dict]:
    """One row per player: his season points per game played (full and half)."""
    games: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if r["week"] <= last_week:
            games[r["player_id"]].append(r)
    rows = []
    for pid, gs in games.items():
        if len(gs) < min_games or gs[0]["position"] not in _POSITIONS:
            continue
        rows.append({"position": gs[0]["position"], "rank": ranks.get(pid),
                     "full": mean(g["ppr"] for g in gs), "half": mean(half_ppr(g) for g in gs)})
    return rows


def ros_rows(records: list[dict], ranks: dict[str, int], as_of: int,
             last_week: int = 17) -> list[dict]:
    """One row per player still playing: points per game played after `as_of`."""
    games: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        games[r["player_id"]].append(r)
    rows = []
    for pid, gs in games.items():
        if gs[0]["position"] not in _POSITIONS:
            continue
        later = [g for g in gs if as_of < g["week"] <= last_week]
        if sum(1 for g in gs if g["week"] < as_of) < 2 or len(later) < 3:
            continue
        rows.append({"position": gs[0]["position"], "rank": ranks.get(pid),
                     "full": mean(g["ppr"] for g in later),
                     "half": mean(half_ppr(g) for g in later)})
    return rows


def tier_means(rows: list[dict], position: str) -> dict:
    """``{tier index | "unranked": {"n", "full", "half"}}`` for one position."""
    cells: dict = defaultdict(list)
    for row in rows:
        if row["position"] == position:
            idx = tier_index(position, row["rank"])
            cells["unranked" if idx is None else idx].append(row)
    return {k: {"n": len(v), "full": mean(r["full"] for r in v),
                "half": mean(r["half"] for r in v)} for k, v in cells.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", type=int, nargs="+", default=[2023, 2024])
    ap.add_argument("--as-of", type=int, nargs="+", default=[3, 4, 5, 6])
    args = ap.parse_args()
    season, ros = [], []
    for s in args.seasons:
        records, ranks = load_season(s), _prior_ranks(load_season(s - 1))
        season += season_rows(records, ranks)
        for w in args.as_of:
            ros += ros_rows(records, ranks, w)
    for position in _POSITIONS:
        sm, rm = tier_means(season, position), tier_means(ros, position)
        print(f"{position}   tier      bucket full | season n  full  half(bucket) "
              f"| ros n  full  half")
        for i, (last, _) in enumerate(_RANK_BUCKETS[position]):
            rank = last if last is not None else 999
            full, half = base_ppg(position, rank, 1.0), base_ppg(position, rank, 0.5)
            s_, r_ = sm.get(i), rm.get(i)
            line = f"     {tier_label(position, i):9s} {full:5.1f}      "
            line += (f"| {s_['n']:4d} {s_['full']:5.2f} {s_['half']:5.2f}({half:5.2f}) "
                     if s_ else "|" + " " * 29)
            line += f"| {r_['n']:4d} {r_['full']:5.2f} {r_['half']:5.2f}" if r_ else "|"
            print(line)
        for label, m in (("season", sm), ("ros", rm)):
            if "unranked" in m:
                u = m["unranked"]
                print(f"     unranked ({label}): n={u['n']} full {u['full']:.2f} "
                      "(mostly rookies: no previous-season rank)")


if __name__ == "__main__":
    main()
