"""
Practice and news signal backtest (Eval Layer A), from the signal history.

QUESTION
    Two hand-set weight tables move the weekly projection and were never
    measured, because only the latest practice row / news blurb per player
    used to survive:

    - ``projections.PRACTICE_BLEND_MULT``: a questionable player's practice
      week (one DNP so far 0.90, DNP every day 0.75, LP after an FP 0.95).
    - ``news_signals.EFFECTS``: the role flags read from the blurbs
      (benched 0.85, committee 0.93, limited_snaps 0.90, ...).

    Do those players really score that fraction of their usual output?

DATA
    The append-only tables of schema v17 in the server's database
    (``NFL_MCP_DB_PATH``, default ``nfl_data.db``): ``practice_report_history``
    (every distinct player/day/status/source) and ``injury_news_history``
    (every distinct blurb with its ``date_reported``), plus ``schedule_games``
    for kickoffs. Collection starts with the v17 upgrade (2026 week 5); wait
    for at least four weeks -- a few hundred questionable player-weeks --
    before reading a bucket. Truth is nflverse's weekly PPR (``data.load_season``),
    published a day or two after each week.

METHOD (leak-free)
    For every (week, team) the signals are rebuilt as the server saw them at
    kickoff: practice rows *recorded* before kickoff (the last status per
    report day), the newest blurb per player recorded before kickoff, read
    with the live ``news_signals.build_index`` at kickoff time. Each player is
    bucketed by the live functions (``practice_blend_mult``, ``adjustment``)
    and scored by ``ratio = actual PPR / trailing PPR per game`` (prior
    weeks, >= ``MIN_TRAILING`` games; a player with no game record that week
    scores 0 -- he sat). A bucket's mean ratio against the mean ratio of the
    unflagged questionable players is the multiplier the data implies; compare
    it with the live constant.

RUN
    python -m evals.backtest.signal_history --db nfl_data.db --season 2026
    python -m evals.backtest.signal_history --db nfl_data.db --season 2026 --counts
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from statistics import mean

from nfl_mcp.game_clock import parse_kickoff
from nfl_mcp.news_signals import adjustment, build_index, signals_for
from nfl_mcp.opportunity_tools import norm_name
from nfl_mcp.projections import practice_blend_mult

# Trailing games a player needs before his ratio is scored.
MIN_TRAILING = 2
# A bucket smaller than this is printed but flagged as noise.
MIN_BUCKET = 30


def kickoffs(conn: sqlite3.Connection, season: int) -> dict[tuple[int, str], datetime]:
    """``(week, team) -> kickoff`` from the cached schedule."""
    out = {}
    for week, team, ko in conn.execute(
            "SELECT week, team, kickoff FROM schedule_games WHERE season=?", (season,)):
        when = parse_kickoff(ko)
        if when:
            out[(int(week), team)] = when
    return out


def practice_at_kickoff(rows: list[dict], kicks: dict[tuple[int, str], datetime]) -> dict:
    """``(week, team, name_key) -> {pattern, latest, game_status}`` as known at kickoff.

    The last row *recorded* before kickoff wins per report day, NFL.com over
    the news notes (as ``upsert_practice_status`` ranks them).
    """
    rank = {"nfl.com": 2, "espn_news": 1}
    days: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        ko = kicks.get((r["week"], r["team"]))
        if ko is None or parse_kickoff(r["recorded_at"]) >= ko:
            continue
        key = (r["week"], r["team"], r["name_key"])
        best = days[key].get(r["date"])
        order = (rank.get(r["source"], 0), r["recorded_at"])
        if best is None or order >= (rank.get(best["source"], 0), best["recorded_at"]):
            days[key][r["date"]] = r
    out = {}
    for key, by_day in days.items():
        line = [by_day[d] for d in sorted(by_day)]
        out[key] = {"pattern": "-".join(r["status"] for r in line), "latest": line[-1]["status"],
                    "game_status": next((r["game_status"] for r in reversed(line)
                                         if r.get("game_status")), None)}
    return out


def news_at_kickoff(rows: list[dict], kicks: dict[tuple[int, str], datetime]) -> dict:
    """``(week, team) -> news index`` from the newest blurb per player
    recorded before that team's kickoff, read at kickoff time."""
    out = {}
    for (week, team), ko in kicks.items():
        newest: dict[str, dict] = {}
        for r in rows:
            if r["team_id"] != team or parse_kickoff(r["recorded_at"]) >= ko:
                continue
            if r["recorded_at"] >= newest.get(r["player_id"], {}).get("recorded_at", ""):
                newest[r["player_id"]] = r
        if newest:
            out[(week, team)] = build_index(
                [{**r, "injury_description": r["text"]} for r in newest.values()], now=ko)
    return out


