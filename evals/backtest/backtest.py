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

from nfl_mcp.matchup_tools import (
    _get_matchup_tier,
    attach_prior_season,
    compute_defense_rankings,
    matchup_ratio,
)
from nfl_mcp.opportunity import project_opportunity

# Import the LIVE constants/functions so the backtest evaluates production behaviour.
from nfl_mcp.projections import (
    _MATCHUP_TIER_DEV,
    _ranking_entry,
    _usage_mult,
    matchup_factor,
    matchup_multiplier,
)
from nfl_mcp.weather_tools import weather_multiplier

from .data import load_games, load_season
from .metrics import evaluate, mae

_DOME_ROOFS = {"dome", "closed"}

logger = logging.getLogger(__name__)


def _defense_ranks(records: list[dict], upto_week: int) -> dict[str, dict[str, int]]:
    """{position: {team: rank}} from weeks < upto_week. rank 1 = toughest defense."""
    bywk: dict = defaultdict(float)
    weeks: dict = defaultdict(set)
    for r in records:
        if r["week"] >= upto_week:
            continue
        key = (r["opponent"], r["position"])
        bywk[(r["opponent"], r["position"], r["week"])] += r["ppr"]
        weeks[key].add(r["week"])
    totals: dict = defaultdict(float)
    for (opp, pos, _wk), v in bywk.items():
        totals[(opp, pos)] += v
    pos_teams: dict = defaultdict(list)
    for (opp, pos), tot in totals.items():
        games = max(1, len(weeks[(opp, pos)]))
        pos_teams[pos].append((opp, tot / games))
    ranks: dict[str, dict[str, int]] = {}
    for pos, lst in pos_teams.items():
        lst.sort(key=lambda x: x[1])  # fewest allowed first -> rank 1 (toughest)
        ranks[pos] = {team: i + 1 for i, (team, _) in enumerate(lst)}
    return ranks


def _weekly_allowed(records: list[dict], upto_week: int = 99) -> dict:
    """``{(opponent, position, week): [ppr, receptions]}`` from weeks < upto_week,
    the input production's `compute_defense_rankings` takes."""
    out: dict = {}
    for r in records:
        if r["week"] >= upto_week:
            continue
        cell = out.setdefault((r["opponent"], r["position"], r["week"]), [0.0, 0.0])
        cell[0] += r["ppr"]
        cell[1] += r.get("receptions", 0.0)
    return out


def _tier_for(rank: int | None) -> str:
    return _get_matchup_tier(rank) if rank else "unknown"


