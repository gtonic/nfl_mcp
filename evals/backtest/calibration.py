"""
Uncertainty calibration backtest (Eval Layer A).

QUESTION
    The projection engine reports a floor and a ceiling, and
    ``get_win_probability_lineup`` turns them into "you have a 72% chance to
    win". Nothing has ever checked whether those numbers mean what they say.
    Two things are measured here:

      1. BAND COVERAGE — floor/ceiling are ``mean ± volatility·mean``, i.e. a
         symmetric ±1σ band under the Normal the optimizer assumes, which should
         contain the real outcome ~68.3% of the time with ~15.9% in each tail.
      2. WIN-PROBABILITY CALIBRATION — of the matchups called 70%, do about 70%
         actually get won? Reported as a Brier score plus a reliability table.

    Both matter for different reasons. Coverage that is too low means the stated
    floor is not a floor. Miscalibrated win probability means the lineup
    optimizer is solving for the wrong objective: it tilts toward ceiling when
    it thinks you are an underdog, so believing the wrong probability picks the
    wrong lineup.

METHOD (leak-free, walk-forward)
    Projections come from the live engine's opportunity baseline using only
    weeks before the one being predicted; sd comes from the live
    ``_VOLATILITY`` table via ``win_probability.player_sd`` — so this evaluates
    production's constants, not a copy of them.

    Ground truth is nflverse. Weekly fantasy scoring is right-skewed (a spike
    near zero when a player leaves early, a long right tail on touchdowns), so
    the two tails are reported separately rather than as one coverage number.

    Win probability has no historical league matchups to test against, so
    matchups are synthesised: within a week, players are shuffled with a fixed
    seed into two lineups of equal size, P(win) is computed from the
    projections, and the actual points decide the winner. Both sides face the
    same week, so nothing systematic separates them.

TUNING
    Sweeps a scalar on sd (effective sd = s·sd) and reports the s that brings
    coverage closest to 68.3% and the s that minimises Brier.

RUN
    python -m evals.backtest.calibration --seasons 2023,2024 --start-week 5
    python -m evals.backtest.calibration --seasons 2024 --ppr 0.5
"""

from __future__ import annotations

import argparse
import logging
import random
from collections import defaultdict

from nfl_mcp.opportunity import project_opportunity
from nfl_mcp.projections import _VOLATILITY
from nfl_mcp.scoring import ScoringModel
from nfl_mcp.win_probability import player_sd, win_prob

from .data import load_season
from .metrics import brier, reliability

logger = logging.getLogger(__name__)

# What a symmetric ±1σ Normal band claims, which is what floor/ceiling are.
NOMINAL_COVERAGE = 0.6827
NOMINAL_TAIL = (1 - NOMINAL_COVERAGE) / 2

DEFAULT_LINEUP_SIZE = 9


def actual_points(record: dict, ppr: float) -> float:
    """The week's real points in the requested scoring.

    Priced from the full stat line with the same `ScoringModel` the
    projection uses (Sleeper's defaults at `ppr`), not from nflverse's
    `fantasy_points_ppr`, whose INT -2 is not the scoring being predicted.
    """
    return ScoringModel.preset(ppr).game_points(record, record.get("position"))


