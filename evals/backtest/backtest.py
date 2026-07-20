"""
Projection accuracy backtest (Eval Layer A).

QUESTION
    Do the projection engine's *adjustments* (matchup, usage) actually make the
    projection better than a sensible baseline — and are their magnitudes tuned
    right?

METHOD (leak-free, walk-forward)
    For each player/week in the test range we predict that week's PPR points using
    only information available *before* the week:
        base      = the player's trailing average PPR (prior weeks)
        matchup   = base × matchup_multiplier(opponent defense vs position),
                    where the defense ranking is computed from prior weeks only
        usage     = base × usage_multiplier(recent touch trend)
        full      = base × matchup_multiplier × usage_multiplier
    Ground truth = the player's actual PPR points that week (nflverse).

    The multipliers are imported from the LIVE engine (nfl_mcp.projections /
    matchup_tools), so this literally evaluates production's constants. If `full`
    doesn't beat `base`, the adjustments are noise and should change.

METRICS
    MAE / RMSE (lower better), Spearman rank correlation (higher better — did we
    order players like reality did?), bias (over/under prediction).

TUNING
    We also sweep a `matchup strength` scalar s (effective = 1 + s·(mult−1)) and
    report which s minimises MAE. s≈1 ⇒ the live magnitudes are about right;
    s<1 ⇒ we over-adjust; s>1 ⇒ we under-adjust.

RUN
    python -m evals.backtest.backtest --seasons 2024 --start-week 5 --min-trailing 5
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from typing import Dict, List, Optional

# Import the LIVE constants so the backtest evaluates production behaviour.
from nfl_mcp.projections import matchup_multiplier, _MATCHUP_TIER_DEV, _usage_mult
from nfl_mcp.matchup_tools import _get_matchup_tier

from .data import load_season
from .metrics import evaluate, mae

logger = logging.getLogger(__name__)


def _defense_ranks(records: List[Dict], upto_week: int) -> Dict[str, Dict[str, int]]:
    """{position: {team: rank}} from weeks < upto_week. rank 1 = toughest defense."""
    bywk: Dict = defaultdict(float)
    weeks: Dict = defaultdict(set)
    for r in records:
        if r["week"] >= upto_week:
            continue
        key = (r["opponent"], r["position"])
        bywk[(r["opponent"], r["position"], r["week"])] += r["ppr"]
        weeks[key].add(r["week"])
    totals: Dict = defaultdict(float)
    for (opp, pos, _wk), v in bywk.items():
        totals[(opp, pos)] += v
    pos_teams: Dict = defaultdict(list)
    for (opp, pos), tot in totals.items():
        games = max(1, len(weeks[(opp, pos)]))
        pos_teams[pos].append((opp, tot / games))
    ranks: Dict[str, Dict[str, int]] = {}
    for pos, lst in pos_teams.items():
        lst.sort(key=lambda x: x[1])  # fewest allowed first -> rank 1 (toughest)
        ranks[pos] = {team: i + 1 for i, (team, _) in enumerate(lst)}
    return ranks


def _tier_for(rank: Optional[int]) -> str:
    return _get_matchup_tier(rank) if rank else "unknown"


def _touch_trend(prior: List[Dict]) -> str:
    """up/down/flat from recent (last 2) vs earlier touches."""
    if len(prior) < 3:
        return "flat"
    ordered = sorted(prior, key=lambda g: g["week"])
    recent = ordered[-2:]
    earlier = ordered[:-2]
    r = sum(g["touches"] for g in recent) / len(recent)
    e = sum(g["touches"] for g in earlier) / len(earlier) if earlier else r
    if e <= 0:
        return "flat"
    if r > e * 1.15:
        return "up"
    if r < e * 0.85:
        return "down"
    return "flat"


def build_samples(
    records: List[Dict], start_week: int, min_prior: int, min_trailing: float,
    positions: Optional[List[str]] = None,
) -> List[Dict]:
    """Build leak-free prediction samples with base / matchup / usage / full."""
    games_by_player: Dict[str, Dict[int, Dict]] = defaultdict(dict)
    for r in records:
        games_by_player[r["player_id"]][r["week"]] = r

    dcache: Dict[int, Dict[str, Dict[str, int]]] = {}
    samples: List[Dict] = []

    for r in records:
        wk = r["week"]
        if wk < start_week:
            continue
        if positions and r["position"] not in positions:
            continue
        prior = [g for w, g in games_by_player[r["player_id"]].items() if w < wk]
        if len(prior) < min_prior:
            continue
        trailing = sum(g["ppr"] for g in prior) / len(prior)
        if trailing < min_trailing:
            continue

        if wk not in dcache:
            dcache[wk] = _defense_ranks(records, wk)
        rank = dcache[wk].get(r["position"], {}).get(r["opponent"])
        tier = _tier_for(rank)
        m_mult = matchup_multiplier(r["position"], tier)  # position-aware (live)
        tier_dev = _MATCHUP_TIER_DEV.get(tier, 0.0)        # raw ±dev for sweeps
        u_mult = _usage_mult(None, _touch_trend(prior))

        samples.append({
            "position": r["position"],
            "actual": r["ppr"],
            "base": trailing,
            "matchup": trailing * m_mult,
            "usage": trailing * u_mult,
            "full": trailing * m_mult * u_mult,
            "matchup_mult": m_mult,
            "tier_dev": tier_dev,
        })
    return samples


def _series(samples: List[Dict], key: str):
    return [s[key] for s in samples], [s["actual"] for s in samples]


def run_backtest(
    seasons: List[int], start_week: int = 5, min_prior: int = 3, min_trailing: float = 5.0,
    positions: Optional[List[str]] = None,
) -> Dict:
    """Run the backtest and return structured results."""
    records: List[Dict] = []
    for s in seasons:
        records.extend(load_season(s))

    samples = build_samples(records, start_week, min_prior, min_trailing, positions)

    models = ["base", "matchup", "usage", "full"]
    results = {m: evaluate(*_series(samples, m)) for m in models}

    # Per-position breakdown for base vs full.
    per_pos: Dict[str, Dict] = {}
    for pos in ("QB", "RB", "WR", "TE"):
        sub = [s for s in samples if s["position"] == pos]
        if not sub:
            continue
        per_pos[pos] = {
            "n": len(sub),
            "base": evaluate(*_series(sub, "base")),
            "full": evaluate(*_series(sub, "full")),
        }

    # Tuning: sweep matchup strength scalar.
    tuning = []
    actuals = [smp["actual"] for smp in samples]
    for s in [0.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]:
        # Rebuild full with a scaled matchup multiplier; keep the usage factor.
        preds = []
        for smp in samples:
            usage_factor = (smp["usage"] / smp["base"]) if smp["base"] else 1.0
            scaled_m = 1 + s * smp["tier_dev"]
            preds.append(smp["base"] * scaled_m * usage_factor)
        tuning.append({"strength": s, "mae": round(mae(preds, actuals), 3)})

    # Per-position best matchup strength (drives position-specific multipliers).
    grid = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25]
    per_pos_tuning: Dict[str, Dict] = {}
    for pos in ("QB", "RB", "WR", "TE"):
        sub = [smp for smp in samples if smp["position"] == pos]
        if not sub:
            continue
        acts = [smp["actual"] for smp in sub]
        curve = []
        for s in grid:
            preds = []
            for smp in sub:
                scaled_m = 1 + s * smp["tier_dev"]
                preds.append(smp["base"] * scaled_m)  # matchup-only, isolate its effect
            curve.append({"strength": s, "mae": round(mae(preds, acts), 3)})
        best = min(curve, key=lambda c: c["mae"])
        per_pos_tuning[pos] = {"best_strength": best["strength"], "best_mae": best["mae"],
                               "base_mae": round(mae([smp["base"] for smp in sub], acts), 3),
                               "curve": curve}
    best = min(tuning, key=lambda t: t["mae"])

    return {
        "seasons": seasons,
        "start_week": start_week,
        "min_prior": min_prior,
        "min_trailing": min_trailing,
        "n_samples": len(samples),
        "models": results,
        "per_position": per_pos,
        "tuning": {"sweep": tuning, "best_strength": best["strength"], "best_mae": best["mae"]},
        "per_position_tuning": per_pos_tuning,
    }


def _fmt_row(name: str, m: Dict) -> str:
    return (f"  {name:<9} n={m['n']:<5} MAE={m['mae']:<7} RMSE={m['rmse']:<7} "
            f"Spearman={m['spearman']:<8} bias={m['bias']:<7} R2={m['r2']}")


def print_report(res: Dict) -> None:
    print("=" * 78)
    print(f"PROJECTION BACKTEST — seasons {res['seasons']}, weeks {res['start_week']}+, "
          f"trailing≥{res['min_trailing']} pts, n={res['n_samples']} player-weeks")
    print("=" * 78)
    print("\nModels (base = trailing PPG; then × live multipliers):")
    for name in ("base", "matchup", "usage", "full"):
        print(_fmt_row(name, res["models"][name]))

    b, f = res["models"]["base"]["mae"], res["models"]["full"]["mae"]
    delta = (b - f) / b * 100 if b else 0
    verdict = ("adjustments HELP" if f < b else "adjustments do NOT help — revisit")
    print(f"\n  => full vs base: MAE {b} -> {f} ({delta:+.1f}%)  [{verdict}]")

    print("\nPer position (base MAE -> full MAE):")
    for pos, d in res["per_position"].items():
        bm, fm = d["base"]["mae"], d["full"]["mae"]
        print(f"  {pos}: n={d['n']:<4} {bm} -> {fm} ({(bm-fm)/bm*100:+.1f}%)  "
              f"Spearman {d['base']['spearman']} -> {d['full']['spearman']}")

    print("\nMatchup-strength tuning (effective_mult = 1 + s·(mult−1)):")
    for t in res["tuning"]["sweep"]:
        mark = "  <= best" if t["strength"] == res["tuning"]["best_strength"] else ""
        cur = "  (current=1.0)" if t["strength"] == 1.0 else ""
        print(f"  s={t['strength']:<5} MAE={t['mae']}{cur}{mark}")
    bs = res["tuning"]["best_strength"]
    hint = ("magnitudes ~right" if abs(bs - 1.0) < 0.3 else
            "we OVER-adjust; soften multipliers" if bs < 1.0 else
            "we UNDER-adjust; strengthen multipliers")
    print(f"\n  => best matchup strength ≈ {bs}  [{hint}]")

    if res.get("per_position_tuning"):
        print("\nBest matchup strength PER POSITION (matchup-only MAE vs base):")
        for pos, d in res["per_position_tuning"].items():
            print(f"  {pos}: best s={d['best_strength']:<5} "
                  f"MAE {d['base_mae']} -> {d['best_mae']}")
    print("=" * 78)


def main():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser(description="Projection accuracy backtest")
    ap.add_argument("--seasons", default="2024", help="comma list, e.g. 2023,2024")
    ap.add_argument("--start-week", type=int, default=5)
    ap.add_argument("--min-prior", type=int, default=3)
    ap.add_argument("--min-trailing", type=float, default=5.0,
                    help="only score players averaging ≥ this (fantasy-relevant)")
    ap.add_argument("--positions", default=None, help="comma list QB,RB,WR,TE")
    args = ap.parse_args()

    seasons = [int(s) for s in args.seasons.split(",") if s.strip()]
    positions = [p.strip().upper() for p in args.positions.split(",")] if args.positions else None
    res = run_backtest(seasons, args.start_week, args.min_prior, args.min_trailing, positions)
    print_report(res)


if __name__ == "__main__":
    main()