def _touch_trend(prior: list[dict]) -> str:
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
    records: list[dict], start_week: int, min_prior: int, min_trailing: float,
    positions: list[str] | None = None, games: dict | None = None,
    prior_records: dict[int, list[dict]] | None = None,
) -> list[dict]:
    """Build leak-free prediction samples with base / matchup / usage / full / weather."""
    games = games or {}
    # Last season's final rankings per season: the matchup factor's prior.
    prior_final = {
        season: compute_defense_rankings(_weekly_allowed(rs), season)
        for season, rs in (prior_records or {}).items()
    }
    # Keyed by season too: with several seasons in `records`, a (player, week)
    # key let 2024's weeks overwrite 2023's, so 2023 samples were "predicted"
    # from the next season's games.
    games_by_player: dict[tuple[int, str], dict[int, dict]] = defaultdict(dict)
    for r in records:
        games_by_player[(r["season"], r["player_id"])][r["week"]] = r

    dcache: dict[tuple[int, int], dict[str, dict[str, int]]] = {}
    rcache: dict[tuple[int, int], dict] = {}
    by_season: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        by_season[r["season"]].append(r)
    samples: list[dict] = []

    for r in records:
        wk = r["week"]
        if wk < start_week:
            continue
        if positions and r["position"] not in positions:
            continue
        prior = [g for w, g in games_by_player[(r["season"], r["player_id"])].items() if w < wk]
        if len(prior) < min_prior:
            continue
        trailing = sum(g["ppr"] for g in prior) / len(prior)
        if trailing < min_trailing:
            continue

        key = (r["season"], wk)
        if key not in dcache:
            dcache[key] = _defense_ranks(by_season[r["season"]], wk)
        rank = dcache[key].get(r["position"], {}).get(r["opponent"])
        tier = _tier_for(rank)
        m_mult = matchup_multiplier(r["position"], tier)  # position-aware (live)
        tier_dev = _MATCHUP_TIER_DEV.get(tier, 0.0)        # raw ±dev for sweeps
        u_mult = _usage_mult(None, _touch_trend(prior))

        # Opportunity-based baseline (volume × shrunk efficiency), leak-free.
        opp = project_opportunity(prior, r["position"])
        if opp is None:
            opp = trailing

        # Production's continuous matchup factor on top of it: rankings from
        # weeks < wk, shrunk toward part of last season's final ratio.
        key = (r["season"], wk)
        if key not in rcache:
            rcache[key] = attach_prior_season(
                compute_defense_rankings(
                    _weekly_allowed(by_season[r["season"]], wk), r["season"]) or {},
                prior_final.get(r["season"] - 1),
            )
        ratio = matchup_ratio(_ranking_entry(rcache[key], r["position"], r["opponent"]))
        opp_matchup = opp * (matchup_factor(r["position"], ratio) if ratio is not None else 1.0)

        # Weather: this week's recorded wind/roof for the player's game.
        g = games.get((r["season"], wk, r["team"])) or {}
        wind = g.get("wind")
        is_dome = (g.get("roof") or "") in _DOME_ROOFS
        w_mult = weather_multiplier(r["position"], wind or 0.0, is_dome=is_dome)

        samples.append({
            "position": r["position"],
            "actual": r["ppr"],
            "base": trailing,
            "opportunity": opp,
            "opportunity_matchup": opp_matchup,
            "matchup": trailing * m_mult,
            "usage": trailing * u_mult,
            "full": trailing * m_mult * u_mult,
            "weather": trailing * w_mult,
            "matchup_mult": m_mult,
            "tier_dev": tier_dev,
            "wind": wind,
            "is_dome": is_dome,
        })
    return samples


def _series(samples: list[dict], key: str):
    return [s[key] for s in samples], [s["actual"] for s in samples]


def run_backtest(
    seasons: list[int], start_week: int = 5, min_prior: int = 3, min_trailing: float = 5.0,
    positions: list[str] | None = None,
) -> dict:
    """Run the backtest and return structured results."""
    records: list[dict] = []
    games: dict = {}
    prior_records: dict[int, list[dict]] = {}
    for s in seasons:
        records.extend(load_season(s))
        try:
            prior_records[s - 1] = load_season(s - 1)
        except Exception as e:
            logger.warning("No prior season %s for the matchup prior: %s", s - 1, e)
        try:
            games.update(load_games(s))
        except Exception as e:
            logger.warning("Could not load games/weather for %s: %s", s, e)

    samples = build_samples(records, start_week, min_prior, min_trailing, positions, games,
                            prior_records)

    models = ["base", "opportunity", "opportunity_matchup", "matchup", "usage", "full", "weather"]
    results = {m: evaluate(*_series(samples, m)) for m in models}

    # Weather only bites in windy, outdoor games — measure it where it applies:
    # passing positions (QB/WR/TE) in games with wind >= 15 mph.
    windy = [s for s in samples
             if s["position"] in ("QB", "WR", "TE")
             and not s["is_dome"] and (s["wind"] or 0) >= 15]
    weather_effect = {"n_windy_passing": len(windy),
                      "n_with_wind_data": sum(1 for s in samples if s["wind"] is not None)}
    if windy:
        weather_effect["base"] = evaluate(*_series(windy, "base"))
        weather_effect["weather"] = evaluate(*_series(windy, "weather"))

    # Per-position breakdown: base (trailing PPG) vs opportunity vs full.
    per_pos: dict[str, dict] = {}
    for pos in ("QB", "RB", "WR", "TE"):
        sub = [s for s in samples if s["position"] == pos]
        if not sub:
            continue
        per_pos[pos] = {
            "n": len(sub),
            "base": evaluate(*_series(sub, "base")),
            "opportunity": evaluate(*_series(sub, "opportunity")),
            "opportunity_matchup": evaluate(*_series(sub, "opportunity_matchup")),
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
    per_pos_tuning: dict[str, dict] = {}
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
        "weather_effect": weather_effect,
    }


