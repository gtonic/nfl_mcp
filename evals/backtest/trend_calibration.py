"""
Calibration sweeps for the trend heuristics (returning teammates, role shifts).

QUESTION
    The trend constants of #246-#253 were set from one or two backtest points
    each. Which values does the walk-forward backtest actually prefer?

    - ``projections.RETURNING_KEEP_WEIGHT``: once a teammate whose absence
      inflated a player's trailing volume is back, the share of the full
      trailing rate kept (the rest is the rate from their games together).
    - ``role_shift.DOWN_STRENGTH`` / ``MIN_MULTIPLIER`` / ``ONE_WEEK_WEIGHT``:
      the multiplier on Sleeper's share for a lost role.
    - ``opportunity.POST_BREAK_WEIGHT``: how much more the games since a role
      change weigh in the model's volume.
    - ``role_shift.UP_STRENGTH`` / ``MAX_MULTIPLIER`` and the gate on a gained
      role: priced only when it held two games (``recent_weeks == 2``), at RB
      / WR / TE, and was not explained by a teammate's absence (a same-room
      teammate ranked ahead, or sharing his volume, missed one of the gain
      weeks after playing earlier -- live: ``returning_teammates`` /
      ``starters_out_ahead``).

METHOD
    The rows of ``sleeper_blend.build_samples`` (walk-forward, leak-free;
    2023-25 weeks 3+, Sleeper-matched), re-priced at each candidate value with
    production's blend rule (``BLEND_MODEL_WEIGHT`` x model + the rest x
    Sleeper). Per sweep the blend MAE on the affected rows *and* on every row
    is reported (a gain on a few rows that costs the rest is no gain), plus
    the model's MAE against the next four games (``actual_next4``) for the
    returning-teammate weight: the rest-of-season rate a trade signal prices.

RUN
    python -m evals.backtest.trend_calibration --seasons 2023 2024 2025
    python -m evals.backtest.trend_calibration --only returning
    python -m evals.backtest.trend_calibration --only role_down role_up
"""
from __future__ import annotations

import argparse
import logging

from nfl_mcp import opportunity, role_shift
from nfl_mcp import sleeper_projections as sp
from nfl_mcp.projections import RETURNING_KEEP_WEIGHT

from .metrics import bias, mae
from .sleeper_blend import build_samples

POST_BREAK_GRID = (1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
KEEP_GRID = tuple(x / 10 for x in range(11))
DOWN_STRENGTH_GRID = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0)
MIN_MULT_GRID = (0.70, 0.75, 0.80, 0.85, 0.90)
ONE_WEEK_GRID = (0.5, 0.75, 1.0)
UP_STRENGTH_GRID = (0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5)
MAX_MULT_GRID = (1.10, 1.15, 1.25)
_POS = ("RB", "WR", "TE")


def _w() -> float:
    return sp.BLEND_MODEL_WEIGHT


def _blend(s: dict, model: float, sleeper_mult: float = 1.0) -> float:
    if s["sleeper_status"] == "not_projected":
        return 0.0
    return _w() * model + (1 - _w()) * s["sleeper"] * sleeper_mult


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------
# Returning teammate
# --------------------------------------------------------------------------
def sweep_returning(samples: list[dict]) -> dict[float, dict]:
    rows = [s for s in samples if "ret_full_rate" in s and s["sleeper"] is not None]
    out: dict[float, dict] = {}
    for k in KEEP_GRID:
        cells = {}
        for pos in ("QB", *_POS, "ALL"):
            sub = [s for s in rows if pos in ("ALL", s["position"])]
            if not sub:
                continue
            model = [(k * s["ret_full_rate"] + (1 - k) * s["ret_kept_rate"]) * s["ret_scale"]
                     for s in sub]
            act = [s["actual"] for s in sub]
            fut = [(m, s["actual_next4"]) for m, s in zip(model, sub, strict=True)
                   if s.get("actual_next4") is not None]
            cells[pos] = {
                "n": len(sub),
                "model": mae(model, act), "bias": bias(model, act),
                "blend": mae([_blend(s, m) for s, m in zip(sub, model, strict=True)], act),
                "next4": mae([m for m, _ in fut], [a for _, a in fut]) if fut else None,
            }
        out[k] = cells
    return out


