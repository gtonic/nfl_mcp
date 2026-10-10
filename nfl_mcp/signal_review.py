"""The weekly signal review: is each projection signal's weight still right?

`projection_accuracy` grades every logged pre-kickoff projection against what
the player scored, with the signals that were active on it. This reads those
graded rows the way a calibration review would, one signal at a time:

- **realised ratio**: what the rows carrying the signal scored over what they
  were projected (a ratio of sums — one 0-for-14 week does not swamp it the
  way a mean of ratios would);
- **relative ratio**: that over the same ratio for a baseline — healthy,
  undesignated players for an injury / practice bucket, every other row with
  signals for the rest. A calibrated signal sits at 1.0: its rows scored the
  same share of their projection as rows without it. Above 1.0 the
  projection under-rates players with the signal (the multiplier is too
  low), below 1.0 it over-rates them;
- a 95% **bootstrap interval** on the relative ratio (the signal's rows
  resampled; the baseline's sampling error, usually far larger in n, drawn
  from its delta-method standard error), bias and MAE with vs without, for
  the week and cumulative over the season's graded weeks;
- the **implied multiplier** — the current value times the relative ratio —
  when the signal has one (the practice buckets' realised shares, the mean
  role / news / QB multiplier the rows were logged with).

Each signal gets a recommendation under explicit minimum-sample rules so a
single week cannot move a weight: below :data:`MIN_N_READ` rows nothing is
read; an interval that excludes 1.0 on fewer than :data:`MIN_N_ACT` rows (or
fewer than :data:`MIN_WEEKS_ACT` weeks) is a "watch" — keep the value; only
past both, with the shift at least :data:`MIN_SHIFT`, is it a "review" —
and even then the line says to confirm with the multi-season backtest
(`evals/backtest`) before changing a constant.

It also lists the week's biggest individual misses among the user's rostered
players with the signals that were active, for a human read of what the
aggregate hides. The prefetch's ``accuracy`` scope stores each newly graded
week's review (``signal_reviews``) so reading it back is cheap.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import zlib
from collections.abc import Callable
from datetime import UTC, datetime

from .errors import create_success_response

logger = logging.getLogger(__name__)

# Minimum-sample rules (see the module doc).
MIN_N_READ = 10
MIN_N_ACT = 60
MIN_WEEKS_ACT = 2
MIN_SHIFT = 0.10
BOOTSTRAP_ITERS = 1000
CI_LEVEL = 0.95
TOP_MISSES = 8
# Stored review scope: every graded row of the week.
SCOPE_ALL = "all"


# --------------------------------------------------------------------------
# Row helpers
# --------------------------------------------------------------------------

def _sig(row: dict) -> dict:
    raw = row.get("signals")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _flags(row: dict) -> list[str]:
    raw = row.get("news_flags")
    if isinstance(raw, list):
        return raw
    try:
        out = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return out if isinstance(out, list) else []


def _status(row: dict) -> str:
    return (row.get("injury_status") or _sig(row).get("injury_status") or "").strip().lower()


def _practice(row: dict) -> tuple[str, int]:
    """``(latest practice day, days reported)`` as logged."""
    sig = _sig(row)
    latest = (row.get("practice_status") or sig.get("practice_status") or "").strip().upper()
    pattern = row.get("practice_pattern") or sig.get("practice_pattern") or ""
    days = [d for d in str(pattern).split("-") if d.strip()]
    if not latest and days:
        latest = days[-1].strip().upper()
    return latest, len(days)


def _gameday(row: dict) -> str | None:
    return _sig(row).get("gameday_status")


def _healthy(row: dict) -> bool:
    return not _status(row) and _gameday(row) != "inactive"


def _q_bucket(practice: str, days: int) -> Callable[[dict], bool]:
    def pred(r: dict) -> bool:
        if _status(r) != "questionable" or _gameday(r):
            return False
        latest, n = _practice(r)
        if practice == "NONE":
            return latest == ""
        if practice == "FP":
            return latest in ("FP", "REST")
        if practice == "DNP":
            return latest == "DNP" and n >= 2
        if practice == "DNP_SINGLE":
            return latest == "DNP" and n < 2
        return latest == practice
    return pred


def _current_constants() -> dict[str, float]:
    """The values the projection engine applies today (read live, so the
    review tracks a re-calibration without being edited)."""
    from . import injury_status
    from .projections import CONFIRMED_ACTIVE_REALISED, QUESTIONABLE_REALISED
    return {
        "q_fp": QUESTIONABLE_REALISED["FP"],
        "q_lp": QUESTIONABLE_REALISED["LP"],
        "q_dnp": QUESTIONABLE_REALISED["DNP"],
        "q_dnp_single": QUESTIONABLE_REALISED["DNP_SINGLE"],
        "q_no_practice": QUESTIONABLE_REALISED["NONE"],
        "doubtful": injury_status.DOUBTFUL_MULT,
        "gameday_active_q": CONFIRMED_ACTIVE_REALISED["NONE"],
        "gameday_active_d": CONFIRMED_ACTIVE_REALISED["DNP"],
    }


def _mean_logged(key: str) -> Callable[[list[dict]], float | None]:
    """The mean multiplier the rows were logged with (None: not logged)."""
    def current(rows: list[dict]) -> float | None:
        vals = [float(_sig(r)[key]) for r in rows
                if isinstance(_sig(r).get(key), int | float)]
        return round(sum(vals) / len(vals), 3) if vals else None
    return current


# name -> (label, predicate, baseline, current value: constant key / logged key / None)
def _catalogue(rows: list[dict]) -> list[dict]:
    consts = _current_constants()

    def const(name):
        return lambda _rows, v=consts[name]: v

    cat = [
        {"name": "q_fp", "group": "practice", "label": "Questionable + full practice",
         "param": "projections.QUESTIONABLE_REALISED['FP']",
         "pred": _q_bucket("FP", 0), "baseline": "healthy", "current": const("q_fp")},
        {"name": "q_lp", "group": "practice", "label": "Questionable + limited practice",
         "param": "projections.QUESTIONABLE_REALISED['LP']",
         "pred": _q_bucket("LP", 0), "baseline": "healthy", "current": const("q_lp")},
        {"name": "q_dnp", "group": "practice", "label": "Questionable + DNP week",
         "param": "projections.QUESTIONABLE_REALISED['DNP']",
         "pred": _q_bucket("DNP", 2), "baseline": "healthy", "current": const("q_dnp")},
        {"name": "q_dnp_single", "group": "practice",
         "label": "Questionable + a single DNP so far",
         "param": "projections.QUESTIONABLE_REALISED['DNP_SINGLE']",
         "pred": _q_bucket("DNP_SINGLE", 1), "baseline": "healthy",
         "current": const("q_dnp_single")},
        {"name": "q_no_practice", "group": "practice",
         "label": "Questionable, no practice line",
         "param": "projections.QUESTIONABLE_REALISED['NONE']",
         "pred": _q_bucket("NONE", 0), "baseline": "healthy",
         "current": const("q_no_practice")},
        {"name": "doubtful", "group": "practice", "label": "Doubtful (before inactives)",
         "param": "injury_status.DOUBTFUL_MULT",
         "pred": lambda r: _status(r) == "doubtful" and not _gameday(r),
         "baseline": "healthy", "current": const("doubtful")},
        {"name": "gameday_active_q", "group": "gameday",
         "label": "Questionable, confirmed active at inactives",
         "param": "projections.CONFIRMED_ACTIVE_REALISED",
         "pred": lambda r: _gameday(r) == "active" and _status(r) == "questionable",
         "baseline": "healthy", "current": const("gameday_active_q")},
        {"name": "gameday_active_d", "group": "gameday",
         "label": "Doubtful/Out tag, confirmed active at inactives",
         "param": "projections.CONFIRMED_ACTIVE_REALISED['DNP']",
         "pred": lambda r: _gameday(r) == "active" and _status(r) in ("doubtful", "out"),
         "baseline": "healthy", "current": const("gameday_active_d")},
        {"name": "gameday_inactive", "group": "gameday", "label": "Officially inactive",
         "param": None, "pred": lambda r: _gameday(r) == "inactive",
         "baseline": "healthy", "current": None},
        {"name": "role_down", "group": "role", "label": "Role trend down",
         "param": "role_shift role multiplier (Sleeper's share)",
         "pred": lambda r: r.get("role_trend") == "role_down", "baseline": "rest",
         "current": _mean_logged("role_mult")},
        {"name": "role_up", "group": "role", "label": "Role trend up",
         "param": None, "pred": lambda r: r.get("role_trend") == "role_up",
         "baseline": "rest", "current": None},
        {"name": "role_up_gain_priced", "group": "role",
         "label": "Role gain priced (no absence explains it)",
         "param": "role_shift.UP_STRENGTH",
         "pred": lambda r: bool(_sig(r).get("role_gain_priced")), "baseline": "rest",
         "current": _mean_logged("role_mult")},
        {"name": "returning_teammates", "group": "role", "label": "Returning teammates",
         "param": "opportunity RETURNING_KEEP_WEIGHT",
         "pred": lambda r: (r.get("returning_teammates") or 0) > 0, "baseline": "rest",
         "current": None},
        {"name": "inherited_volume", "group": "role", "label": "Inherited volume",
         "param": None, "pred": lambda r: bool(_sig(r).get("inherited_volume")),
         "baseline": "rest", "current": None},
        {"name": "injury_exit", "group": "role",
         "label": "Injury-shortened game left out of the volume",
         "param": None, "pred": lambda r: bool(_sig(r).get("injury_exit")),
         "baseline": "rest", "current": None},
        {"name": "qb_coupling", "group": "qb_context", "label": "Backup-QB coupling",
         "param": "qb_coupling.RECEIVER_MULT",
         "pred": lambda r: r.get("qb_mult") is not None and float(r["qb_mult"]) < 1.0,
         "baseline": "rest", "current": _mean_logged("qb_mult")},
    ]
    for basis in ("gameday", "status", "practice"):
        cat.append({
            "name": f"qb_coupling:{basis}", "group": "qb_context",
            "label": f"Backup-QB coupling, starter's sit weight from the {basis}",
            "param": "qb_coupling.starter_sit_weight",
            "pred": (lambda b: lambda r: _sig(r).get("qb_sit_basis") == b
                     and r.get("qb_mult") is not None and float(r["qb_mult"]) < 1.0)(basis),
            "baseline": "rest", "current": _mean_logged("qb_mult")})
    for flag in sorted({f for r in rows for f in _flags(r)}):
        cat.append({
            "name": f"news:{flag}", "group": "news", "label": f"News flag {flag}",
            "param": "news_signals adjustment",
            "pred": (lambda fl: lambda r: fl in _flags(r))(flag),
            "baseline": "rest", "current": _mean_logged("news_model_mult")})
    for src in sorted({r.get("projection_source") for r in rows if r.get("projection_source")}):
        cat.append({
            "name": f"source:{src}", "group": "projection_source",
            "label": f"Projection source {src}", "param": None,
            "pred": (lambda s: lambda r: r.get("projection_source") == s)(src),
            "baseline": "rest", "current": None})
    cat.append({
        "name": "sleeper_disagreement", "group": "projection_source",
        "label": "Ours and Sleeper's 4+ points apart", "param": None,
        "pred": lambda r: (r.get("model_projection") is not None
                           and r.get("sleeper_projection") is not None
                           and abs(float(r["model_projection"])
                                   - float(r["sleeper_projection"])) > 4.0),
        "baseline": "rest", "current": None})
    return cat


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

def _ratio(rows: list[dict]) -> float | None:
    proj = sum(float(r["projected"]) for r in rows)
    if proj <= 0:
        return None
    return sum(float(r["actual"]) for r in rows) / proj


def _ratio_se(rows: list[dict]) -> float:
    """Delta-method standard error of a ratio of sums."""
    proj = sum(float(r["projected"]) for r in rows)
    ratio = _ratio(rows)
    if ratio is None or proj <= 0:
        return 0.0
    resid = sum((float(r["actual"]) - ratio * float(r["projected"])) ** 2 for r in rows)
    return math.sqrt(resid) / proj


def _err(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "mae": None, "bias": None}
    errs = [float(r["projected"]) - float(r["actual"]) for r in rows]
    return {"n": n, "mae": round(sum(abs(e) for e in errs) / n, 2),
            "bias": round(sum(errs) / n, 2)}


def bootstrap_relative(hit: list[dict], base: list[dict], seed: str,
                       iters: int = BOOTSTRAP_ITERS) -> tuple[float, float] | None:
    """95% interval of (hit ratio / baseline ratio); deterministic per ``seed``.

    The hit rows are resampled with replacement; the baseline ratio is drawn
    from its delta-method normal (it is usually ten times the size, and
    resampling it every iteration is what made this slow)."""
    if len(hit) < 2 or not base:
        return None
    base_ratio = _ratio(base)
    if not base_ratio:
        return None
    base_se = _ratio_se(base)
    rng = random.Random(zlib.crc32(seed.encode()))
    pairs = [(float(r["projected"]), float(r["actual"])) for r in hit]
    n = len(pairs)
    draws = []
    for _ in range(iters):
        sample = rng.choices(pairs, k=n)
        proj = sum(p for p, _ in sample)
        if proj <= 0:
            continue
        b = base_ratio + rng.gauss(0.0, base_se)
        if b <= 0:
            continue
        draws.append(sum(a for _, a in sample) / proj / b)
    if len(draws) < iters // 2:
        return None
    draws.sort()
    tail = (1.0 - CI_LEVEL) / 2.0
    lo = draws[int(tail * (len(draws) - 1))]
    hi = draws[int((1.0 - tail) * (len(draws) - 1))]
    return round(lo, 3), round(hi, 3)


def _measure(hit: list[dict], base: list[dict], seed: str) -> dict:
    ratio, base_ratio = _ratio(hit), _ratio(base)
    rel = (ratio / base_ratio) if ratio is not None and base_ratio else None
    w, wo = _err(hit), _err(base)
    return {
        "n": len(hit),
        "weeks": sorted({int(r["week"]) for r in hit}),
        "projected_mean": round(sum(float(r["projected"]) for r in hit) / len(hit), 2)
        if hit else None,
        "actual_mean": round(sum(float(r["actual"]) for r in hit) / len(hit), 2)
        if hit else None,
        "realised_ratio": round(ratio, 3) if ratio is not None else None,
        "baseline_ratio": round(base_ratio, 3) if base_ratio is not None else None,
        "baseline_n": len(base),
        "relative_ratio": round(rel, 3) if rel is not None else None,
        "relative_ci": bootstrap_relative(hit, base, seed) if hit else None,
        "bias": w["bias"], "mae": w["mae"],
        "baseline_bias": wo["bias"], "baseline_mae": wo["mae"],
        "mae_gap": (round(w["mae"] - wo["mae"], 2)
                    if w["mae"] is not None and wo["mae"] is not None else None),
    }


def recommend(entry: dict) -> tuple[str, str]:
    """``(verdict, line)`` for one signal's cumulative read (see the module
    doc's rules). Verdicts: insufficient, calibrated, watch, review."""
    c = entry["cumulative"]
    label, n = entry["label"], c["n"]
    rel, ci = c.get("relative_ratio"), c.get("relative_ci")
    weeks = len(c.get("weeks") or [])
    current = entry.get("current")
    if n < MIN_N_READ or rel is None:
        return "insufficient", (f"{label}: n={n} — too few to read (need n≥{MIN_N_READ}); "
                                "no change.")
    ci_txt = f"[{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "[no interval]"
    if current is not None:
        implied = entry.get("implied")
        lo, hi = (entry.get("implied_ci") or (None, None))
        what = (f"{label} multiplier {current:g} looks "
                f"{'too low' if rel > 1 else 'too high'}: realised {implied:.2f}"
                + (f" [{lo:.2f}, {hi:.2f}]" if lo is not None else ""))
        calm = f"{label} multiplier {current:g} holds: realised {implied:.2f}" + (
            f" [{lo:.2f}, {hi:.2f}]" if lo is not None else "")
    else:
        lean = "under-rates" if rel > 1 else "over-rates"
        what = (f"{label}: rows scored {rel:.2f}× the baseline's share of their projection "
                f"{ci_txt} — the projection {lean} them")
        calm = f"{label}: {rel:.2f}× the baseline {ci_txt} — no clear bias"
    excludes_one = bool(ci) and (ci[0] > 1.0 or ci[1] < 1.0)
    if not excludes_one:
        more = (f" — keep collecting until n≥{MIN_N_ACT}" if n < MIN_N_ACT else "")
        return "calibrated", f"{calm}, n={n}{more}; keep."
    if n < MIN_N_ACT or weeks < MIN_WEEKS_ACT:
        need = [f"n≥{MIN_N_ACT}" if n < MIN_N_ACT else None,
                f"≥{MIN_WEEKS_ACT} weeks" if weeks < MIN_WEEKS_ACT else None]
        return "watch", (f"{what}, n={n} over {weeks} week(s) — keep until "
                         f"{' and '.join(x for x in need if x)}.")
    if abs(rel - 1.0) < MIN_SHIFT:
        return "calibrated", (f"{calm}, n={n} — off by {abs(rel - 1) * 100:.0f}%, under the "
                              f"{MIN_SHIFT * 100:.0f}% worth acting on; keep.")
    target = (f"move {entry['param']} toward {entry['implied']:.2f}"
              if current is not None and entry.get("param") else
              f"re-weight {entry['param'] or 'the signal'}")
    return "review", (f"{what}, n={n} over {weeks} weeks — consider: {target} "
                      "(confirm with the multi-season backtest before changing it).")