def _fmt_row(name: str, m: dict) -> str:
    return (f"  {name:<9} n={m['n']:<5} MAE={m['mae']:<7} RMSE={m['rmse']:<7} "
            f"Spearman={m['spearman']:<8} bias={m['bias']:<7} R2={m['r2']}")


def print_report(res: dict) -> None:
    print("=" * 78)
    print(f"PROJECTION BACKTEST — seasons {res['seasons']}, weeks {res['start_week']}+, "
          f"trailing≥{res['min_trailing']} pts, n={res['n_samples']} player-weeks")
    print("=" * 78)
    print("\nModels (base = trailing PPG; opportunity = volume×shrunk-efficiency):")
    for name in ("base", "opportunity", "opportunity_matchup", "matchup", "usage", "full",
                 "weather"):
        print(_fmt_row(name[:9] if name != "opportunity_matchup" else "opp+mtch", res["models"][name]))

    b, f = res["models"]["base"]["mae"], res["models"]["full"]["mae"]
    delta = (b - f) / b * 100 if b else 0
    verdict = ("adjustments HELP" if f < b else "adjustments do NOT help — revisit")
    print(f"\n  => full vs base: MAE {b} -> {f} ({delta:+.1f}%)  [{verdict}]")

    o = res["models"]["opportunity"]
    bs, os_ = res["models"]["base"]["spearman"], o["spearman"]
    od = (b - o["mae"]) / b * 100 if b else 0
    ov = ("opportunity HELPS" if o["mae"] < b else "opportunity does NOT help — revisit")
    print(f"  => opportunity vs base: MAE {b} -> {o['mae']} ({od:+.1f}%), "
          f"Spearman {bs} -> {os_}  [{ov}]")

    om = res["models"]["opportunity_matchup"]
    omv = "matchup factor HELPS" if om["mae"] < o["mae"] else "matchup factor does NOT help — revisit"
    print(f"  => opportunity + matchup factor: MAE {o['mae']} -> {om['mae']}, "
          f"Spearman {os_} -> {om['spearman']}  [{omv}]")

    print("\nPer position (base -> opportunity, MAE & Spearman):")
    for pos, d in res["per_position"].items():
        bm, om = d["base"]["mae"], d["opportunity"]["mae"]
        print(f"  {pos}: n={d['n']:<4} MAE {bm} -> {om} ({(bm-om)/bm*100:+.1f}%)  "
              f"Spearman {d['base']['spearman']} -> {d['opportunity']['spearman']}  "
              f"bias {d['base']['bias']:+.2f} -> {d['opportunity']['bias']:+.2f}")

    we = res.get("weather_effect", {})
    print("\nWeather (wind) effect on passing (QB/WR/TE) in windy outdoor games "
          f"[wind>=15 mph, n={we.get('n_windy_passing', 0)} of "
          f"{we.get('n_with_wind_data', 0)} with wind data]:")
    if we.get("n_windy_passing") and "base" in we:
        b_, w_ = we["base"], we["weather"]
        verdict = ("weather HELPS here" if w_["mae"] < b_["mae"] else "weather does NOT help — keep it out of projections")
        print(f"  base    MAE={b_['mae']} Spearman={b_['spearman']}")
        print(f"  weather MAE={w_['mae']} Spearman={w_['spearman']}   [{verdict}]")
    else:
        print("  (no windy-passing samples in range)")

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
