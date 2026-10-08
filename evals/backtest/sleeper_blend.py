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

    ``--role-shift`` also prices every row the way the live engine does once
    ``role_shift`` finds a new role: the model's volume weighted from the
    break week (``opportunity.POST_BREAK_WEIGHT``) and Sleeper's share times
    the role multiplier. The read uses nflverse carries / target share only —
    the backtest has no snap or red-zone history, which live rows add.

    ``--returning`` prices the returning-teammate deflation
    (``projections._returning_teammates``) on the rows it applies to: a
    same-team, same-position teammate ranked ahead (previous-season rank) or
    who took half the player's volume in their games together, who missed
    some of his trailing games and plays this week — the backtest's stand-in
    for "back from injury", which live reads off the report. The model keeps
    ``RETURNING_KEEP_WEIGHT`` of its rate, the rest is the rate over the games
    together (the rank prior with none).

    ``--qb-coupling`` prices ``qb_coupling`` on the rows it applies to: a
    WR/TE whose team's starter (most pass attempts over its last six games)
    did not start (nflverse ``games.csv`` starting QB -- realised, where live
    reads the Out/Doubtful designation), the backup's tier from his
    previous-season rank (live: market rank), Sleeper's share times the
    multiplier and the model's scaled by the share of the player's trailing
    games the starter played; and a QB whose top-two pass catchers by
    targets have no stat line that week. Reported against the position's
    other rows (total-points ratio) and a uniform-cut sweep, so a gain from
    the skew of fantasy points is not read as a quarterback effect.

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
    python -m evals.backtest.sleeper_blend --seasons 2023 2024 2025 --role-shift
    python -m evals.backtest.sleeper_blend --seasons 2023 2024 2025 --returning
    python -m evals.backtest.sleeper_blend --seasons 2023 2024 2025 --qb-coupling
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import defaultdict

import httpx