def _graded(rows: list[dict]) -> list[dict]:
    """Rows worth reading: with signals, not a 0-projected 0-scored bye/out."""
    return [r for r in rows if _sig(r)
            and not (float(r["projected"]) == 0 and float(r["actual"]) == 0)]


def review_rows(rows: list[dict], week: int) -> dict:
    """The review of ``week`` (and cumulative through it) from graded rows; pure."""
    rows = [r for r in rows if int(r["week"]) <= week]
    usable = _graded(rows)
    this_week = [r for r in usable if int(r["week"]) == week]
    signals = []
    for spec in _catalogue(usable):
        pred = spec["pred"]
        cum_hit = [r for r in usable if pred(r)]
        if not cum_hit:
            continue
        healthy_only = spec["baseline"] == "healthy"

        def base_of(pool, pred=pred, healthy_only=healthy_only):
            return [r for r in pool if not pred(r) and (not healthy_only or _healthy(r))]
        wk_hit = [r for r in this_week if pred(r)]
        cumulative = _measure(cum_hit, base_of(usable), f"{spec['name']}:cum")
        entry = {
            "signal": spec["name"], "group": spec["group"], "label": spec["label"],
            "baseline": spec["baseline"], "param": spec["param"],
            "week": _measure(wk_hit, base_of(this_week), f"{spec['name']}:{week}")
            if wk_hit else {"n": 0},
            "cumulative": cumulative,
        }
        current = spec["current"](cum_hit) if spec["current"] else None
        if current is not None:
            entry["current"] = current
            rel, ci = cumulative.get("relative_ratio"), cumulative.get("relative_ci")
            if rel is not None:
                entry["implied"] = round(current * rel, 3)
                entry["implied_ci"] = ((round(current * ci[0], 3), round(current * ci[1], 3))
                                       if ci else None)
        entry["verdict"], entry["recommendation"] = recommend(entry)
        signals.append(entry)
    order = {"review": 0, "watch": 1, "calibrated": 2, "insufficient": 3}
    signals.sort(key=lambda e: (order[e["verdict"]], -e["cumulative"]["n"]))
    return {
        "week": week,
        "weeks_cumulative": sorted({int(r["week"]) for r in usable}),
        "rows_week": len(this_week),
        "rows_cumulative": len(usable),
        "rows_without_signals": sum(1 for r in rows if not _sig(r)),
        "overall_week": _err(this_week),
        "overall_cumulative": _err(usable),
        "signals": signals,
        "recommendations": [e["recommendation"] for e in signals
                            if e["verdict"] in ("review", "watch")],
        "not_logged": [
            "risk_mode choices (a lineup-level decision, not a per-projection signal)",
        ],
        "rules": {
            "min_n_read": MIN_N_READ, "min_n_act": MIN_N_ACT,
            "min_weeks_act": MIN_WEEKS_ACT, "min_shift": MIN_SHIFT,
            "ci": f"{int(CI_LEVEL * 100)}% bootstrap ({BOOTSTRAP_ITERS} resamples)",
            "verdicts": {
                "insufficient": f"n<{MIN_N_READ}: shown, not read",
                "calibrated": "the interval includes 1.0 (or the shift is under "
                              f"{int(MIN_SHIFT * 100)}%): keep",
                "watch": f"the interval excludes 1.0 but n<{MIN_N_ACT} or fewer than "
                         f"{MIN_WEEKS_ACT} weeks: keep, keep watching",
                "review": "the interval excludes 1.0 on enough rows and weeks: worth a "
                          "backtest of the weight",
            },
        },
    }


