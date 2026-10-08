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

    ``--truth sleeper`` scores against Sleeper's weekly stat lines instead
    (priced with ``data.TRUTH_SCORING`` through production's
    ``sleeper_projections.points_for``): available the morning after a week,
    where nflverse's weekly file lags. Every bucket carries a 95% bootstrap
    interval on its implied multiplier, and ``--transitions`` prints how a
    questionable player's first report day ends (the mix that prices a single
    Wednesday DNP, ``PRACTICE_BLEND_MULT["DNP_SINGLE"]``).

    The 2023-25 counterpart with the final practice day per player-week is
    ``evals.backtest.practice_backtest`` (nflverse ``injuries``); it set the
    live values. Re-calibrate once a season of history is collected.

RUN
    python -m evals.backtest.signal_history --db nfl_data.db --season 2026
    python -m evals.backtest.signal_history --db nfl_data.db --season 2026 --truth sleeper \\
        --weeks 3 4 --transitions
    python -m evals.backtest.signal_history --db nfl_data.db --season 2026 --counts
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
from collections import defaultdict
from datetime import datetime
from statistics import mean

from nfl_mcp.game_clock import parse_kickoff
from nfl_mcp.news_signals import adjustment, build_index, signals_for
from nfl_mcp.opportunity_tools import norm_name
from nfl_mcp.projections import practice_blend_mult
from nfl_mcp.teams import normalize_team