def build_samples(
    records: list[dict], start_week: int, min_prior: int, min_trailing: float,
    ppr: float = 1.0, positions: list[str] | None = None,
) -> list[dict]:
    """Leak-free (projection, floor, ceiling, sd, actual) per player-week."""
    games_by_player: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in records:
        games_by_player[r["player_id"]][r["week"]] = r

    samples: list[dict] = []
    for r in records:
        week = r["week"]
        if week < start_week:
            continue
        if positions and r["position"] not in positions:
            continue
        prior = [g for w, g in games_by_player[r["player_id"]].items() if w < week]
        if len(prior) < min_prior:
            continue
        trailing = sum(actual_points(g, ppr) for g in prior) / len(prior)
        if trailing < min_trailing:
            continue

        mean = project_opportunity(prior, r["position"], ppr=ppr)
        if mean is None:
            continue

        # Exactly how the live engine derives the band and the optimizer's sd.
        volatility = _VOLATILITY.get(r["position"], 0.35)
        floor = round(mean * (1 - volatility), 1)
        ceiling = round(mean * (1 + volatility), 1)
        sd = player_sd({"projected_points": mean, "floor": floor,
                        "ceiling": ceiling, "position": r["position"]})

        samples.append({
            "player_id": r["player_id"],
            "position": r["position"],
            "season": r["season"],
            "week": week,
            "mean": mean,
            "floor": floor,
            "ceiling": ceiling,
            "sd": sd,
            "actual": actual_points(r, ppr),
        })
    return samples


def coverage(samples: list[dict], sd_scale: float = 1.0) -> dict:
    """How often reality lands inside the band, and which side it misses on."""
    if not samples:
        return {"n": 0}
    below = above = inside = 0
    z_values = []
    for s in samples:
        half_width = s["sd"] * sd_scale
        low, high = s["mean"] - half_width, s["mean"] + half_width
        if s["actual"] < low:
            below += 1
        elif s["actual"] > high:
            above += 1
        else:
            inside += 1
        if s["sd"] > 0:
            z_values.append((s["actual"] - s["mean"]) / s["sd"])
    n = len(samples)
    mean_z = sum(z_values) / len(z_values) if z_values else 0.0
    var_z = (sum((z - mean_z) ** 2 for z in z_values) / len(z_values)) if z_values else 0.0
    return {
        "n": n,
        "sd_scale": sd_scale,
        "inside": round(inside / n, 4),
        "below_floor": round(below / n, 4),
        "above_ceiling": round(above / n, 4),
        # z has mean 0 and sd 1 if the stated uncertainty is honest; sd(z) > 1
        # means the band is too narrow for the spread reality actually shows.
        "z_mean": round(mean_z, 4),
        "z_sd": round(var_z ** 0.5, 4),
    }


def build_matchups(
    samples: list[dict], lineup_size: int = DEFAULT_LINEUP_SIZE, seed: int = 20260918,
    repeats: int = 5,
) -> list[dict]:
    """Synthetic head-to-heads within a week: (predicted P(win), did win).

    Each week is re-partitioned ``repeats`` times. The draws overlap, so this
    buys precision on the calibration curve rather than genuinely independent
    samples — enough to tell a 0.20 Brier from a 0.25, not enough to read a
    third decimal.
    """
    by_week: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for s in samples:
        by_week[(s["season"], s["week"])].append(s)

    rng = random.Random(seed)
    matchups: list[dict] = []
    for _key, pool in sorted(by_week.items()):
        if len(pool) < lineup_size * 2:
            continue
        for _ in range(max(1, repeats)):
            shuffled = pool[:]
            rng.shuffle(shuffled)
            for i in range(0, len(shuffled) - 2 * lineup_size + 1, 2 * lineup_size):
                mine = shuffled[i:i + lineup_size]
                theirs = shuffled[i + lineup_size:i + 2 * lineup_size]
                my_mean = sum(p["mean"] for p in mine)
                my_var = sum(p["sd"] ** 2 for p in mine)
                their_mean = sum(p["mean"] for p in theirs)
                their_var = sum(p["sd"] ** 2 for p in theirs)
                my_actual = sum(p["actual"] for p in mine)
                their_actual = sum(p["actual"] for p in theirs)
                if my_actual == their_actual:
                    continue  # a tie decides nothing either way
                matchups.append({
                    "p_win": win_prob(my_mean, my_var, their_mean, their_var),
                    "won": 1.0 if my_actual > their_actual else 0.0,
                    "margin": round(my_actual - their_actual, 2),
                    "predicted_margin": round(my_mean - their_mean, 2),
                    "my_var": my_var,
                    "their_var": their_var,
                    "my_mean": my_mean,
                    "their_mean": their_mean,
                })
    return matchups