# --------------------------------------------------------------------------
# Signals in words (misses, retro)
# --------------------------------------------------------------------------

def describe_signals(row_or_signals: dict) -> list[str]:
    """The signals that moved a projection, in words, from a graded row or a
    logged ``signals`` dict."""
    sig = row_or_signals if "signals" not in row_or_signals else _sig(row_or_signals)
    sig = sig or {}
    out = []
    status = (sig.get("injury_status") or "").strip()
    if status:
        practice = sig.get("practice_pattern") or sig.get("practice_status")
        mult = sig.get("injury_mult")
        out.append(f"{status}" + (f" ({practice})" if practice else "")
                   + (f" ×{mult:g} on ours" if isinstance(mult, int | float) else "")
                   + (f", ×{sig['practice_blend_mult']:g} on Sleeper's"
                      if isinstance(sig.get("practice_blend_mult"), int | float) else ""))
    if sig.get("gameday_status"):
        out.append(f"gameday: {sig['gameday_status']}")
    trend = sig.get("role_trend")
    if trend in ("role_down", "role_up"):
        mult = sig.get("role_mult")
        out.append(trend + (f" ×{mult:g}" if isinstance(mult, int | float) else "")
                   + (" (gain priced)" if sig.get("role_gain_priced") else ""))
    if sig.get("returning_teammates"):
        out.append(f"returning teammates: {sig['returning_teammates']}")
    if sig.get("inherited_volume"):
        out.append("inherited volume")
    if sig.get("injury_exit"):
        out.append("injury-shortened game excluded")
    if isinstance(sig.get("qb_mult"), int | float) and sig["qb_mult"] < 1.0:
        out.append(f"backup QB ×{sig['qb_mult']:g}"
                   + (f" ({sig['qb_sit_basis']})" if sig.get("qb_sit_basis") else ""))
    flags = sig.get("news_flags") or []
    if flags:
        mult = sig.get("news_model_mult")
        out.append("news: " + ", ".join(flags)
                   + (f" ×{mult:g}" if isinstance(mult, int | float) else ""))
    src = sig.get("projection_source")
    if src and src != "sleeper_blend":
        out.append(f"source: {src}")
    ours, theirs = sig.get("model_projection"), sig.get("sleeper_projection")
    if isinstance(ours, int | float) and isinstance(theirs, int | float) \
            and abs(ours - theirs) > 4.0:
        out.append(f"ours {ours:g} vs Sleeper {theirs:g}")
    return out


