"""
Rest-of-season calibration backtest.

QUESTION
    Is the per-game rate ``ros.py`` prices later weeks at unbiased, and how much
    should a few games of opportunity be regressed toward the rank prior?

METHOD (leak-free)
    As of week W, for every QB/RB/WR/TE with at least two games before W:
        opp    = the opportunity base on games < W (``opportunity.project_opportunity``)
        bucket = ``projections.base_ppg`` at the player's rank. The live engine
                 uses the FantasyCalc rank; here the proxy is the player's rank
                 by points per game in the previous season (unranked: no rank).
        rate   = ros.regressed_rate(opp, bucket, games)   (the live blend)
    Ground truth = his mean PPR points per game played in weeks W+1..17 (at
    least three games), which is what ``per_game`` stands for: byes and
    absences are priced separately.

    Reported for everyone and for "starters" (the top 12 QB/TE and 30 RB/WR by
    the opportunity base as of W) — the players a lineup is built from, where a
    too-strong pull toward the prior shows up as a systematic under-projection.
    And for "ranked" players (with a previous-season rank): the unranked are
    mostly rookies, whom the proxy prices at the bottom bucket while the market
    ranks them, so their prior error is an artefact of the proxy.

RUN
    python -m evals.backtest.ros_backtest --seasons 2023 2024 --as-of 3 4 5 6
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from nfl_mcp.opportunity import project_opportunity
from nfl_mcp.projections import base_ppg
from nfl_mcp.ros import PRIOR_GAMES, regressed_rate

from .data import load_season
from .metrics import bias, mae

_POSITIONS = ("QB", "RB", "WR", "TE")
_STARTERS = {"QB": 12, "TE": 12, "RB": 30, "WR": 30}


def _prior_ranks(records: list[dict]) -> dict[str, int]:
    """{player_id: positional rank by PPR points per game} (min. 4 games)."""
    games: dict[str, list[float]] = defaultdict(list)
    pos: dict[str, str] = {}
    for r in records:
        games[r["player_id"]].append(r["ppr"])
        pos[r["player_id"]] = r["position"]
    by_pos: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for pid, pts in games.items():
        if len(pts) >= 4:
            by_pos[pos[pid]].append((sum(pts) / len(pts), pid))
    ranks: dict[str, int] = {}
    for rows in by_pos.values():
        for i, (_, pid) in enumerate(sorted(rows, reverse=True), start=1):
            ranks[pid] = i
    return ranks


def build_samples(season: int, as_of: int, last_week: int = 17) -> list[dict]:
    records = load_season(season)
    ranks = _prior_ranks(load_season(season - 1))
    by_player: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_player[r["player_id"]].append(r)
    samples = []
    for pid, games in by_player.items():
        position = games[0]["position"]
        if position not in _POSITIONS:
            continue
        prior_games = [g for g in games if g["week"] < as_of]
        later = [g["ppr"] for g in games if as_of < g["week"] <= last_week]
        if len(prior_games) < 2 or len(later) < 3:
            continue
        opp = project_opportunity(prior_games, position)
        if opp is None:
            continue
        samples.append({
            "season": season, "as_of": as_of, "position": position,
            "opp": opp, "games": min(len(prior_games), 6),
            "prior": base_ppg(position, ranks.get(pid)), "ranked": pid in ranks,
            "actual": sum(later) / len(later),
        })
    # Starters: the top of each position by the as-of opportunity base.
    for position, n in _STARTERS.items():
        rows = sorted((s for s in samples if s["position"] == position),
                      key=lambda s: s["opp"], reverse=True)
        for s in rows[:n]:
            s["starter"] = True
    return samples


def _report(samples: list[dict], predict, label: str) -> None:
    for subset, pick in (("all", lambda s: True), ("starters", lambda s: s.get("starter")),
                         ("ranked", lambda s: s["ranked"])):
        cells = []
        for position in (*_POSITIONS, "ALL"):
            rows = [s for s in samples if pick(s) and position in ("ALL", s["position"])]
            if not rows:
                continue
            pred = [predict(s) for s in rows]
            act = [s["actual"] for s in rows]
            cells.append(f"{position} mae {mae(pred, act):4.2f} bias {bias(pred, act):+5.2f}")
        print(f"  {label:24s} {subset:9s} " + " | ".join(cells))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", type=int, nargs="+", default=[2023, 2024])
    ap.add_argument("--as-of", type=int, nargs="+", default=[3, 4, 5, 6])
    ap.add_argument("--prior-games", type=float, nargs="*", default=[1, 2, 3])
    args = ap.parse_args()
    for as_of in args.as_of:
        samples = [s for season in args.seasons for s in build_samples(season, as_of)]
        n_start = sum(1 for s in samples if s.get("starter"))
        print(f"as of week {as_of}: n={len(samples)} (starters {n_start})")
        _report(samples, lambda s: s["opp"], "opportunity only")
        _report(samples, lambda s: s["prior"], "prior only")
        _report(samples, lambda s: regressed_rate(s["opp"], s["prior"], s["games"]),
                f"live: k={PRIOR_GAMES}")
        for k in args.prior_games:
            _report(samples, lambda s, k=k: regressed_rate(s["opp"], s["prior"], s["games"], k),
                    f"regressed k={k}")


if __name__ == "__main__":
    main()
