"""
Sleeper-first blend backtest: how much of the weekly projection should be ours?

QUESTION
    The live weekly projection is ``BLEND_MODEL_WEIGHT × ours + (1 − w) ×
    Sleeper`` (``nfl_mcp.sleeper_projections``). Is 0.25 the right share, does
    the blend beat either input, and are the floor/ceiling widths
    (``projections._VOLATILITY``) still ~68% around it?

METHOD (leak-free, walk-forward)
    For every QB/RB/WR/TE player-week (weeks 3+, >= 2 prior games, trailing
    >= 5 points) the model is production's own pieces on games before the
    week: ``opportunity.project_opportunity`` regressed toward the rank bucket
    (``ros.regressed_rate``; rank = previous-season points-per-game rank, as
    in ``ros_backtest``) × the continuous matchup factor × the Vegas
    environment multiplier (closing lines from nflverse games.csv).
    Sleeper = that week's Sleeper projection (the pre-game stat line it
    publishes; the history endpoint returns the last one) priced with the same
    ``ScoringModel`` through production's ``sleeper_projections.points_for``,
    matched by name + team — including the explicit zero of a player Sleeper
    lists without points. Truth = the week's stat line priced with the same
    scoring (``data.TRUTH_SCORING``).

    ``--include-dnp`` adds the weeks a relevant player's team played without
    him (truth 0): the population a start/sit decision actually faces.

    Reported: MAE / bias / Spearman per position for model-raw (no
    regression), model, Sleeper and the live blend, a sweep of the model
    weight, band coverage at the live volatility (and the width that would
    hit 68.3%), and start/sit pairwise accuracy by projected gap.

DATA
    Sleeper projection history is fetched once per week from
    ``api.sleeper.app/projections/nfl/<season>/<week>`` and cached (compacted)
    under ``evals/backtest/.cache/sleeper/`` — ~0.6 MB a week, not committed.
    This is a NETWORK eval on first run (48 requests for 2023-25 weeks 2-17);
    offline afterwards.

RUN
    python -m evals.backtest.sleeper_blend --seasons 2023 2024 2025
    python -m evals.backtest.sleeper_blend --seasons 2025 --include-dnp
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import defaultdict

import httpx

from nfl_mcp import sleeper_projections as sp
from nfl_mcp.matchup_tools import attach_prior_season, compute_defense_rankings, matchup_ratio
from nfl_mcp.opportunity import project_opportunity
from nfl_mcp.projections import (
    _VOLATILITY,
    _environment_mult,
    _ranking_entry,
    base_ppg,
    matchup_factor,
)
from nfl_mcp.ros import regressed_rate

from .backtest import _weekly_allowed
from .data import _CACHE_DIR, TRUTH_SCORING, load_games, load_season
from .metrics import bias, mae, spearman
from .ros_backtest import _prior_ranks

logger = logging.getLogger(__name__)

_POSITIONS = ("QB", "RB", "WR", "TE")
_SLEEPER_DIR = os.path.join(_CACHE_DIR, "sleeper")
_KEEP_PLAYER = ("first_name", "last_name", "position", "team")


def compact(rows: list) -> list[dict]:
    """The fields ``sleeper_projections._index`` reads, numeric stats only."""
    return [{"player_id": r.get("player_id"), "team": r.get("team"),
             "opponent": r.get("opponent"),
             "player": {k: (r.get("player") or {}).get(k) for k in _KEEP_PLAYER},
             "stats": {k: v for k, v in (r.get("stats") or {}).items()
                       if isinstance(v, int | float) and not k.startswith(("adp", "pos_adp"))}}
            for r in rows or [] if isinstance(r, dict)]


def load_sleeper_week(season: int, week: int, use_cache: bool = True) -> dict:
    """Sleeper's projections for one past week, as production indexes them."""
    os.makedirs(_SLEEPER_DIR, exist_ok=True)
    path = os.path.join(_SLEEPER_DIR, f"{season}_{week}.json")
    rows = None
    if use_cache and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    if rows is None:
        params = [("season_type", "regular")] + [("position[]", p) for p in sp.PROJECTED_POSITIONS]
        url = sp.PROJECTIONS_URL.format(season=season, week=week)
        logger.info("Downloading %s", url)
        resp = httpx.get(url, params=params, timeout=60)
        resp.raise_for_status()
        rows = compact(resp.json())
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, separators=(",", ":"))
    return sp._index(rows)