def ratios(records: list[dict]) -> dict[tuple[int, str, str], float]:
    """``(week, team, name_key) -> actual / trailing PPR per game`` for every
    player-week with ``MIN_TRAILING`` prior games, and 0.0 for the week after
    such a game run that has no record (he did not play)."""
    by_player: dict[tuple[str, str], dict[int, float]] = defaultdict(dict)
    for r in records:
        by_player[(norm_name(r["player"]), r["team"])][r["week"]] = r["ppr"]
    out = {}
    for (name, team), weeks in by_player.items():
        last = max(weeks)
        for week in range(min(weeks) + MIN_TRAILING, last + 1):
            prior = [p for w, p in weeks.items() if w < week]
            base = mean(prior) if len(prior) >= MIN_TRAILING else 0.0
            if base > 0:
                out[(week, team, name)] = weeks.get(week, 0.0) / base
    return out


def practice_buckets(practice: dict, truth: dict) -> dict[str, list[float]]:
    """Ratios of questionable players grouped by the live practice multiplier."""
    out: dict[str, list[float]] = defaultdict(list)
    for key, p in practice.items():
        if (p["game_status"] or "").lower() != "questionable" or key not in truth:
            continue
        mult = practice_blend_mult("Questionable", p["latest"], p["pattern"])
        out[f"mult={mult:.2f}"].append(truth[key])
    return out


def news_buckets(news: dict, truth: dict) -> dict[str, list[float]]:
    """Ratios grouped by applied news flag (``none`` for no flag)."""
    out: dict[str, list[float]] = defaultdict(list)
    for (week, team, name), ratio in truth.items():
        index = news.get((week, team))
        if index is None:
            continue
        applied = adjustment(signals_for(index, name, team))["applied"] or ["none"]
        for flag in applied:
            out[flag].append(ratio)
    return out


def _report(title: str, buckets: dict[str, list[float]], reference: str) -> None:
    print(f"\n{title}")
    ref = mean(buckets[reference]) if buckets.get(reference) else None
    for name in sorted(buckets):
        vals = buckets[name]
        implied = f"{mean(vals) / ref:.2f}" if ref else "n/a"
        noise = "" if len(vals) >= MIN_BUCKET else "  (too few)"
        print(f"  {name:<18} n={len(vals):<5} mean ratio={mean(vals):.2f} "
              f"implied mult vs {reference}={implied}{noise}")


def _rows(conn: sqlite3.Connection, sql: str, *params) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.getenv("NFL_MCP_DB_PATH", "nfl_data.db"))
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--counts", action="store_true", help="only show what has been collected")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    practice_rows = _rows(conn, "SELECT * FROM practice_report_history WHERE season=?", args.season)
    news_rows = _rows(conn, "SELECT * FROM injury_news_history WHERE recorded_at >= ?",
                      f"{args.season}-07-01")
    weeks = sorted({r["week"] for r in practice_rows if r["week"]})
    print(f"practice rows: {len(practice_rows)} (weeks {weeks}); news blurbs: {len(news_rows)}")
    if args.counts:
        return

    from .data import load_season
    kicks = kickoffs(conn, args.season)
    # Only weeks his team played: a bye is not a week he sat.
    truth = {k: v for k, v in ratios(load_season(args.season)).items() if k[:2] in kicks}
    _report("Practice (questionable players) -- live PRACTICE_BLEND_MULT bucket",
            practice_buckets(practice_at_kickoff(practice_rows, kicks), truth), "mult=1.00")
    _report("News flags -- live news_signals.EFFECTS",
            news_buckets(news_at_kickoff(news_rows, kicks), truth), "none")


if __name__ == "__main__":
    main()