def _rescaled_win_probs(matchups: list[dict], sd_scale: float) -> list[float]:
    scale_sq = sd_scale ** 2
    return [
        win_prob(m["my_mean"], m["my_var"] * scale_sq,
                 m["their_mean"], m["their_var"] * scale_sq)
        for m in matchups
    ]


def run_calibration(
    seasons: list[int], start_week: int = 5, min_prior: int = 3,
    min_trailing: float = 5.0, ppr: float = 1.0,
    positions: list[str] | None = None, lineup_size: int = DEFAULT_LINEUP_SIZE,
) -> dict:
    records: list[dict] = []
    for season in seasons:
        records.extend(load_season(season))

    samples = build_samples(records, start_week, min_prior, min_trailing, ppr, positions)
    matchups = build_matchups(samples, lineup_size)
    outcomes = [m["won"] for m in matchups]
    probabilities = [m["p_win"] for m in matchups]

    scales = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5]
    band_sweep = [coverage(samples, s) for s in scales]
    best_band = min(
        band_sweep, key=lambda c: abs(c.get("inside", 0) - NOMINAL_COVERAGE)
    ) if samples else {}

    brier_sweep = []
    for scale in scales:
        rescaled = _rescaled_win_probs(matchups, scale)
        brier_sweep.append({"sd_scale": scale, "brier": round(brier(rescaled, outcomes), 4)})
    best_brier = min(brier_sweep, key=lambda b: b["brier"]) if brier_sweep else {}

    base_rate = sum(outcomes) / len(outcomes) if outcomes else 0.0

    per_position = {}
    for position in sorted({s["position"] for s in samples}):
        subset = [s for s in samples if s["position"] == position]
        stats = coverage(subset)
        # The scale this position's band would need. Volatility is per-position
        # in the live engine, so the correction should be too.
        fine = [round(0.5 + i * 0.05, 2) for i in range(61)]
        best = min(fine, key=lambda s: abs(coverage(subset, s)["inside"] - NOMINAL_COVERAGE))
        stats["best_scale"] = best
        stats["suggested_volatility"] = round(_VOLATILITY.get(position, 0.35) * best, 3)
        per_position[position] = stats

    return {
        "seasons": seasons,
        "ppr": ppr,
        "start_week": start_week,
        "n_samples": len(samples),
        "coverage": coverage(samples),
        "per_position": per_position,
        "band_sweep": band_sweep,
        "best_band_scale": best_band.get("sd_scale"),
        "n_matchups": len(matchups),
        "brier": round(brier(probabilities, outcomes), 4) if matchups else None,
        # What you would score by always predicting the base rate. Beating this
        # is the bar for the probability carrying any information at all.
        "brier_baseline": round(base_rate * (1 - base_rate), 4),
        "reliability": reliability(probabilities, outcomes),
        "brier_sweep": brier_sweep,
        "best_brier_scale": best_brier.get("sd_scale"),
    }