def build_samples(seasons: list[int], start_week: int = 3, min_prior: int = 2,
                  min_trailing: float = 5.0, include_dnp: bool = False) -> list[dict]:
    samples: list[dict] = []
    for season in seasons:
        records = load_season(season)
        prev = load_season(season - 1)
        ranks = _prior_ranks(prev)
        games = load_games(season)
        prior_final = compute_defense_rankings(_weekly_allowed(prev), season - 1)
        by_player: dict[str, list[dict]] = defaultdict(list)
        teams_played: dict[int, set] = defaultdict(set)
        for r in records:
            by_player[r["player_id"]].append(r)
            teams_played[r["week"]].add(r["team"])
        rankings: dict[int, dict] = {}
        sleeper: dict[int, dict] = {}
        for pid, gs in by_player.items():
            gs.sort(key=lambda g: g["week"])
            pos = gs[0]["position"]
            by_week = {g["week"]: g for g in gs}
            for week in range(start_week, 18):
                prior = [g for g in gs if g["week"] < week]
                if len(prior) < min_prior:
                    continue
                game = by_week.get(week)
                team = game["team"] if game else prior[-1]["team"]
                if game is None and (not include_dnp or team not in teams_played[week]):
                    continue
                trailing = sum(g["ppr"] for g in prior) / len(prior)
                if trailing < min_trailing:
                    continue
                opp = project_opportunity(prior, pos)
                if opp is None:
                    continue
                if week not in rankings:
                    rankings[week] = attach_prior_season(
                        compute_defense_rankings(_weekly_allowed(records, week), season) or {},
                        prior_final)
                    sleeper[week] = load_sleeper_week(season, week)
                opponent = game["opponent"] if game else None
                ratio = matchup_ratio(_ranking_entry(rankings[week], pos, opponent or ""))
                mf = matchup_factor(pos, ratio) if ratio is not None else 1.0
                implied = (games.get((season, week, team)) or {}).get("implied")
                env = _environment_mult(implied, implied is None)
                n = min(len(prior), 6)
                model = regressed_rate(opp, base_ppg(pos, ranks.get(pid)), n) * mf * env
                theirs, status = sp.points_for(sleeper[week], TRUTH_SCORING,
                                               name=gs[0]["player"], team=team)
                samples.append({
                    "season": season, "week": week, "position": pos, "played": game is not None,
                    "model_raw": opp * mf * env, "model": model, "sleeper": theirs,
                    "sleeper_status": status, "actual": game["ppr"] if game else 0.0,
                })
    return samples


def _blend(s: dict, w: float) -> float:
    """Production's rule (``sleeper_projections.blend``) at model weight `w`."""
    if s["sleeper_status"] == "not_projected":
        return 0.0
    return w * s["model"] + (1 - w) * s["sleeper"]


def _row(label: str, pred: list[float], act: list[float]) -> str:
    return (f"{label} {mae(pred, act):5.2f} {bias(pred, act):+5.2f} "
            f"rho {spearman(pred, act):.3f}")