# Trailing games a player needs before his ratio is scored.
MIN_TRAILING = 2
# ...averaging at least this many points: a 1-point player's ratio is noise
# (the 2023-25 backtest uses the same floor).
MIN_BASE_PPG = 5.0
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
    # The game designation is published with Friday's report; in weeks 3-4
    # of 2026 it was only recorded by a re-read after kickoff. Reading it
    # late does not leak the outcome (it was public before the game), so it
    # is taken from any row of the week when the on-time rows lack it.
    late: dict[tuple, str] = {}
    for r in sorted(rows, key=lambda r: (r["date"], r["recorded_at"])):
        if r.get("game_status"):
            late[(r["week"], r["team"], r["name_key"])] = r["game_status"]
    out = {}
    for key, by_day in days.items():
        line = [by_day[d] for d in sorted(by_day)]
        out[key] = {"pattern": "-".join(r["status"] for r in line), "latest": line[-1]["status"],
                    "game_status": next((r["game_status"] for r in reversed(line)
                                         if r.get("game_status")), None) or late.get(key)}
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
    player-week with ``MIN_TRAILING`` prior games averaging at least
    ``MIN_BASE_PPG``, and 0.0 for a later week without a record (he did not
    play) -- through the last week in `records`, so a player who sat the
    latest week is counted too."""
    by_player: dict[tuple[str, str], dict[int, float]] = defaultdict(dict)
    for r in records:
        by_player[(norm_name(r["player"]), r["team"])][r["week"]] = r["ppr"]
    last = max((r["week"] for r in records), default=0)
    out = {}
    for (name, team), weeks in by_player.items():
        for week in range(min(weeks) + MIN_TRAILING, last + 1):
            prior = [p for w, p in weeks.items() if w < week]
            base = mean(prior) if len(prior) >= MIN_TRAILING else 0.0
            if base >= MIN_BASE_PPG:
                out[(week, team, name)] = weeks.get(week, 0.0) / base
    return out


def practice_buckets(practice: dict, truth: dict) -> dict[str, list[float]]:
    """Ratios of questionable players grouped by the live practice multiplier,
    by their latest practice day (``latest=LP`` ...), and every player-week
    not on the report (``not_on_report``, the healthy reference)."""
    out: dict[str, list[float]] = defaultdict(list)
    for key, p in practice.items():
        if (p["game_status"] or "").lower() != "questionable" or key not in truth:
            continue
        mult = practice_blend_mult("Questionable", p["latest"], p["pattern"])
        out[f"mult={mult:.2f}"].append(truth[key])
        out[f"latest={p['latest']}"].append(truth[key])
        out["questionable"].append(truth[key])
    out["not_on_report"] = [v for k, v in truth.items() if k not in practice]
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


BOOTSTRAP = 2000


def bootstrap_ci(vals: list[float], ref: list[float], seed: int = 7) -> tuple[float, float]:
    """95% interval of ``mean(vals) / mean(ref)``, both resampled."""
    rng = random.Random(seed)
    out = []
    for _ in range(BOOTSTRAP):
        r = mean(rng.choices(ref, k=len(ref)))
        if r > 0:
            out.append(mean(rng.choices(vals, k=len(vals))) / r)
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out)) - 1]


def _report(title: str, buckets: dict[str, list[float]], reference: str) -> None:
    print(f"\n{title}")
    ref_vals = buckets.get(reference) or []
    ref = mean(ref_vals) if ref_vals else None
    for name in sorted(buckets):
        vals = buckets[name]
        implied = f"{mean(vals) / ref:.2f}" if ref else "n/a"
        ci = ""
        if ref and name != reference and len(vals) >= 3:
            lo, hi = bootstrap_ci(vals, ref_vals)
            ci = f" [{lo:.2f}, {hi:.2f}]"
        noise = "" if len(vals) >= MIN_BUCKET else "  (too few)"
        print(f"  {name:<18} n={len(vals):<5} mean ratio={mean(vals):.2f} "
              f"implied mult vs {reference}={implied}{ci}{noise}")


SLEEPER_STATS_URL = "https://api.sleeper.app/stats/nfl/{season}/{week}"


def sleeper_records(season: int, weeks: list[int]) -> list[dict]:
    """``data.load_season``-shaped records (player, team, week, ppr) from
    Sleeper's weekly stat lines -- a player-week counts when he took a snap
    or played a game (``gp``)."""
    import httpx

    from nfl_mcp import sleeper_projections as sp

    from .data import TRUTH_SCORING
    from .sleeper_blend import compact
    out = []
    for week in weeks:
        params = [("season_type", "regular")] + [("position[]", p) for p in sp.PROJECTED_POSITIONS]
        resp = httpx.get(SLEEPER_STATS_URL.format(season=season, week=week), params=params,
                         timeout=60)
        resp.raise_for_status()
        rows = compact(resp.json())
        index = sp._index(rows)
        for r in rows:
            st, pl = r.get("stats") or {}, r.get("player") or {}
            if not (st.get("gp") or st.get("off_snp")):
                continue
            name = f"{pl.get('first_name') or ''} {pl.get('last_name') or ''}".strip()
            team = normalize_team(r.get("team")) or r.get("team")
            pts, _ = sp.points_for(index, TRUTH_SCORING, player_id=r.get("player_id"),
                                   name=name, team=r.get("team"))
            if name and team and pts is not None:
                out.append({"player": name, "team": team, "week": week, "ppr": pts})
    return out


def transitions(practice_rows: list[dict]) -> dict[str, dict[str, int]]:
    """``{first day: {final day: count}}`` per player-week of the NFL.com
    report (three or more days), for players whose latest game status is
    Questionable."""
    days: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for r in practice_rows:
        if r["source"] != "nfl.com":
            continue
        key = (r["week"], r["team"], r["name_key"])
        prev = days[key].get(r["date"])
        if prev is None or r["recorded_at"] >= prev["recorded_at"]:
            days[key][r["date"]] = r
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for by_day in days.values():
        line = [by_day[d] for d in sorted(by_day)]
        status = next((r["game_status"] for r in reversed(line) if r.get("game_status")), "")
        if len(line) < 3 or (status or "").lower() != "questionable":
            continue
        out[line[0]["status"]][line[-1]["status"]] += 1
    return out


def _rows(conn: sqlite3.Connection, sql: str, *params) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.getenv("NFL_MCP_DB_PATH", "nfl_data.db"))
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--counts", action="store_true", help="only show what has been collected")
    ap.add_argument("--truth", choices=("nflverse", "sleeper"), default="nflverse",
                    help="actual points: nflverse weekly file, or Sleeper's weekly stat lines")
    ap.add_argument("--weeks", type=int, nargs="+",
                    help="only score these weeks (default: every collected week)")
    ap.add_argument("--transitions", action="store_true",
                    help="also: how a questionable player's first report day ends")
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
    if args.weeks:
        kicks = {k: v for k, v in kicks.items() if k[0] in args.weeks}
    if args.transitions:
        print("\nQuestionable players: first report day -> last report day (NFL.com)")
        for first, finals in sorted(transitions(practice_rows).items()):
            n = sum(finals.values())
            print(f"  {first:4s} n={n:<4d} " + " ".join(
                f"{k}:{v / n:.0%}" for k, v in sorted(finals.items())))
    records = (sleeper_records(args.season, list(range(1, max(w for w, _ in kicks) + 1)))
               if args.truth == "sleeper" else load_season(args.season))
    # Only weeks his team played: a bye is not a week he sat.
    truth = {k: v for k, v in ratios(records).items() if k[:2] in kicks}
    _report("Practice (questionable players) -- live PRACTICE_BLEND_MULT bucket",
            practice_buckets(practice_at_kickoff(practice_rows, kicks), truth), "not_on_report")
    _report("News flags -- live news_signals.EFFECTS",
            news_buckets(news_at_kickoff(news_rows, kicks), truth), "none")


if __name__ == "__main__":
    main()