from nfl_mcp import qb_coupling, role_shift, usage_trends
from nfl_mcp import sleeper_projections as sp
from nfl_mcp.matchup_tools import attach_prior_season, compute_defense_rankings, matchup_ratio
from nfl_mcp.opportunity import project_opportunity
from nfl_mcp.projections import (
    _VOLATILITY,
    RETURNING_KEEP_WEIGHT,
    RETURNING_MIN_VOLUME_RATIO,
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


def _role_read(gs: list[dict], pos: str, team: str, week: int,
               team_carries: dict, teams_played: dict) -> dict:
    """``role_shift.classify`` on the weeks before `week`, as production reads
    them (``usage_trends.week_row``), nflverse shares only."""
    by_week = {g["week"]: g for g in gs}
    weeks = range(max(1, week - role_shift.LOOKBACK_WEEKS), week)
    rows = [usage_trends.week_row(w, by_week.get(w), None, team, team_carries,
                                  teams_played.get(w)) for w in weeks]
    return role_shift.classify(rows, pos)


def _volume(g: dict) -> float:
    return g["targets"] + g["carries"] + g.get("attempts", 0.0)


def _returning_weeks(pid: str, prior: list[dict], week: int, team: str, pos: str,
                     mates: dict[str, dict[int, dict]], ranks: dict) -> set[int]:
    """His trailing weeks a teammate who plays `week` missed (see module doc)."""
    window = [g["week"] for g in prior[-6:]]
    mine = {g["week"]: g for g in prior}
    out: set[int] = set()
    for mate, games in mates.items():
        if mate == pid or week not in games:
            continue
        missed = [w for w in window if w not in games]
        together = [w for w in mine if w in games]
        if not missed or not together:
            continue
        ahead = ranks.get(mate) is not None and (
            ranks.get(pid) is None or ranks[mate] < ranks[pid])
        shares = sum(_volume(games[w]) for w in together) >= \
            RETURNING_MIN_VOLUME_RATIO * sum(_volume(mine[w]) for w in together)
        if ahead or shares:
            out |= set(missed)
    return out


def _qb_coupling_read(pid: str, pos: str, prior: list[dict], week: int, team: str,
                      team_games: dict[str, dict[int, dict]], team_weeks: list[int],
                      started_by: str | None, ranks: dict) -> dict | None:
    """The QB <-> pass-catcher read for one row (see module doc), or None.

    WR/TE: the team's starter (most pass attempts over its last six games
    before `week`) did not start (nflverse ``games.csv``), with the backup's
    tier from his previous-season rank. QB: he starts, and one or both of
    the team's top-two pass catchers by targets over those games have no
    stat line this week. ``with_frac`` is the share of the player's trailing
    games in which the missing teammate(s) did play -- what is left for the
    model's own volume to have missed.
    """
    window = team_weeks[-6:]
    if not window:
        return None
    mine = [g["week"] for g in prior[-6:]]
    qbs: dict[str, float] = defaultdict(float)
    for mate, gs in team_games.items():
        for w in window:
            g = gs.get(w)
            if g is not None and g["position"] == "QB":
                qbs[mate] += g.get("attempts", 0.0)
    starter = max(qbs, key=qbs.get) if qbs else None
    if pos in ("WR", "TE"):
        if not starter or not started_by or started_by == starter:
            return None
        played = [w for w in mine
                  if (team_games.get(starter, {}).get(w) or {}).get("attempts", 0.0)
                  >= qb_coupling.QB_PLAYED_ATTEMPTS]
        return {"kind": "qb_out", "tier": qb_coupling.qb_tier(ranks.get(started_by)),
                "with_frac": len(played) / len(mine) if mine else 1.0}
    if pos != "QB" or started_by != pid:
        return None
    targets: dict[str, float] = defaultdict(float)
    for mate, gs in team_games.items():
        for w in window:
            g = gs.get(w)
            if g is not None and g["position"] in ("WR", "TE", "RB"):
                targets[mate] += g["targets"]
    top = sorted(targets, key=targets.get, reverse=True)[:2]
    missing = [m for m in top if week not in team_games.get(m, {})]
    if not missing:
        return None
    fracs = [sum(1 for w in mine if w in team_games.get(m, {})) / len(mine) if mine else 1.0
             for m in missing]
    return {"kind": "catchers_out", "n": len(missing), "with_frac": sum(fracs) / len(fracs)}


def build_samples(seasons: list[int], start_week: int = 3, min_prior: int = 2,
                  min_trailing: float = 5.0, include_dnp: bool = False,
                  with_role: bool = False, with_returning: bool = False,
                  with_qb: bool = False) -> list[dict]:
    samples: list[dict] = []
    for season in seasons:
        records = load_season(season)
        prev = load_season(season - 1)
        ranks = _prior_ranks(prev)
        games = load_games(season)
        prior_final = compute_defense_rankings(_weekly_allowed(prev), season - 1)
        by_player: dict[str, list[dict]] = defaultdict(list)
        teams_played: dict[int, set] = defaultdict(set)
        team_carries: dict[tuple[str, int], float] = defaultdict(float)
        # (team, position) -> {player: {week: game for that team}}
        rooms: dict[tuple[str, str], dict[str, dict[int, dict]]] = defaultdict(
            lambda: defaultdict(dict))
        # team -> {player: {week: game}}, every position (QB coupling).
        squads: dict[str, dict[str, dict[int, dict]]] = defaultdict(lambda: defaultdict(dict))
        for r in records:
            squads[r["team"]][r["player_id"]][r["week"]] = r
            by_player[r["player_id"]].append(r)
            teams_played[r["week"]].add(r["team"])
            team_carries[(r["team"], r["week"])] += r["carries"]
            rooms[(r["team"], r["position"])][r["player_id"]][r["week"]] = r
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
                sample = {
                    "season": season, "week": week, "position": pos, "played": game is not None,
                    "model_raw": opp * mf * env, "model": model, "sleeper": theirs,
                    "sleeper_status": status, "actual": game["ppr"] if game else 0.0,
                }
                if with_role:
                    role = _role_read(gs, pos, team, week, team_carries, teams_played)
                    opp_role = (project_opportunity(prior, pos,
                                                    break_week=role["reweight_from_week"])
                                if role["reweight_from_week"] else opp)
                    sample.update({
                        "role_trend": role["role_trend"],
                        "role_mult": role["role_multiplier"],
                        "model_role": regressed_rate(opp_role, base_ppg(pos, ranks.get(pid)), n)
                        * mf * env,
                    })
                if with_qb and game is not None:
                    team_weeks = sorted(w for w in teams_played if w < week
                                        and team in teams_played[w])
                    read = _qb_coupling_read(
                        pid, pos, prior, week, team, squads[team], team_weeks,
                        (games.get((season, week, team)) or {}).get("qb_id"), ranks)
                    if read:
                        sample["qb_coupling"] = read
                if with_returning and game is not None:
                    missed = _returning_weeks(pid, prior, week, team, pos,
                                              rooms[(team, pos)], ranks)
                    kept = [g for g in prior if g["week"] not in missed]
                    prior_ppg = base_ppg(pos, ranks.get(pid))
                    kept_rate = (regressed_rate(project_opportunity(kept, pos), prior_ppg,
                                                min(len(kept), 6)) if kept else prior_ppg)
                    full_rate = model / (mf * env) if mf * env else 0.0
                    if missed and kept_rate < full_rate:
                        sample["model_returning"] = (
                            RETURNING_KEEP_WEIGHT * full_rate
                            + (1 - RETURNING_KEEP_WEIGHT) * kept_rate) * mf * env
                samples.append(sample)
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


def _blend_role(s: dict, w: float) -> float:
    """The live engine's blend once a role shift is priced: the change-point
    weighted model, and Sleeper's share times the role multiplier."""
    if s["sleeper_status"] == "not_projected":
        return 0.0
    return w * s["model_role"] + (1 - w) * s["sleeper"] * s["role_mult"]


def report_role(samples: list[dict]) -> None:
    w = sp.BLEND_MODEL_WEIGHT
    matched = [s for s in samples if s["sleeper"] is not None and "role_mult" in s]
    counts = defaultdict(int)
    for s in matched:
        counts[s["role_trend"]] += 1
    print("\nRole shift (nflverse shares only): " + ", ".join(
        f"{k} {v}" for k, v in sorted(counts.items())))
    print("MAE / bias: blend -> blend with role shift (all rows | shifted rows)")
    for pos in (*_POSITIONS, "ALL"):
        rows = [s for s in matched if pos in ("ALL", s["position"])]
        moved = [s for s in rows if s["role_trend"] in ("role_up", "role_down")]
        cells = []
        for subset in (rows, moved):
            if not subset:
                cells.append("n=0")
                continue
            act = [s["actual"] for s in subset]
            before = [_blend(s, w) for s in subset]
            after = [_blend_role(s, w) for s in subset]
            cells.append(f"n={len(subset):5d} {mae(before, act):5.3f} {bias(before, act):+5.2f}"
                         f" -> {mae(after, act):5.3f} {bias(after, act):+5.2f}")
        print(f"  {pos:3s} " + " | ".join(cells))
    for trend in ("role_up", "role_down"):
        rows = [s for s in matched if s["role_trend"] == trend]
        if rows:
            act = [s["actual"] for s in rows]
            print(f"  {trend} n={len(rows)}: model {mae([s['model'] for s in rows], act):.3f}"
                  f" -> {mae([s['model_role'] for s in rows], act):.3f} | blend "
                  f"{mae([_blend(s, w) for s in rows], act):.3f} -> "
                  f"{mae([_blend_role(s, w) for s in rows], act):.3f}")


def _qb_mults(s: dict) -> tuple[float, float]:
    """``(model_mult, sleeper_mult)`` production's coupling gives a row
    (``qb_coupling.receiver_context``; the QB side is ``CATCHER_MULT``)."""
    read = s.get("qb_coupling") or {}
    if read.get("kind") == "qb_out":
        m = qb_coupling.RECEIVER_MULT[s["position"]][read["tier"]]
        if read["with_frac"] == 0:
            return 1.0, 1.0
        return 1 - (1 - m) * read["with_frac"], m
    if read.get("kind") == "catchers_out":
        return qb_coupling.CATCHER_MULT, qb_coupling.CATCHER_MULT
    return 1.0, 1.0


def report_qb(samples: list[dict]) -> None:
    w = sp.BLEND_MODEL_WEIGHT
    matched = [s for s in samples if s["sleeper"] is not None]

    def after(s: dict, scale: float | None = None) -> float:
        if s["sleeper_status"] == "not_projected":
            return 0.0
        mm, sm = _qb_mults(s)
        if scale is not None:  # a uniform cut, for the sweep
            mm, sm = 1 - (1 - scale) * s["qb_coupling"]["with_frac"], scale
        return w * s["model"] * mm + (1 - w) * s["sleeper"] * sm

    print("\nQB coupling: blend MAE / bias before -> after on the affected rows; "
          "total-points ratio (actual / blend) vs the position's other rows")
    groups: dict = defaultdict(list)
    for s in matched:
        read = s.get("qb_coupling")
        if read:
            key = read["tier"] if read["kind"] == "qb_out" else f"{read['n']} out"
            groups[(read["kind"], s["position"], key)].append(s)
            groups[(read["kind"], s["position"], "ALL")].append(s)
    for (kind, pos, key), rows in sorted(groups.items()):
        act = [s["actual"] for s in rows]
        before = [_blend(s, w) for s in rows]
        adj = [after(s) for s in rows]
        ctrl = [s for s in matched if s["position"] == pos and "qb_coupling" not in s]
        ctrl_ratio = sum(s["actual"] for s in ctrl) / max(1e-9, sum(_blend(s, w) for s in ctrl))
        ratio = sum(act) / max(1e-9, sum(before))
        sweep = " ".join(f"{m:g}:{mae([after(s, m) for s in rows], act):.3f}"
                         for m in (0.95, 0.9, 0.85))
        print(f"  {kind:12s} {pos} {key:13s} n={len(rows):4d} {mae(before, act):5.3f} "
              f"{bias(before, act):+5.2f} -> {mae(adj, act):5.3f} {bias(adj, act):+5.2f} | "
              f"ratio {ratio:.3f} vs {ctrl_ratio:.3f} | uniform cut {sweep}")


def report_returning(samples: list[dict]) -> None:
    w = sp.BLEND_MODEL_WEIGHT
    rows = [s for s in samples if "model_returning" in s and s["sleeper"] is not None]
    print(f"\nReturning teammate (keep weight {RETURNING_KEEP_WEIGHT}): n={len(rows)} rows")
    print("MAE / bias, affected rows: model -> deflated | blend -> deflated")
    for pos in (*_POSITIONS, "ALL"):
        sub = [s for s in rows if pos in ("ALL", s["position"])]
        if not sub:
            continue
        act = [s["actual"] for s in sub]
        model, deflated = [s["model"] for s in sub], [s["model_returning"] for s in sub]
        before = [_blend(s, w) for s in sub]
        after = [0.0 if s["sleeper_status"] == "not_projected"
                 else w * s["model_returning"] + (1 - w) * s["sleeper"] for s in sub]
        print(f"  {pos:3s} n={len(sub):5d} model {mae(model, act):5.3f} {bias(model, act):+5.2f}"
              f" -> {mae(deflated, act):5.3f} {bias(deflated, act):+5.2f} | blend "
              f"{mae(before, act):5.3f} -> {mae(after, act):5.3f}")


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
    ap.add_argument("--role-shift", action="store_true",
                    help="also price the role-shift read the live engine applies")
    ap.add_argument("--returning", action="store_true",
                    help="also price the returning-teammate deflation")
    ap.add_argument("--qb-coupling", action="store_true",
                    help="also price the QB <-> pass-catcher coupling")
    args = ap.parse_args()
    samples = build_samples(args.seasons, args.start_week, include_dnp=args.include_dnp,
                            with_role=args.role_shift, with_returning=args.returning,
                            with_qb=args.qb_coupling)
    report(samples)
    if args.role_shift:
        report_role(samples)
    if args.returning:
        report_returning(samples)
    if args.qb_coupling:
        report_qb(samples)


if __name__ == "__main__":
    main()