def biggest_misses(rows: list[dict], week: int, player_ids: set[str] | None = None,
                   top: int = TOP_MISSES) -> list[dict]:
    """The week's largest |actual − projected| rows (optionally one roster's)."""
    pool = [r for r in rows if int(r["week"]) == week
            and (player_ids is None or str(r["player_id"]) in player_ids)
            and not (float(r["projected"]) == 0 and float(r["actual"]) == 0)]
    seen: dict[str, dict] = {}
    for r in pool:  # one row per player (a player logged in two scorings)
        pid = str(r["player_id"])
        if pid not in seen or abs(float(r["actual"]) - float(r["projected"])) > abs(
                float(seen[pid]["actual"]) - float(seen[pid]["projected"])):
            seen[pid] = r
    ranked = sorted(seen.values(),
                    key=lambda r: -abs(float(r["actual"]) - float(r["projected"])))
    return [{
        "player": r.get("player_name"), "player_id": str(r["player_id"]),
        "position": r.get("position"), "team": r.get("team"),
        "projected": round(float(r["projected"]), 1), "actual": round(float(r["actual"]), 1),
        "diff": round(float(r["actual"]) - float(r["projected"]), 1),
        "played": bool(r.get("played")),
        "model_projection": r.get("model_projection"),
        "sleeper_projection": r.get("sleeper_projection"),
        "signals": describe_signals(r),
        "league_id": r.get("league_id"),
    } for r in ranked[:top]]