def print_report(res: dict) -> None:
    print("=" * 78)
    print(f"UNCERTAINTY CALIBRATION — seasons {res['seasons']}, weeks "
          f"{res['start_week']}+, {res['ppr']} pts/reception, "
          f"n={res['n_samples']} player-weeks")
    print("=" * 78)

    c = res["coverage"]
    print(f"\nFloor/ceiling band (claims {NOMINAL_COVERAGE:.1%} inside, "
          f"{NOMINAL_TAIL:.1%} per tail):")
    print(f"  inside        {c['inside']:.1%}   (want {NOMINAL_COVERAGE:.1%})")
    print(f"  below floor   {c['below_floor']:.1%}   (want {NOMINAL_TAIL:.1%})")
    print(f"  above ceiling {c['above_ceiling']:.1%}   (want {NOMINAL_TAIL:.1%})")
    print(f"  z: mean={c['z_mean']} sd={c['z_sd']}   "
          f"(honest uncertainty => mean 0, sd 1)")
    verdict = ("band is about right" if abs(c["inside"] - NOMINAL_COVERAGE) < 0.05
               else "band is TOO NARROW" if c["inside"] < NOMINAL_COVERAGE
               else "band is TOO WIDE")
    skew = ("symmetric" if abs(c["below_floor"] - c["above_ceiling"]) < 0.03
            else "misses LOW more often — the floor is not a floor"
            if c["below_floor"] > c["above_ceiling"]
            else "misses HIGH more often — the ceiling is too low")
    print(f"  => {verdict}; tails are {skew}")

    print("\nPer position (inside / below / above), and the volatility that "
          "would hit 68.3%:")
    for position, stats in res["per_position"].items():
        print(f"  {position}: n={stats['n']:<5} {stats['inside']:.1%} / "
              f"{stats['below_floor']:.1%} / {stats['above_ceiling']:.1%}   "
              f"z_sd={stats['z_sd']:<7} "
              f"_VOLATILITY {_VOLATILITY.get(position, 0.35)} -> "
              f"{stats['suggested_volatility']}")

    print("\nBand width sweep (effective sd = s · sd):")
    for entry in res["band_sweep"]:
        mark = "  <= closest" if entry["sd_scale"] == res["best_band_scale"] else ""
        current = "  (current=1.0)" if entry["sd_scale"] == 1.0 else ""
        print(f"  s={entry['sd_scale']:<5} inside={entry['inside']:.1%}{current}{mark}")

    print(f"\nWin probability — {res['n_matchups']} synthetic same-week matchups:")
    if res["brier"] is None:
        print("  (not enough samples per week to build matchups)")
    else:
        print(f"  Brier      {res['brier']}   (base-rate baseline "
              f"{res['brier_baseline']}, lower is better)")
        informative = res["brier"] < res["brier_baseline"]
        print(f"  => the probability {'carries' if informative else 'does NOT carry'} "
              "information beyond the base rate")
        print("\n  Reliability (predicted -> observed win rate):")
        for row in res["reliability"]:
            gap = row["observed"] - row["predicted"]
            flag = "" if abs(gap) < 0.05 else ("  over-confident" if gap < 0 else "  under-confident")
            print(f"    {row['bin_low']:.1f}-{row['bin_high']:.1f}  n={row['n']:<5} "
                  f"pred={row['predicted']:.3f} obs={row['observed']:.3f}{flag}")
        print("\n  Brier by sd scale:")
        for entry in res["brier_sweep"]:
            mark = "  <= best" if entry["sd_scale"] == res["best_brier_scale"] else ""
            current = "  (current=1.0)" if entry["sd_scale"] == 1.0 else ""
            print(f"    s={entry['sd_scale']:<5} Brier={entry['brier']}{current}{mark}")
    print("=" * 78)


def main():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser(description="Uncertainty / win-probability calibration")
    ap.add_argument("--seasons", default="2024", help="comma list, e.g. 2023,2024")
    ap.add_argument("--start-week", type=int, default=5)
    ap.add_argument("--min-prior", type=int, default=3)
    ap.add_argument("--min-trailing", type=float, default=5.0)
    ap.add_argument("--ppr", type=float, default=1.0,
                    help="points per reception (1.0 full, 0.5 half, 0.0 standard)")
    ap.add_argument("--positions", default=None, help="comma list QB,RB,WR,TE")
    ap.add_argument("--lineup-size", type=int, default=DEFAULT_LINEUP_SIZE)
    args = ap.parse_args()

    seasons = [int(s) for s in args.seasons.split(",") if s.strip()]
    positions = [p.strip().upper() for p in args.positions.split(",")] if args.positions else None
    res = run_calibration(
        seasons, args.start_week, args.min_prior, args.min_trailing,
        args.ppr, positions, args.lineup_size,
    )
    print_report(res)


if __name__ == "__main__":
    main()
