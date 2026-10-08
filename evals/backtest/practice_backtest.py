"""
Practice-participation backtest on 2023-25: what does a questionable tag
with a given practice week really cost?

QUESTION
    Three hand-set tables price the week's injury report:

    - ``projections.QUESTIONABLE_BY_PRACTICE`` -- the model share's
      multiplier for a questionable player by his latest practice day
      (FP 0.97, LP 0.72, DNP 0.54; without a practice line and Doubtful:
      ``injury_status.QUESTIONABLE_MULT`` / ``DOUBTFUL_MULT`` via
      ``_injury_mult``);
    - ``projections.PRACTICE_BLEND_MULT`` -- Sleeper's share for a
      questionable player whose week ends on DNP (0.90 one DNP so far, 0.75
      DNP on two or more days) or on LP after an FP (0.95);
    - ``qb_coupling.QUESTIONABLE_DNP_WEIGHT`` / ``DOUBTFUL_WEIGHT`` /
      ``QUESTIONABLE_WEIGHT`` -- the probability a starting QB with that tag
      does not start (his receivers' cut is scaled by it).

    The live signal history only starts in 2026 (``signal_history``). The
    nflverse ``injuries`` release has every official report back to 2009: the
    game designation (``report_status``) and the last practice day of the week
    (``practice_status``: DNP / Limited / Full) per player-week -- the
    Friday line, so a DNP there is the "DNP all week" case
    (``PRACTICE_BLEND_MULT["DNP"]``); the single-DNP and LP-after-FP patterns
    need the daily rows and stay with ``signal_history``.

METHOD (leak-free)
    The rows of ``sleeper_blend.build_samples`` with ``include_dnp`` (a
    relevant player's team played without him: truth 0), so a questionable
    player who sat is in the sample. Each row is bucketed by that week's
    report (designation x final practice day; "none" = not on the report). The
    reference is our model's projection, which reads no injury information
    (its trailing opportunity only). A bucket's implied multiplier is
    ``(sum actual / sum model)`` over the bucket divided by the same ratio on
    the unreported rows -- the share of a healthy projection those players
    really scored -- with a 95% bootstrap interval. Sleeper's archived
    projection is the last one it published (after inactives), so it is
    reported for context, not used as the reference.

    Starting QBs: a team's QB with the most pass attempts over its previous
    six games, on that week's report; did he start (nflverse ``games.csv``
    starting QB)? The share who did not is the coupling weight the data
    implies per tag.

DATA
    ``injuries_<season>.csv`` from the nflverse-data ``injuries`` release,
    cached next to the stats (``.cache/``); a network eval on first run.

RUN
    python -m evals.backtest.practice_backtest --seasons 2023 2024 2025
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import random
from collections import defaultdict
from io import StringIO

import httpx

from nfl_mcp import qb_coupling
from nfl_mcp.projections import (
    SLEEPER_QUESTIONABLE_PRICED,
    _injury_mult,
    confirmed_active_blend_mult,
    confirmed_active_mult,
    practice_adjusted_mult,
    practice_blend_mult,
)

from .data import _CACHE_DIR, load_games, load_season
from .metrics import mae
from .sleeper_blend import build_samples

logger = logging.getLogger(__name__)

INJURIES_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
                "injuries/injuries_{season}.csv")
_PRACTICE = {"did not participate in practice": "DNP",
             "limited participation in practice": "LP",
             "full participation in practice": "FP"}
_STATUS = {"questionable": "Q", "doubtful": "D", "out": "O"}
BOOTSTRAP = 2000


def load_injuries(season: int, use_cache: bool = True) -> dict[tuple[int, str], dict]:
    """``{(week, gsis_id): {status, practice}}`` for the regular season.
    ``status``: Q / D / O / "" (on the practice report without a game
    designation); ``practice``: DNP / LP / FP / ""."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = os.path.join(_CACHE_DIR, f"injuries_{season}.csv")
    text = None
    if use_cache and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
    if text is None:
        resp = httpx.get(INJURIES_URL.format(season=season), follow_redirects=True, timeout=60)
        resp.raise_for_status()
        text = resp.text
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    out = {}
    for row in csv.DictReader(StringIO(text)):
        if (row.get("game_type") or "").upper() != "REG" or not row.get("week"):
            continue
        out[(int(row["week"]), row["gsis_id"])] = {
            "status": _STATUS.get((row.get("report_status") or "").strip().lower(), ""),
            "practice": _PRACTICE.get((row.get("practice_status") or "").strip().lower(), ""),
        }
    return out


def bucket_of(report: dict | None) -> str:
    if not report:
        return "none"
    return f"{report['status'] or '-'}/{report['practice'] or '-'}"


def _ratio(rows: list[dict], num: str, den: str) -> float:
    d = sum(s[den] for s in rows)
    return sum(s[num] for s in rows) / d if d else float("nan")