# --------------------------------------------------------------------------
# Store / read
# --------------------------------------------------------------------------

def _summary_payload(review: dict) -> dict:
    """What is stored: the review minus nothing (it is already compact)."""
    return review


async def store_review(db, season: int, week: int) -> dict | None:
    """Run the week's review over every graded row and store it (the
    ``accuracy`` refresh scope calls this after grading). Never raises."""
    try:
        rows = await asyncio.to_thread(db.get_projection_accuracy, season)
        rows = [r for r in rows if int(r["week"]) <= week]
        if not any(int(r["week"]) == week for r in rows):
            return None
        review = review_rows(rows, week)
        review["biggest_misses_briefing"] = biggest_misses(
            [r for r in rows if r.get("log_source") == "briefing"], week)
        review["stored_at"] = datetime.now(UTC).isoformat()
        if hasattr(db, "save_signal_review"):
            await asyncio.to_thread(db.save_signal_review, season, week, SCOPE_ALL,
                                    _summary_payload(review), review["rows_week"])
        return review
    except Exception as e:
        logger.warning(f"signal review for {season} week {week} failed: {e}")
        return None


def _stored_fresh(db, season: int, week: int) -> dict | None:
    """The stored review when it is newer than the week's last grading."""
    if not hasattr(db, "get_signal_review"):
        return None
    stored = db.get_signal_review(season, week, SCOPE_ALL)
    if not stored:
        return None
    graded_at = (db.get_accuracy_graded_weeks(season) or {}).get(week)
    if graded_at and str(stored.get("created_at") or "") < str(graded_at):
        return None
    return stored["payload"]