def report_returning(samples: list[dict]) -> None:
    kinds = sorted({s.get("ret_kind") for s in samples if s.get("ret_kind")})
    subsets = [("all", samples)]
    if len(kinds) > 1:
        # same: a same-position teammate only; cross: the other pass-catching
        # position's (a WR back for a TE, Jefferson for Hockenson), alone or both.
        subsets += [("same-position only", [s for s in samples if s.get("ret_kind") in
                                            (None, "same")]),
                    ("cross-position (alone or with same)",
                     [s for s in samples if s.get("ret_kind") in (None, "cross", "both")])]
    for label, rows in subsets:
        res = sweep_returning(rows)
        if not res:
            continue
        n = res[KEEP_GRID[0]]["ALL"]["n"]
        print(f"\nRETURNING_KEEP_WEIGHT sweep (live {RETURNING_KEEP_WEIGHT}), {label}: "
              f"n={n} affected rows")
        print("  keep | ALL model MAE (bias) | ALL blend | ALL model vs next-4 | "
              "model RB / WR / TE | next-4 RB / WR / TE")
        for k, cells in res.items():
            a = cells["ALL"]
            per = " / ".join(f"{cells[p]['model']:.3f}" if p in cells else "  -  " for p in _POS)
            fut = " / ".join(f"{cells[p]['next4']:.3f}" if p in cells and cells[p]["next4"]
                             else "  -  " for p in _POS)
            print(f"  {k:4.1f} | {a['model']:.3f} ({a['bias']:+.2f}) | {a['blend']:.3f} | "
                  f"{a['next4']:.3f} | {per} | {fut}")
        for metric in ("model", "blend", "next4"):
            best = min(res, key=lambda k, m=metric: res[k]["ALL"][m])
            per = ", ".join(
                f"{p} {min(res, key=lambda k, m=metric, p=p: res[k][p][m] or 9e9):.1f}"
                for p in _POS if p in res[KEEP_GRID[0]])
            print(f"  best by {metric}: ALL {best:.1f} ({per})")
        report_trajectory(rows)


def _ratio(rows: list[dict], num: str, den: str) -> float:
    return sum(s[num] for s in rows) / max(1e-9, sum(s[den] for s in rows))


def report_trajectory(samples: list[dict]) -> None:
    """How big is the drop a teammate's return really causes, over the next
    four games -- the size `value_trajectory` reports as ``returning_teammate``?

    Every player regresses from his trailing rate, so the drop attributable to
    the teammate is the affected rows' next-4 / trailing ratio against the
    unaffected rows' (same position). Predicted: the deflated rate against the
    unregressed trailing rate (``per_game_recent``, what `value_trajectory`
    reads since #251) and against the regressed full rate
    (``per_game_until_return``)."""
    rows = [s for s in samples if s.get("actual_next4") is not None and s["model_raw"] > 0]
    print("\n  Trajectory size (teammate back this week), next-4 mean vs trailing rate:")
    print("    pos   n | realised excess | predicted vs recent at keep 0.3/0.5/0.6/0.7"
          " | predicted vs regressed rate at 0.3/0.5/0.6/0.7"
          " | unregressed (kept vs trailing opportunity rate) at 0.3/0.5/0.6/0.7")
    for pos in (*_POS, "ALL"):
        aff = [s for s in rows if "ret_full_rate" in s and pos in ("ALL", s["position"])]
        ctrl = [s for s in rows if "ret_full_rate" not in s and pos in ("ALL", s["position"])]
        if not aff or not ctrl:
            continue
        # Realised: the affected rows' next-4 / recent, over the control's.
        excess = _ratio(aff, "actual_next4", "model_raw") / _ratio(
            ctrl, "actual_next4", "model_raw") - 1
        # The control's own regression, so "vs recent" compares like for like.
        ctrl_reg = _ratio(ctrl, "model", "model_raw")
        vs_recent, vs_reg, raw = [], [], []
        for k in (0.3, 0.5, 0.6, 0.7):
            raw.append(sum((k * s["model_raw"] + (1 - k) * s["ret_kept_raw"] * s["ret_scale"])
                           for s in aff) / sum(s["model_raw"] for s in aff) - 1)
            after = sum((k * s["ret_full_rate"] + (1 - k) * s["ret_kept_rate"]) * s["ret_scale"]
                        for s in aff)
            vs_recent.append(after / sum(s["model_raw"] for s in aff) - 1)
            vs_reg.append(after / sum(s["model"] for s in aff) - 1)
        print(f"    {pos:3s} {len(aff):4d} | {excess:+6.1%} | "
              + " ".join(f"{v:+6.1%}" for v in vs_recent)
              + f" (control regression {ctrl_reg - 1:+.1%}) | "
              + " ".join(f"{v:+6.1%}" for v in vs_reg)
              + " | unregressed " + " ".join(f"{v:+6.1%}" for v in raw))


# --------------------------------------------------------------------------
# Role shift
# --------------------------------------------------------------------------
def _matched(samples: list[dict]) -> list[dict]:
    return [s for s in samples if s["sleeper"] is not None and "role_trend" in s]


