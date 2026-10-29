"""
Weekly calibration fit (Eval Layer A): is the blend biased by position x tier?

QUESTION
    The weekly projection is right on average, but is it right for each kind
    of player? Top-12 defenses, mid-tier tight ends and depth backs can each
    run high or low while the overall bias is near zero. ``nfl_mcp.calibration``
    corrects that with one shrunk multiplier per (position, tier); this script
    fits the table, checks it on held-out weeks and writes it.

DATA (all from the season being played; no history needed)
    * Population **S**: Sleeper's projection for every player it projects
      (``api.sleeper.app/projections``, cached under ``.cache/sleeper/``),
      priced in each league's scoring (``sleeper_projections.price_stats``),
      against his real stat line from the server's ``player_week_stats``
      priced the same way (``projection_accuracy.price_actual``). A projected
      player with no stat line sat and scored 0. Sleeper is 75% of the blend,
      and this population has every rank, not only the rostered ones.
    * Population **L**: the served blend itself, the last pre-kickoff row per
      league and player (``projection_accuracy``) -- smaller (two leagues'
      rosters) but exactly what the tools said. Rows logged after the
      calibration went live are divided back by their ``calibration_mult``.

    Tier = rank at the position in Sleeper's week in that league's scoring
    (``calibration.tier_for``: top 12 / 24 = T1, next = T2, rest = T3).

METHOD
    Per cell, ratio = sum(actual) / sum(projected) on the fit weeks, shrunk
    ``1 + (ratio - 1) * n / (n + K)`` and capped (``calibration.CAP_*``).
    Held out: fit on all but the last ``--holdout`` weeks, grade on those (MAE
    and bias, before / after, on S and on L). A cell is **kept** only when it
    has ``MIN_N`` fit rows and its correction lowers the held-out MAE on S
    (the larger population); others stay at 1.0. ``--write`` refits the kept
    cells on every week and rewrites the table block of
    ``nfl_mcp/calibration.py``.

RUN
    python -m evals.backtest.weekly_calibration --db nfl_data.db --season 2026
    python -m evals.backtest.weekly_calibration --db nfl_data.db --season 2026 --write
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

import httpx

from nfl_mcp import calibration as cal
from nfl_mcp.projection_accuracy import price_actual
from nfl_mcp.scoring import ScoringModel
from nfl_mcp.sleeper_projections import lookup, price_stats

from .data import _CACHE_DIR
from .metrics import bias, mae
from .sleeper_blend import load_sleeper_week

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
MIN_PROJECTED = 2.0  # rows Sleeper projects below this are not fit on (deep backups)
_LEAGUE_URL = "https://api.sleeper.app/v1/league/{}"
_TABLE_RE = re.compile(r"(# --- fitted table.*?\n)(.*?)(# --- end fitted table ---)", re.S)


def league_models(db: sqlite3.Connection, season: int,
                  league_ids: list[str] | None = None) -> dict[str, ScoringModel]:
    """``{league_id: ScoringModel}`` for the leagues graded in the database
    (or the given ones); the scoring is fetched once and cached."""
    if not league_ids:
        league_ids = [r[0] for r in db.execute(
            "SELECT DISTINCT league_id FROM projection_accuracy WHERE season = ? "
            "AND league_id IS NOT NULL", (season,))]
    out = {}
    os.makedirs(_CACHE_DIR, exist_ok=True)
    for lid in league_ids:
        path = os.path.join(_CACHE_DIR, f"league_{lid}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                league = json.load(f)
        else:
            resp = httpx.get(_LEAGUE_URL.format(lid), timeout=30)
            resp.raise_for_status()
            league = resp.json()
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"name": league.get("name"),
                           "scoring_settings": league.get("scoring_settings")}, f)
        out[lid] = ScoringModel.from_settings(league.get("scoring_settings"), league.get("name"))
    return out


def graded_weeks(db: sqlite3.Connection, season: int) -> list[int]:
    return [int(r[0]) for r in db.execute(
        "SELECT DISTINCT week FROM player_week_stats WHERE season = ? ORDER BY week", (season,))]


def _actuals(db: sqlite3.Connection, season: int, week: int) -> dict[str, dict]:
    return {pid: json.loads(raw) for pid, raw in db.execute(
        "SELECT player_id, raw FROM player_week_stats WHERE season = ? AND week = ?",
        (season, week)) if raw}


def population(db, season: int, weeks: list[int], models: dict[str, ScoringModel],
               indexes: dict[int, dict]) -> list[dict]:
    """Population S (see module doc)."""
    rows = []
    for week in weeks:
        index, actual = indexes[week], _actuals(db, season, week)
        for lid, model in models.items():
            ranks = cal.position_ranks(index, model)
            seen = set()
            for row in list(index["by_id"].values()) + list(index["by_def"].values()):
                pos = cal._position_of(row)
                if pos not in POSITIONS:
                    continue
                key = cal._key(pos, row)
                if (pos, key) in seen:
                    continue
                seen.add((pos, key))
                projected = price_stats(row["stats"], model, calibrated=False)
                if projected < MIN_PROJECTED:
                    continue
                stats = actual.get(str(row.get("player_id"))) or (actual.get(key) if pos == "DEF" else None)
                rank = ranks.get((pos, key))
                out = {"week": week, "league": lid, "pos": pos, "name": row.get("name") or key,
                       "rank": rank, "tier": cal.tier_for(pos, rank), "raw_projected": projected,
                       "projected": projected, "actual": price_actual(stats, model)}
                if pos == "DEF":  # kept for the component fit
                    out.update({"model": model, "stats": row["stats"], "actual_stats": stats or {}})
                rows.append(out)
    return rows


def fit_components(rows: list[dict], k: float = cal.SHRINK_K) -> dict[str, dict]:
    """``{event: {mult, raw, n}}``: real over projected counts of each DEF
    scoring event (``calibration.DEF_EVENTS``), shrunk like the tier cells."""
    rows = [r for r in rows if r["pos"] == "DEF"]
    out = {}
    if not rows:
        return out
    seen = {(r["week"], r["name"]) for r in rows}  # one count per team-week, not per league
    once = {(r["week"], r["name"]): r for r in rows}
    n = len(seen)
    for event in cal.DEF_EVENTS:
        proj = sum(float(r["stats"].get(event) or 0) for r in once.values())
        act = sum(float(r["actual_stats"].get(event) or 0) for r in once.values())
        if proj <= 0:
            continue
        raw = act / proj
        mult = max(cal.DEF_CAP_LOW, min(cal.DEF_CAP_HIGH, 1.0 + (raw - 1.0) * n / (n + k)))
        out[event] = {"mult": round(mult, 3), "raw": round(raw, 3), "n": n}
    return out


def with_components(rows: list[dict], comps: dict) -> list[dict]:
    """Rows with each DEF number scaled as its Sleeper line moves under
    `comps` (others, and DEF rows served with the components, as they are)."""
    if not comps:
        return [{**r, "projected": r["raw_projected"]} for r in rows]
    table = {"def_components": comps}
    out = []
    for r in rows:
        projected = r["raw_projected"]
        if r["pos"] == "DEF" and r.get("stats") and not r.get("components_served"):
            raw = price_stats(r["stats"], r["model"], calibrated=False)
            new = price_stats(r["stats"], r["model"], table=table)
            projected *= new / raw if raw > 0 else 1.0
        out.append({**r, "projected": projected})
    return out


def logged(db, season: int, weeks: list[int], models: dict[str, ScoringModel],
           indexes: dict[int, dict]) -> list[dict]:
    """Population L (see module doc)."""
    rows = []
    marks = ",".join("?" * len(weeks))
    for r in db.execute(
            f"SELECT week, league_id, player_id, player_name, position, team, projected, actual, signals "
            f"FROM projection_accuracy WHERE season = ? AND week IN ({marks}) AND projected > 0",
            (season, *weeks)):
        week, lid, pid, name, pos, team, projected, actual, signals = r
        model = models.get(lid)
        pos = "DEF" if (pos or "").upper() == "DST" else (pos or "").upper()
        if model is None or pos not in POSITIONS:
            continue
        sig = json.loads(signals) if signals else {}
        projected = float(projected) / float(sig.get("calibration_mult") or 1.0)
        rank = cal.rank_of(indexes[week], model, player_id=pid, name=name, team=team, position=pos)
        out = {"week": week, "league": lid, "pos": pos, "name": name, "rank": rank,
               "tier": cal.tier_for(pos, rank), "raw_projected": projected,
               "projected": projected, "actual": float(actual)}
        if pos == "DEF" and (line := lookup(indexes[week], team=team or name, position="DEF")):
            out.update({"model": model, "stats": line["stats"],
                        "components_served": bool(sig.get("def_components"))})
        rows.append(out)
    return rows


def fit(rows: list[dict], k: float = cal.SHRINK_K) -> dict[str, dict[str, dict]]:
    """``{pos: {tier: {mult, raw, n}}}`` from rows (every cell with a row)."""
    sums: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
    for r in rows:
        if r["tier"] is None:
            continue
        s = sums[(r["pos"], r["tier"])]
        s[0] += r["actual"]
        s[1] += r["projected"]
        s[2] += 1
    cells: dict[str, dict[str, dict]] = defaultdict(dict)
    for (pos, tier), (act, proj, n) in sums.items():
        raw = act / proj if proj > 0 else 1.0
        shrunk = 1.0 + (raw - 1.0) * n / (n + k)
        mult = max(cal.CAP_LOW, min(cal.CAP_HIGH, shrunk))
        cells[pos][tier] = {"mult": round(mult, 3), "raw": round(raw, 3), "n": int(n)}
    return dict(cells)


def apply(rows: list[dict], cells: dict) -> list[float]:
    table = {"cells": cells}
    return [cal.calibrate(r["projected"], r["pos"], r["rank"], table)[0] for r in rows]



def _metrics(rows: list[dict], cells: dict | None,
             comps: dict | None = None) -> tuple[int, float, float]:
    """``(n, MAE, bias)`` of the rows' projections under the DEF components
    `comps` and the tier `cells` (both None: the raw projection)."""
    rows = with_components(rows, comps or {})
    pred = apply(rows, cells) if cells else [r["projected"] for r in rows]
    act = [r["actual"] for r in rows]
    return len(rows), mae(pred, act), bias(pred, act)


def keep_cells(fitted: dict, holdout_rows: list[dict], min_n: int = cal.MIN_N) -> dict:
    """The fitted cells with enough rows whose correction lowered the held-out
    MAE. `holdout_rows` carry the projection the cells are applied to."""
    kept: dict[str, dict] = {}
    for pos, tiers in fitted.items():
        for tier, cell in tiers.items():
            if cell["n"] < min_n or cell["mult"] == 1.0:
                continue
            sub = [r for r in holdout_rows if r["pos"] == pos and r["tier"] == tier]
            if not sub:
                continue
            before = mae([r["projected"] for r in sub], [r["actual"] for r in sub])
            after = mae(apply(sub, {pos: {tier: cell}}), [r["actual"] for r in sub])
            if after < before:
                kept.setdefault(pos, {})[tier] = cell
    return kept


def keep_components(comps: dict, holdout_rows: list[dict]) -> dict:
    """`comps` when they lower the held-out DEF MAE, else {}."""
    defs = [r for r in holdout_rows if r["pos"] == "DEF"]
    if not comps or not defs:
        return {}
    _, before, _ = _metrics(defs, None)
    _, after, _ = _metrics(defs, None, comps)
    return comps if after < before else {}


def table_lines(rows_by: dict[str, list[dict]], cells: dict,
                comps: dict | None = None) -> list[str]:
    """Before/after lines: overall, per position, per position x tier."""
    out = [f"{'pop':<4} {'group':<10} {'n':>5} {'MAE before':>10} {'after':>7} "
           f"{'bias before':>11} {'after':>7}"]
    for label, rows in rows_by.items():
        groups: dict[str, list[dict]] = {"all": rows}
        for r in rows:
            groups.setdefault(r["pos"], []).append(r)
            groups.setdefault(f"{r['pos']} {r['tier'] or '-'}", []).append(r)
        for g in ["all", *sorted(k for k in groups if k != "all")]:
            sub = groups[g]
            n, m0, b0 = _metrics(sub, None)
            _, m1, b1 = _metrics(sub, cells, comps)
            out.append(f"{label:<4} {g:<10} {n:>5} {m0:>10.2f} {m1:>7.2f} {b0:>11.2f} {b1:>7.2f}")
    return out


def write_table(cells: dict, season: int, weeks: list[int], n_rows: int,
                path: str | None = None, comps: dict | None = None) -> None:
    """Rewrite the fitted-table block of ``nfl_mcp/calibration.py``."""
    path = path or cal.__file__
    with open(path, encoding="utf-8") as f:
        text = f.read()
    today = datetime.now(UTC).date().isoformat()
    ordered = {pos: {t: cells[pos][t] for t in cal.TIERS if t in cells[pos]}
               for pos in POSITIONS if pos in cells}
    body = (f"# Fitted {today} on {season} weeks {weeks[0]}-{weeks[-1]}: "
            f"{n_rows} Sleeper-projected player-weeks in the graded leagues' scoring;\n"
            f"# held-out MAE gate on the last weeks; K={cal.SHRINK_K}, cap "
            f"[{cal.CAP_LOW}, {cal.CAP_HIGH}], min n {cal.MIN_N}. raw = unshrunk ratio;\n"
            "# def_components n = team-weeks.\n"
            "CALIBRATION: dict[str, Any] = {\n"
            f"    \"fitted\": \"{today}\",\n"
            f"    \"season\": {season},\n"
            f"    \"weeks\": {weeks},\n"
            "    \"cells\": {\n")
    for pos, tiers in ordered.items():
        body += f"        \"{pos}\": {{\n"
        for t, c in tiers.items():
            body += (f"            \"{t}\": {{\"mult\": {c['mult']}, \"raw\": {c['raw']}, "
                     f"\"n\": {c['n']}}},\n")
        body += "        },\n"
    body += "    },\n    \"def_components\": {\n"
    for event, c in (comps or {}).items():
        body += f"        \"{event}\": {{\"mult\": {c['mult']}, \"raw\": {c['raw']}, \"n\": {c['n']}}},\n"
    body += "    },\n}\n"
    new, count = _TABLE_RE.subn(lambda m: m.group(1) + body + m.group(3), text)
    if count != 1:
        raise RuntimeError("fitted-table markers not found in " + path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("NFL_MCP_DB_PATH", "nfl_data.db"))
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--weeks", type=int, nargs="*", help="default: every week with stat lines")
    ap.add_argument("--holdout", type=int, default=2, help="last N weeks held out")
    ap.add_argument("--league", action="append", help="league id (default: graded leagues)")
    ap.add_argument("--k", type=float, default=cal.SHRINK_K)
    ap.add_argument("--write", action="store_true", help="rewrite nfl_mcp/calibration.py")
    args = ap.parse_args()

    # Fit on raw numbers: the shipped table must not leak into ranks or prices.
    cal.CALIBRATION = {"cells": {}, "def_components": {}}
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    weeks = args.weeks or graded_weeks(db, args.season)
    models = league_models(db, args.season, args.league)
    indexes = {w: load_sleeper_week(args.season, w) for w in weeks}
    fit_weeks, hold_weeks = weeks[:-args.holdout], weeks[-args.holdout:]
    pop = population(db, args.season, weeks, models, indexes)
    log = logged(db, args.season, weeks, models, indexes)
    fit_rows = [r for r in pop if r["week"] in fit_weeks]
    hold = {"S": [r for r in pop if r["week"] in hold_weeks],
            "L": [r for r in log if r["week"] in hold_weeks]}
    print(f"Season {args.season}: fit weeks {fit_weeks}, held out {hold_weeks}; "
          f"S {len(pop)} rows, L {len(log)} rows; leagues {list(models)}; K={args.k}")

    # 1) DEF scoring events on Sleeper's line.
    comps_fit = fit_components(fit_rows, args.k)
    comps = keep_components(comps_fit, hold["S"])
    print("\nDEF events, fit weeks (mult / raw):", ", ".join(
        f"{e} {c['mult']:.2f}/{c['raw']:.2f}" for e, c in comps_fit.items()),
        "->", "kept" if comps else "not kept (no held-out gain)")
    # 2) Tier cells on top of them.
    fit_c = with_components(fit_rows, comps)
    hold_c = {k: with_components(v, comps) for k, v in hold.items()}
    for k in (50, 100, 200, 400, 800):
        cells = keep_cells(fit(fit_c, k), hold_c["S"])
        _, ms, bs = _metrics(hold["S"], cells, comps)
        _, ml, bl = _metrics(hold["L"], cells, comps)
        print(f"  K={k:<4} kept {sum(len(v) for v in cells.values()):>2} cells: "
              f"held-out S MAE {ms:.3f} bias {bs:+.2f} | L MAE {ml:.3f} bias {bl:+.2f}")
    fitted = fit(fit_c, args.k)
    kept = keep_cells(fitted, hold_c["S"])
    print("\nFit on weeks", fit_weeks, "(mult / raw / n; * = kept by the held-out gate)")
    for pos in POSITIONS:
        cells = fitted.get(pos) or {}
        print(f"  {pos:<4}" + "  ".join(
            f"{t}: {c['mult']:.3f}/{c['raw']:.3f}/{c['n']}{'*' if t in kept.get(pos, {}) else ' '}"
            for t, c in sorted(cells.items())))
    print("\nHeld-out weeks", hold_weeks, "(before = uncalibrated, after = DEF events + kept cells; "
          "bias = projected - actual):")
    print("\n".join(table_lines(hold, kept, comps)))

    # 3) Refit what passed on every week.
    final_comps = ({e: c for e, c in fit_components(pop, args.k).items() if e in comps}
                   if comps else {})
    final = fit(with_components(pop, final_comps), args.k)
    final_kept = {pos: {t: final[pos][t] for t in tiers} for pos, tiers in kept.items()}
    print("\nRefit on all weeks: def_components", json.dumps(final_comps),
          "\n  cells", json.dumps(final_kept))
    if args.write:
        write_table(final_kept, args.season, weeks, len(pop), comps=final_comps)
        print("wrote", cal.__file__)


if __name__ == "__main__":
    main()
