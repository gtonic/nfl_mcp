"""
Live accuracy report: the weekly accuracy loop's numbers, as tables.

QUESTION
    How accurate were the projections this server actually served (the last
    pre-kickoff number logged by the briefing / project_players / league
    changes), per position, per projection source and per signal -- and does
    each signal's adjustment pull its rows toward the truth?

METHOD
    Reads ``projection_accuracy`` (schema v18), which
    ``nfl_mcp.projection_accuracy.grade_week`` fills once a week is final:
    the projection against the points scored in the scoring it was made in
    (Sleeper's ``players_points`` for a league, else the stat line priced in
    it) and a neutral half-PPR actual. ``--grade`` first grades every finished
    logged week that needs it (NETWORK: Sleeper stats, leagues, matchups).
    Errors are projected − actual; rows projected 0 that scored 0 (byes,
    ruled-out players) are left out. Unlike the nflverse backtests this is the
    real served number with every live input (injury report, practice, news,
    Sleeper), on a small sample: two leagues' rosters and opponents a week.

RUN
    python -m evals.backtest.accuracy_report --db nfl_data.db --season 2026
    python -m evals.backtest.accuracy_report --db /tmp/copy.db --grade --position WR
"""

from __future__ import annotations

import argparse
import asyncio

from nfl_mcp.database import NFLDatabase
from nfl_mcp.lineup_slots import normalize_position
from nfl_mcp.projection_accuracy import accuracy_report, refresh_accuracy


def _cell(s: dict) -> str:
    if not s or not s.get("n"):
        return "n=0"
    return f"n={s['n']:<4} MAE {s['mae']:5.2f}  bias {s['bias']:+5.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="nfl_data.db")
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--weeks", type=int, nargs="*", default=None)
    ap.add_argument("--position", default=None)
    ap.add_argument("--league-id", default=None)
    ap.add_argument("--grade", action="store_true",
                    help="grade finished, logged weeks first (network)")
    args = ap.parse_args()

    db = NFLDatabase(args.db)
    season = args.season
    if args.grade:
        out = asyncio.run(refresh_accuracy(db, season))
        season = season or out["season"]
        print(f"graded weeks {out['weeks']}: {out['written']} rows")
    if season is None:
        from nfl_mcp.week_context import current_season_week
        season = asyncio.run(current_season_week(db))["season"]
    rows = db.get_projection_accuracy(
        season, weeks=args.weeks,
        position=normalize_position(args.position) if args.position else None,
        league_id=args.league_id)
    rep = accuracy_report(rows)
    print(f"season {season}: {len(rows)} graded rows "
          f"({rep['excluded_zero_zero']} zero/zero excluded, "
          f"{rep['rows_with_signals']} with signals)")
    print(f"  overall              {_cell(rep['overall'])}")
    print("by position:")
    for pos, s in rep["by_position"].items():
        print(f"  {pos:20s} {_cell(s)}")
    print("by projection source:")
    for src, s in rep["by_projection_source"].items():
        print(f"  {src:20s} {_cell(s)}")
    if rep.get("components"):
        print("same rows, model vs Sleeper vs blend:")
        for k in ("model", "sleeper", "blend"):
            print(f"  {k:20s} {_cell(rep['components'][k])}")
    print("trend:")
    for t in rep["trend"]:
        print(f"  week {t['week']:<15d} {_cell(t)}")
    if rep.get("by_signal"):
        print("by signal (with | without | bias gap):")
        for name, s in rep["by_signal"].items():
            flag = " (small)" if s["small_sample"] else ""
            print(f"  {name:20s} {_cell(s['with'])} | {_cell(s['without'])} | "
                  f"{s['bias_gap']:+5.2f}{flag}")
    print("interpretation:")
    for line in rep["interpretation"]:
        print(f"  - {line}")


if __name__ == "__main__":
    main()