def _down_mult(s: dict, strength: float, lo: float, one_week: float) -> float:
    weight = 1.0 if s.get("role_weeks", 0) >= 2 else one_week
    return _clamp(1 + strength * weight * s["role_rel"], lo, 1.0)


def _live_pred(s: dict) -> float:
    """The live engine's blend for a row (lost role priced, gained role not)."""
    if s["role_trend"] == "role_down" and s["position"] in _POS:
        strength = role_shift.DOWN_STRENGTH.get(s["position"], 0.0)
        mult = _down_mult(s, strength, role_shift.MIN_MULTIPLIER, role_shift.ONE_WEEK_WEIGHT)
        return _blend(s, s["model_role_at"][opportunity.POST_BREAK_WEIGHT], mult)
    return _blend(s, s["model"])


def report_role_down(samples: list[dict]) -> None:
    matched = _matched(samples)
    down = [s for s in matched if s["role_trend"] == "role_down" and s["position"] in _POS
            and "model_role_at" in s]
    print(f"\nRole down: n={len(down)} rows "
          + ", ".join(f"{p} {sum(s['position'] == p for s in down)}" for p in _POS))
    print("  POST_BREAK_WEIGHT (model MAE on role_down rows; live "
          f"{opportunity.POST_BREAK_WEIGHT}):")
    for p in (*_POS, "ALL"):
        sub = [s for s in down if p in ("ALL", s["position"])]
        act = [s["actual"] for s in sub]
        cells = {w: mae([s["model_role_at"][w] for s in sub], act) for w in POST_BREAK_GRID}
        best = min(cells, key=cells.get)
        print(f"    {p:3s} plain {mae([s['model'] for s in sub], act):.3f} | "
              + " ".join(f"{w:g}:{m:.3f}" for w, m in cells.items()) + f" best {best:g}")
    pbw = opportunity.POST_BREAK_WEIGHT
    print(f"  Sleeper-share multiplier, blend MAE on role_down rows (model at {pbw}):")
    for p in _POS:
        sub = [s for s in down if s["position"] == p]
        act = [s["actual"] for s in sub]
        base = mae([_blend(s, s["model_role_at"][pbw]) for s in sub], act)
        live_s = role_shift.DOWN_STRENGTH.get(p, 0.0)
        grid = {}
        for st in DOWN_STRENGTH_GRID:
            for lo in MIN_MULT_GRID:
                for ow in ONE_WEEK_GRID:
                    grid[(st, lo, ow)] = mae(
                        [_blend(s, s["model_role_at"][pbw], _down_mult(s, st, lo, ow))
                         for s in sub], act)
        live = grid.get((live_s, role_shift.MIN_MULTIPLIER, role_shift.ONE_WEEK_WEIGHT))
        best = min(grid, key=grid.get)
        print(f"    {p} n={len(sub)}: no mult {base:.3f} | live (s={live_s}, lo="
              f"{role_shift.MIN_MULTIPLIER}, 1wk={role_shift.ONE_WEEK_WEIGHT}) {live:.3f} | "
              f"best s={best[0]} lo={best[1]} 1wk={best[2]} {grid[best]:.3f}")
        by_s = {st: min(v for (s2, _, _), v in grid.items() if s2 == st)
                for st in DOWN_STRENGTH_GRID}
        print("      by strength (best bounds): "
              + " ".join(f"{st:g}:{m:.3f}" for st, m in by_s.items()))
        lo_line = {lo: grid[(live_s, lo, role_shift.ONE_WEEK_WEIGHT)] for lo in MIN_MULT_GRID}
        print(f"      by MIN_MULTIPLIER at s={live_s}: "
              + " ".join(f"{lo:g}:{m:.3f}" for lo, m in lo_line.items()))
    act = [s["actual"] for s in matched]
    print(f"  ALL rows n={len(matched)}: blend unpriced "
          f"{mae([_blend(s, s['model']) for s in matched], act):.3f} -> live "
          f"{mae([_live_pred(s) for s in matched], act):.3f}")


def _gates(s: dict) -> dict[str, bool]:
    up = s["role_trend"] == "role_up" and "model_role_at" in s
    two = up and s.get("role_weeks", 0) >= 2
    clean = two and not s.get("role_mates_absent")
    return {"all_up": up, "two_weeks": two, "gated": clean,
            "gated_rbwr": clean and s["position"] in ("RB", "WR")}