def _pairwise(rows: list[dict], predict) -> dict[str, float]:
    """P(the higher projection outscores the lower), same position and week."""
    groups: dict = defaultdict(list)
    for s in rows:
        groups[(s["season"], s["week"], s["position"])].append(s)
    edges = ((1, "<1"), (3, "1-3"), (5, "3-5"), (8, "5-8"), (1e9, "8+"))
    tally: dict = defaultdict(lambda: [0, 0])
    for g in groups.values():
        preds = [predict(s) for s in g]
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                gap = abs(preds[i] - preds[j])
                hi, lo = (g[i], g[j]) if preds[i] >= preds[j] else (g[j], g[i])
                label = next(lab for edge, lab in edges if gap < edge)
                tally[label][0] += hi["actual"] > lo["actual"]
                tally[label][1] += 1
    return {lab: tally[lab][0] / tally[lab][1] for _, lab in edges if tally[lab][1]}


def report(samples: list[dict]) -> None:
    w = sp.BLEND_MODEL_WEIGHT
    matched = [s for s in samples if s["sleeper"] is not None]
    print(f"n={len(samples)} player-weeks, {len(matched)} with a Sleeper projection "
          f"({sum(s['sleeper_status'] == 'not_projected' for s in samples)} listed without points)")
    print(f"\nMAE / bias / Spearman (Sleeper-matched rows), live model weight {w}")
    for pos in (*_POSITIONS, "ALL"):
        rows = [s for s in matched if pos in ("ALL", s["position"])]
        act = [s["actual"] for s in rows]
        cells = [_row("model_raw", [s["model_raw"] for s in rows], act),
                 _row("model", [s["model"] for s in rows], act),
                 _row("sleeper", [s["sleeper"] for s in rows], act),
                 _row("blend", [_blend(s, w) for s in rows], act)]
        print(f"  {pos:3s} n={len(rows):5d} | " + " | ".join(cells))
    print("\nModel-weight sweep (blend MAE):")
    for pos in (*_POSITIONS, "ALL"):
        rows = [s for s in matched if pos in ("ALL", s["position"])]
        act = [s["actual"] for s in rows]
        sweep = {x: mae([_blend(s, x) for s in rows], act) for x in (0, .1, .2, .25, .3, .4, .5, 1)}
        best = min(sweep, key=sweep.get)
        print(f"  {pos:3s} " + " ".join(f"{x:g}:{m:.3f}" for x, m in sweep.items())
              + f"  best {best:g}")
    print("\nBand coverage around the blend (target 68.3%):")
    for pos in _POSITIONS:
        rows = [s for s in matched if s["position"] == pos]
        def cover(v, rows=rows):
            return sum(_blend(s, w) * (1 - v) <= s["actual"] <= _blend(s, w) * (1 + v)
                       for s in rows) / len(rows)
        best = min((x / 100 for x in range(30, 121)), key=lambda v: abs(cover(v) - 0.683))
        print(f"  {pos}: live {_VOLATILITY[pos]} -> {cover(_VOLATILITY[pos]):.1%}; "
              f"68.3% at {best:.2f}")
    print("\nStart/sit: P(higher projected outscores) by projected gap:")
    for label, fn in (("model", lambda s: s["model"]), ("sleeper", lambda s: s["sleeper"]),
                      ("blend", lambda s: _blend(s, w))):
        cells = _pairwise(matched, fn)
        print(f"  {label:8s} " + " | ".join(f"{k}: {v:.1%}" for k, v in cells.items()))
    fallback = [s for s in samples if s["sleeper"] is None]
    if fallback:
        act = [s["actual"] for s in fallback]
        print(f"\nNo Sleeper projection (model_only fallback) n={len(fallback)}: "
              + _row("model_raw", [s["model_raw"] for s in fallback], act) + " | "
              + _row("model", [s["model"] for s in fallback], act))


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", type=int, nargs="+", default=[2023, 2024, 2025])
    ap.add_argument("--start-week", type=int, default=3)
    ap.add_argument("--include-dnp", action="store_true",
                    help="also score weeks a relevant player's team played without him (truth 0)")
    args = ap.parse_args()
    report(build_samples(args.seasons, args.start_week, include_dnp=args.include_dnp))


if __name__ == "__main__":
    main()