def implied(rows: list[dict], ref: float, key: str = "model",
            seed: int = 7) -> tuple[float, float, float]:
    """``(multiplier, lo, hi)``: the bucket's actual/`key` over the reference
    ratio, with a 95% bootstrap interval (rows resampled)."""
    point = _ratio(rows, "actual", key) / ref
    rng = random.Random(seed)
    boots = sorted(_ratio(rng.choices(rows, k=len(rows)), "actual", key) / ref
                   for _ in range(BOOTSTRAP))
    return point, boots[int(0.025 * BOOTSTRAP)], boots[int(0.975 * BOOTSTRAP) - 1]


def _bucket_args(practice: str) -> tuple[str | None, str | None]:
    """``(practice_status, pattern)`` the live code reads for a bucket's final
    practice day: a final-day DNP is the DNP week (two or more days); "all" /
    "-" have no practice line."""
    if practice == "DNP":
        return "DNP", "DNP-DNP"
    if practice in ("FP", "LP"):
        return practice, None
    return None, None


def _live_total(bucket: str) -> float | None:
    """The live blend's multiplier for the bucket against a healthy
    projection: model share x `practice_adjusted_mult` (`_injury_mult`
    without a practice line), Sleeper's x `practice_blend_mult` x Sleeper's
    own shading of a questionable player (``SLEEPER_QUESTIONABLE_PRICED``).
    Doubtful: the blend's cap (`sleeper_projections.blend`)."""
    from nfl_mcp.sleeper_projections import BLEND_MODEL_WEIGHT as w
    status, practice = bucket.split("/") if "/" in bucket else ("", "")
    if status == "Q":
        args = _bucket_args(practice)
        model = practice_adjusted_mult("Questionable", *args)
        sleeper = practice_blend_mult("Questionable", *args) * SLEEPER_QUESTIONABLE_PRICED
        return w * model + (1 - w) * sleeper
    if status == "D":
        return _injury_mult("Doubtful")
    return None


def _live_active(bucket: str) -> float | None:
    """The live blend's multiplier for a player of the bucket confirmed
    active at inactives (`confirmed_active_mult` on our share,
    `confirmed_active_blend_mult` x Sleeper's shading on Sleeper's)."""
    from nfl_mcp.sleeper_projections import BLEND_MODEL_WEIGHT as w
    status, practice = bucket.split("/") if "/" in bucket else ("", "")
    tag = {"Q": "Questionable", "D": "Doubtful"}.get(status)
    if not tag:
        return None
    args = _bucket_args(practice)
    sleeper = confirmed_active_blend_mult(tag, *args) * SLEEPER_QUESTIONABLE_PRICED
    return w * confirmed_active_mult(tag, *args) + (1 - w) * sleeper


def report_practice(samples: list[dict], injuries: dict[int, dict]) -> None:
    rows = [s for s in samples if s["position"] in ("QB", "RB", "WR", "TE") and s["model"] > 0]
    by: dict[str, list[dict]] = defaultdict(list)
    for s in rows:
        b = bucket_of(injuries[s["season"]].get((s["week"], s["player_id"])))
        by[b].append(s)
        if b.startswith(("Q/", "D/")):
            by[b.split("/")[0] + "/all"].append(s)
    ref = _ratio(by["none"], "actual", "model")
    ref_sl = _ratio([s for s in by["none"] if s["sleeper"] is not None], "actual", "sleeper")
    print(f"Reference (not on the report): n={len(by['none'])} actual/model {ref:.3f} "
          f"actual/Sleeper {ref_sl:.3f}")
    print("bucket      n     played  implied mult vs model [95% CI]   live model / blend"
          "   | Sleeper archived: Sleeper/model, zero-projected")
    order = ["Q/FP", "Q/LP", "Q/DNP", "Q/all", "D/LP", "D/DNP", "D/all", "-/FP", "-/LP",
             "-/DNP", "O/DNP"]
    for b in order + sorted(set(by) - set(order) - {"none"}):
        sub = by.get(b) or []
        if len(sub) < 10:
            continue
        m, lo, hi = implied(sub, ref)
        played = sum(s["played"] for s in sub) / len(sub)
        live_model = None
        st, pr = b.split("/")
        if st == "Q":
            live_model = practice_adjusted_mult("Questionable", *_bucket_args(pr))
        elif st == "D":
            live_model = _injury_mult("Doubtful")
        total = _live_total(b)
        sl = [s for s in sub if s["sleeper"] is not None]
        zero = sum(1 for s in sl if not s["sleeper"]) / len(sl) if sl else float("nan")
        slm = _ratio(sl, "sleeper", "model") / _ratio(
            [s for s in by["none"] if s["sleeper"] is not None], "sleeper", "model") if sl else 0
        print(f"  {b:8s} {len(sub):5d}  {played:6.1%}  {m:.3f} [{lo:.3f}, {hi:.3f}]   "
              f"{'' if live_model is None else f'{live_model:.2f}':>5s} / "
              f"{'' if total is None else f'{total:.3f}':>5s}   | {slm:.3f}, {zero:.1%}")
    # MAE view: the multiplier on a healthy projection (model x the
    # reference ratio) that minimises the bucket's absolute error. With a
    # bimodal outcome (sat: 0, played: about full) MAE prefers the median, so
    # this is reported next to the expected-value multiplier above.
    print("  MAE-optimal multiplier on a healthy projection (and MAE at the live blend total"
          " / at the implied mean):")
    grid = [x / 20 for x in range(25)]
    for b in ("Q/FP", "Q/LP", "Q/DNP", "Q/all", "D/all", "-/DNP"):
        sub = by.get(b) or []
        if len(sub) < 10:
            continue
        act = [s["actual"] for s in sub]

        def err(m, sub=sub, act=act):
            return mae([s["model"] * ref * m for s in sub], act)
        best = min(grid, key=err)
        total = _live_total(b)
        mean_m = implied(sub, ref)[0]
        print(f"    {b:8s} best {best:.2f} MAE {err(best):.3f} | live "
              + (f"{total:.3f}: {err(total):.3f}" if total is not None else "-")
              + f" | mean {mean_m:.3f}: {err(mean_m):.3f} | 1.0: {err(1.0):.3f}")
    # The played rows only: what a questionable player who suits up scores.
    # The confirmed-active price (`projections.CONFIRMED_ACTIVE_REALISED`).
    print("  given he played (the in-game cost; live confirmed-active model / blend):")
    for b in ("Q/FP", "Q/LP", "Q/DNP", "Q/all", "D/all"):
        sub = [s for s in by.get(b) or [] if s["played"]]
        ctrl = [s for s in by["none"] if s["played"]]
        if len(sub) >= 10:
            m, lo, hi = implied(sub, _ratio(ctrl, "actual", "model"))
            st, pr = b.split("/")
            tag = {"Q": "Questionable", "D": "Doubtful"}[st]
            print(f"    {b:8s} n={len(sub):4d} {m:.3f} [{lo:.3f}, {hi:.3f}]   "
                  f"{confirmed_active_mult(tag, *_bucket_args(pr)):.2f} / {_live_active(b):.3f}")