def report_role_up(samples: list[dict]) -> None:
    matched = _matched(samples)
    live = [_live_pred(s) for s in matched]
    act_all = [s["actual"] for s in matched]
    base_all = mae(live, act_all)
    ctrl = [s for s in matched if s["role_trend"] == "stable"]
    ctrl_ratio = sum(s["actual"] for s in ctrl) / max(1e-9, sum(_blend(s, s["model"])
                                                             for s in ctrl))
    print(f"\nRole up (gated pricing). ALL rows live blend MAE {base_all:.4f}; "
          f"stable rows actual/blend {ctrl_ratio:.3f}")
    for gate in ("all_up", "two_weeks", "gated", "gated_rbwr"):
        for pos in (*_POS, "ALL"):
            if gate == "gated_rbwr" and pos == "TE":
                continue
            idx = [i for i, s in enumerate(matched) if _gates(s)[gate]
                   and pos in ("ALL", s["position"]) and s["position"] in _POS]
            if not idx:
                continue
            sub = [matched[i] for i in idx]
            act = [s["actual"] for s in sub]
            before = [live[i] for i in idx]
            ratio = sum(act) / max(1e-9, sum(before))
            msg = (f"  {gate:10s} {pos:3s} n={len(sub):4d} blend {mae(before, act):.3f} "
                   f"{bias(before, act):+.2f} ratio {ratio:.3f}")
            best = min(((mae([_up_pred(s, st, hi, rw) for s in sub], act), st, hi, rw)
                        for rw in (False, True) for st in UP_STRENGTH_GRID
                        for hi in MAX_MULT_GRID), key=lambda b: b[0])
            sweep = " ".join(f"{st:g}:{mae([_up_pred(s, st) for s in sub], act):.3f}"
                             for st in UP_STRENGTH_GRID)
            msg += (f" | strength (max {role_shift.MAX_MULTIPLIER}) {sweep} | best s={best[1]} "
                    f"max={best[2]} reweight={best[3]} {best[0]:.3f}")
            if pos == "ALL":
                # The whole population with the best gated pricing applied.
                chosen = set(idx)
                full = [_up_pred(s, best[1], best[2], best[3]) if i in chosen else live[i]
                        for i, s in enumerate(matched)]
                msg += f" | ALL rows {base_all:.4f} -> {mae(full, act_all):.4f}"
            elif gate == "gated" and role_shift.UP_STRENGTH.get(pos, 0.0) > 0:
                # The live strength: the change on these rows, 95% bootstrap.
                st = role_shift.UP_STRENGTH[pos]
                d = [abs(_up_pred(s, st) - s["actual"]) - abs(b - s["actual"])
                     for s, b in zip(sub, before, strict=True)]
                lo, hi = _bootstrap_mean(d)
                msg += f" | live s={st}: {sum(d) / len(d):+.3f} [{lo:+.3f}, {hi:+.3f}]"
            print(msg)


def _up_pred(s: dict, strength: float, hi: float | None = None,
             reweight: bool = False) -> float:
    """A gained role priced like a lost one: Sleeper's share x (1 + strength
    x the relative volume gain), capped; the model plain or reweighted from
    the break week."""
    model = s["model_role_at"][opportunity.POST_BREAK_WEIGHT] if reweight else s["model"]
    cap = role_shift.MAX_MULTIPLIER if hi is None else hi
    return _blend(s, model, _clamp(1 + strength * s["role_rel"], 1.0, cap))


def _bootstrap_mean(d: list[float], n: int = 2000, seed: int = 1) -> tuple[float, float]:
    import random
    rng = random.Random(seed)
    boots = sorted(sum(rng.choices(d, k=len(d))) / len(d) for _ in range(n))
    return boots[int(0.025 * n)], boots[int(0.975 * n) - 1]


def by_season(samples: list[dict], fn) -> None:
    seasons = sorted({s["season"] for s in samples})
    for season in seasons:
        print(f"\n=== season {season} ===")
        fn([s for s in samples if s["season"] == season])


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seasons", type=int, nargs="+", default=[2023, 2024, 2025])
    ap.add_argument("--only", nargs="+", choices=("returning", "role_down", "role_up"),
                    default=("returning", "role_down", "role_up"))
    ap.add_argument("--cross-position", action="store_true",
                    help="returning teammates also from the other pass-catching position "
                         "(as live), reported apart")
    ap.add_argument("--per-season", action="store_true",
                    help="also repeat each sweep per season (stability check)")
    args = ap.parse_args()
    samples = build_samples(args.seasons, with_role=True, with_returning=True,
                            post_break_weights=POST_BREAK_GRID,
                            cross_position=args.cross_position)
    reports = {"returning": report_returning, "role_down": report_role_down,
               "role_up": report_role_up}
    for name in args.only:
        reports[name](samples)
        if args.per_season:
            by_season(samples, reports[name])


if __name__ == "__main__":
    main()