async def _roster_player_ids(league_id: str, week: int, roster_id: int | None,
                             user_id: str | None) -> tuple[set[str] | None, int | None, str | None]:
    """``(player ids on the roster that week, roster_id, error)`` from the
    week's Sleeper matchup (bench included); the current roster as a fallback."""
    from . import sleeper_tools
    try:
        state = await sleeper_tools.load_rosters(league_id, "lineup")
        if state.get("blocking_error"):
            return None, None, state["blocking_error"]
        mine, error = sleeper_tools.find_roster(state["rosters"], league_id, roster_id,
                                                user_id, purpose="review")
        if error:
            return None, None, error
        rid = mine["roster_id"]
        resp = await sleeper_tools.get_matchups(league_id, week)
        match = next((m for m in (resp or {}).get("matchups") or []
                      if m.get("roster_id") == rid), None)
        ids = (match or {}).get("players") or mine.get("players") or []
        return {str(p) for p in ids}, rid, None
    except Exception as e:
        return None, None, f"roster unavailable: {e}"


async def weekly_signal_review(
    db, season: int, week: int | None = None, league_id: str | None = None,
    roster_id: int | None = None, user_id: str | None = None,
    through: int | None = None,
) -> dict:
    """The review dict (no response envelope): stored when fresh and not
    league-filtered, else computed. ``week`` defaults to the newest graded."""
    graded_weeks = sorted(w for w in (db.get_accuracy_graded_weeks(season) or {})
                          if through is None or w <= through)
    if week is None:
        if not graded_weeks:
            return {"week": None, "error": "No graded week yet this season."}
        week = graded_weeks[-1]
    review = None
    source = "computed"
    rows: list[dict] | None = None
    if not league_id:
        review = _stored_fresh(db, season, week)
        source = "stored" if review else "computed"
    if review is None:
        rows = await asyncio.to_thread(db.get_projection_accuracy, season, None, None, None,
                                       league_id)
        if not any(int(r["week"]) == week for r in rows):
            return {"week": week, "error": f"Week {week} has no graded projections"
                    + (f" for league {league_id}" if league_id else "") + "."}
        review = review_rows(rows, week)
        if not league_id:
            review["biggest_misses_briefing"] = biggest_misses(
                [r for r in rows if r.get("log_source") == "briefing"], week)
            review["stored_at"] = datetime.now(UTC).isoformat()
            if hasattr(db, "save_signal_review"):
                await asyncio.to_thread(db.save_signal_review, season, week, SCOPE_ALL,
                                        review, review["rows_week"])
    review = dict(review)
    review["source"] = source
    # The user's rostered players' misses.
    if league_id:
        rows = rows if rows is not None else await asyncio.to_thread(
            db.get_projection_accuracy, season, [week], None, None, league_id)
        ids, rid, error = (None, None, None)
        if roster_id is not None or user_id:
            ids, rid, error = await _roster_player_ids(league_id, week, roster_id, user_id)
        review["biggest_misses"] = biggest_misses(rows, week, ids)
        review["misses_scope"] = (f"roster {rid} in league {league_id}" if ids is not None
                                  else f"every player logged for league {league_id} "
                                       "(your roster and the opponents' starters the "
                                       "briefing projected)")
        if error:
            review["misses_note"] = error
    else:
        review["biggest_misses"] = review.pop("biggest_misses_briefing", [])
        review["misses_scope"] = ("players logged by get_weekly_briefing (your rosters and "
                                  "your opponents' starters); pass league_id + roster_id for "
                                  "one roster")
    review.pop("biggest_misses_briefing", None)
    return review