def _starters(records: list[dict]) -> dict[tuple[str, int], str]:
    """``(team, week) -> the QB with the most pass attempts over the team's
    previous six games`` (the starter a coupling read keys on)."""
    att: dict[str, dict[int, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for r in records:
        if r["position"] == "QB":
            att[r["team"]][r["week"]][r["player_id"]] = r.get("attempts", 0.0)
    out = {}
    for team, weeks in att.items():
        for week in range(2, 19):
            prev = sorted(w for w in weeks if w < week)[-6:]
            tally: dict[str, float] = defaultdict(float)
            for w in prev:
                for pid, a in weeks[w].items():
                    tally[pid] += a
            if tally:
                out[(team, week)] = max(tally, key=tally.get)
    return out


def report_qb(seasons: list[int], injuries: dict[int, dict]) -> None:
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for season in seasons:
        records = load_season(season)
        games = load_games(season)
        team_weeks: dict[str, list[int]] = defaultdict(list)
        for (s_, w, t) in games:
            if s_ == season:
                team_weeks[t].append(w)
        for (team, week), qb in _starters(records).items():
            rep = injuries[season].get((week, qb))
            started_by = (games.get((season, week, team)) or {}).get("qb_id")
            if not rep or not started_by or (not rep["status"] and not rep["practice"]):
                continue
            # The case the coupling prices: the starter of the team's last
            # game is on this week's report (not one long gone, still listed).
            prev = max((w for w in team_weeks[team] if w < week), default=None)
            if prev is None or (games.get((season, prev, team)) or {}).get("qb_id") != qb:
                continue
            b = bucket_of(rep)
            for key in {b, rep["status"] + "/all" if rep["status"] else b}:
                tally[key][0] += started_by != qb
                tally[key][1] += 1
    print("\nStarting QB (started the team's last game) on the report: share who did not "
          "start (the coupling weight)")
    live = {"Q/DNP": qb_coupling.QUESTIONABLE_DNP_WEIGHT,
            "Q/LP": qb_coupling.QUESTIONABLE_LIMITED_WEIGHT,
            "Q/FP": qb_coupling.QUESTIONABLE_FULL_WEIGHT,
            "Q/all": qb_coupling.QUESTIONABLE_WEIGHT, "D/all": qb_coupling.DOUBTFUL_WEIGHT,
            "O/all": qb_coupling.OUT_WEIGHT}
    for b, (sat, n) in sorted(tally.items()):
        lo, hi = _wilson(sat, n)
        lv = f" live {live[b]:.2f}" if b in live else ""
        print(f"  {b:8s} n={n:4d} did not start {sat / n:6.1%} [{lo:.1%}, {hi:.1%}]{lv}")


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seasons", type=int, nargs="+", default=[2023, 2024, 2025])
    args = ap.parse_args()
    injuries = {s: load_injuries(s) for s in args.seasons}
    samples = build_samples(args.seasons, include_dnp=True)
    report_practice(samples, injuries)
    report_qb(args.seasons, injuries)


if __name__ == "__main__":
    main()
