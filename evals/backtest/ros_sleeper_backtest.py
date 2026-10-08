"""
ROS with Sleeper: does Sleeper's projection make the rest-of-season rate better?

QUESTION
    ``ros.py`` prices every week after the current one as a blend of our
    per-game rate (opportunity regressed toward the rank prior, the model
    ``ros_backtest`` calibrates) and Sleeper's projection *for that week*
    (``ros.ROS_MODEL_WEIGHT`` ours). Is that better than our rate alone, and is
    the weekly blend's 0.25 the right share over a season?

THE DATA LIMIT
    Sleeper's history endpoint returns, for each past week, the last
    projection it published *for that week* (a pre-game number). It keeps no
    snapshot of what it projected for week 12 back in week 5, so the live
    input -- week-W+k projections as published at W -- cannot be replayed.
    Two stand-ins bracket it:

    sleeper_now    LEAK-FREE. Sleeper's week-W line (known at W) as a flat
                   per-game rate for every later week -- a lower bound on
                   what its later-week numbers can add (they at least carry
                   the opponent and the depth chart as of W, which is what
                   this one does).
    sleeper_oracle UPPER BOUND, NOT LEAK-FREE. The mean of Sleeper's same-week
                   projections over the later weeks he played: each was made
                   the week of the game, with that week's depth chart, role
                   and news. Live ROS can only approach it.

METHOD
    As of week W (``--as-of``), every QB/RB/WR/TE with >= 2 games before W and
    >= 3 games in W+1..17 (``ros_backtest.build_samples``' population), truth =
    his mean points per game played over W+1..17 in Sleeper's default full-PPR
    scoring (``data.TRUTH_SCORING``) -- the per-game rate ROS prices, byes and
    absences being priced separately. Sleeper numbers are priced in the same
    scoring with production's ``sleeper_projections.points_for``, matched by
    name + team. A player Sleeper does not project at W (no row, or listed
    without points) is scored on the model alone in every variant, so all
    variants are compared on the same rows.

    Reported per position: MAE / bias for model, sleeper_now, the blend at
    the live weight, a sweep of the model weight, and the oracle variants.

RUN (network on the first run: Sleeper weeks are cached by sleeper_blend)
    python -m evals.backtest.ros_sleeper_backtest --seasons 2023 2024 2025 --as-of 4 6 8 10
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from nfl_mcp import sleeper_projections as sp
from nfl_mcp.opportunity import project_opportunity
from nfl_mcp.projections import base_ppg
from nfl_mcp.ros import ROS_MODEL_WEIGHT, regressed_rate

from .data import TRUTH_SCORING, load_season
from .metrics import bias, mae
from .ros_backtest import _prior_ranks
from .sleeper_blend import load_sleeper_week

_POSITIONS = ("QB", "RB", "WR", "TE")
_LAST_WEEK = 17
_WEIGHTS = (0.0, 0.1, 0.25, 0.4, 0.5, 0.75, 1.0)


def _sleeper(index: dict, name: str, team: str) -> float | None:
    pts, status = sp.points_for(index, TRUTH_SCORING, name=name, team=team)
    return pts if status == "projected" else None


def build_samples(season: int, as_of: int, weeks: dict[int, dict]) -> list[dict]:
    records = load_season(season)
    ranks = _prior_ranks(load_season(season - 1))
    by_player: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_player[r["player_id"]].append(r)
    out = []
    for pid, games in by_player.items():
        games.sort(key=lambda g: g["week"])
        position = games[0]["position"]
        if position not in _POSITIONS:
            continue
        prior = [g for g in games if g["week"] < as_of]
        later = [g for g in games if as_of < g["week"] <= _LAST_WEEK]
        if len(prior) < 2 or len(later) < 3:
            continue
        opp = project_opportunity(prior, position)
        if opp is None:
            continue
        model = regressed_rate(opp, base_ppg(position, ranks.get(pid)), min(len(prior), 6))
        name, team_now = games[0]["player"], prior[-1]["team"]
        now = _sleeper(weeks[as_of], name, team_now)
        oracle_pts = [p for g in later
                      if (p := _sleeper(weeks[g["week"]], name, g["team"])) is not None]
        out.append({
            "season": season, "as_of": as_of, "position": position,
            "model": model, "sleeper_now": now,
            "sleeper_oracle": (sum(oracle_pts) / len(oracle_pts)
                               if len(oracle_pts) >= 2 else None),
            "actual": sum(g["ppr"] for g in later) / len(later),
        })
    return out


def _blend(model: float, sleeper: float | None, w: float) -> float:
    return model if sleeper is None else w * model + (1 - w) * sleeper


def report(samples: list[dict]) -> None:
    variants = [
        ("model (live ROS rate)", lambda s: s["model"]),
        ("sleeper_now (flat)", lambda s: _blend(s["model"], s["sleeper_now"], 0.0)),
        (f"blend_now w={ROS_MODEL_WEIGHT:g} (live)",
         lambda s: _blend(s["model"], s["sleeper_now"], ROS_MODEL_WEIGHT)),
        ("sleeper_oracle (upper bd)", lambda s: _blend(s["model"], s["sleeper_oracle"], 0.0)),
        (f"blend_oracle w={ROS_MODEL_WEIGHT:g}",
         lambda s: _blend(s["model"], s["sleeper_oracle"], ROS_MODEL_WEIGHT)),
    ]
    variants += [(f"  sweep blend_now w={w:g}",
                  lambda s, w=w: _blend(s["model"], s["sleeper_now"], w)) for w in _WEIGHTS]
    matched = sum(1 for s in samples if s["sleeper_now"] is not None)
    print(f"  n={len(samples)} (Sleeper-projected at W: {matched})")
    for label, fn in variants:
        cells = []
        for position in (*_POSITIONS, "ALL"):
            rows = [s for s in samples if position in ("ALL", s["position"])]
            if not rows:
                continue
            pred, act = [fn(s) for s in rows], [s["actual"] for s in rows]
            cells.append(f"{position} {mae(pred, act):4.2f}/{bias(pred, act):+5.2f}")
        print(f"  {label:30s} " + " | ".join(cells))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seasons", type=int, nargs="+", default=[2023, 2024, 2025])
    ap.add_argument("--as-of", type=int, nargs="+", default=[4, 6, 8, 10])
    args = ap.parse_args()
    print("cells: MAE/bias (pred - actual) of points per game over W+1..17")
    pooled: list[dict] = []
    for as_of in args.as_of:
        samples = []
        for season in args.seasons:
            weeks = {w: load_sleeper_week(season, w) for w in range(as_of, _LAST_WEEK + 1)}
            samples += build_samples(season, as_of, weeks)
        print(f"as of week {as_of}:")
        report(samples)
        pooled += samples
    print("pooled:")
    report(pooled)


if __name__ == "__main__":
    main()