# --------------------------------------------------------------------------
# MCP tool
# --------------------------------------------------------------------------

async def get_weekly_signal_review(
    week: int | None = None,
    league_id: str | None = None,
    roster_id: int | None = None,
    user_id: str | None = None,
    season: int | None = None,
    db=None,
) -> dict:
    """The weekly signal review (see the module doc). Finished weeks not
    graded yet are graded first."""
    from .database import get_shared_db
    from .projection_accuracy import grade_week, weeks_to_grade
    from .week_context import last_completed_week

    db = db if db is not None else get_shared_db()
    done = await last_completed_week(db)
    season = season or done["season"]
    through = done["week"] if season == done["season"] else 18
    graded_now = []
    for w in weeks_to_grade(db, season, through):
        if week is not None and w > week:
            continue
        try:
            res = await grade_week(db, season, w)
            if res.get("graded"):
                graded_now.append(w)
        except Exception as e:  # report what is there
            logger.warning(f"accuracy grading failed for week {w}: {e}")
    review = await weekly_signal_review(db, season, week, league_id=league_id,
                                        roster_id=roster_id, user_id=user_id, through=through)
    if review.get("error"):
        return create_success_response({"success": False, "season": season,
                                        "graded_now": graded_now, **review})
    return create_success_response({
        "season": season,
        "league_id": league_id,
        "graded_now": graded_now,
        **review,
        "definitions": {
            "realised_ratio": "sum(actual) / sum(projected) over the signal's rows",
            "relative_ratio": "realised_ratio / the baseline's (healthy undesignated players "
                              "for injury/practice/gameday buckets, all other rows with "
                              "signals otherwise); 1.0 = calibrated, >1 = the projection "
                              "under-rates players with the signal",
            "implied": "current multiplier × relative_ratio: the value the rows say it "
                       "should be",
            "bias": "mean projected − actual (+ = projections too high)",
            "excluded": "rows logged before signals were recorded, and player-weeks "
                        "projected 0 that scored 0",
        },
    })


__all__ = [
    "biggest_misses",
    "bootstrap_relative",
    "describe_signals",
    "get_weekly_signal_review",
    "recommend",
    "review_rows",
    "store_review",
    "weekly_signal_review",
]
