"""The weekly accuracy loop: how good were the projections, and which signals help.

Every briefing, ``project_players`` and league-changes run logs its pre-kickoff
projections (``projection_store``), and since schema v18 the signals that were
active on each one (``projection_store.signals_of``). Once a week is over,
:func:`grade_week` grades them:

- **projection**: the *last* pre-kickoff row per (scoring, player) — the number
  the model stood on at lock, with the most news behind it. (The retro grades
  the *first* row instead: the one the lineup was set against.)
- **actual**: the player's points in that row's scoring — Sleeper's own
  ``players_points`` for a league that logged it, else his stat line priced
  with the league's weights (``ScoringModel``) — and, for comparing across
  leagues, in neutral half-PPR (Sleeper's ``pts_half_ppr``). A logged player
  with no stat line did not play: 0.
- **signals**: unpacked into columns (role trend, returning teammates,
  practice, QB coupling, news flags, injury status, projection source, ours
  and Sleeper's numbers) so errors can be grouped on them.

Rows go to ``projection_accuracy`` (one per week, scoring and player; a regrade
replaces them). The prefetch loop grades each week once it is final and once
more a day and a half after its last kickoff (Sleeper's stat corrections);
``refresh_data(scope=["accuracy"])`` does the same on demand; both then run the
weekly signal review (`signal_review`) of each newly graded week and store it.

:func:`get_projection_accuracy` reads it back: MAE and bias per position, per
projection source and per signal (rows with vs without it), the trend over
weeks, our model vs Sleeper vs the blend on the same rows, and a short
interpretation. A player projected at zero who scored zero (a bye, a ruled-out
player who sat) is left out of the numbers: it would flatter them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from .errors import create_success_response
from .lineup_slots import normalize_position
from .scoring import ScoringModel, league_scoring

logger = logging.getLogger(__name__)

# A league's week is regraded once if it was graded sooner than this after the
# week's last kickoff: Sleeper's stat corrections land by Tuesday/Wednesday.
REGRADE_AFTER = timedelta(hours=36)
# Below this many rows a bucket's number is shown but not interpreted.
MIN_SIGNAL_N = 10
# A signal is called out when its rows' bias differs from the rest by this much.
SIGNAL_BIAS_GAP = 1.5
# Our number and Sleeper's this far apart = a "disagreement" row (as in
# `sleeper_projections.DISAGREE_ABSOLUTE`).
DISAGREE_POINTS = 4.0
_HALF_PPR = ScoringModel.preset(0.5)


# --------------------------------------------------------------------------
# Network seams (patched in tests)
# --------------------------------------------------------------------------

async def _week_stats(season: int, week: int) -> dict[str, dict]:
    """Sleeper's stat lines for one week, ``{player_id: stats}``."""
    from .usage_trends import _fetch_week_stats
    return await _fetch_week_stats(season, week)


async def _league(league_id: str) -> dict:
    from . import sleeper_tools
    return ((await sleeper_tools.get_league(league_id)) or {}).get("league") or {}


async def _matchups(league_id: str, week: int) -> list[dict]:
    from . import sleeper_tools
    return ((await sleeper_tools.get_matchups(league_id, week)) or {}).get("matchups") or []


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

def price_actual(stats: dict | None, model: ScoringModel) -> float:
    """A real stat line in a league's scoring: each stat times its weight
    (Sleeper's own rule; the points-allowed tier flags are real 0/1 here)."""
    if not stats:
        return 0.0
    return round(sum(model.w(k) * float(v) for k, v in stats.items()
                     if isinstance(v, int | float) and not isinstance(v, bool)), 2)


def _half_ppr(stats: dict | None) -> float:
    if not stats:
        return 0.0
    value = stats.get("pts_half_ppr")
    if isinstance(value, int | float):
        return round(float(value), 2)
    return price_actual(stats, _HALF_PPR)


def _played(stats: dict | None) -> bool:
    if not stats:
        return False
    try:
        return float(stats.get("gp") or 0) > 0 or float(stats.get("off_snp") or 0) > 0
    except (TypeError, ValueError):
        return False


def _signals(row: dict) -> dict:
    raw = row.get("signals")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


async def _scoring_models(rows: list[dict]) -> tuple[dict[str, ScoringModel], dict[str, str]]:
    """``({scoring_key: model}, {scoring_key: league_id})`` for the keys in `rows`.

    A key logged under a league is read back from that league's settings (and
    only kept when its fingerprint still matches); a key without one from the
    three reception presets. A key neither explains cannot be priced.
    """
    keys = {r["scoring_key"] for r in rows}
    leagues: dict[str, set[str]] = {}
    for r in rows:
        if r.get("league_id"):
            leagues.setdefault(r["scoring_key"], set()).add(str(r["league_id"]))
    models: dict[str, ScoringModel] = {}
    league_of: dict[str, str] = {}
    wanted = sorted({lid for ids in leagues.values() for lid in ids})
    fetched = await asyncio.gather(*(_league(lid) for lid in wanted), return_exceptions=True)
    for lid, league in zip(wanted, fetched, strict=True):
        if isinstance(league, BaseException) or not league:
            continue
        model = league_scoring(league).model
        if model.fingerprint in keys and model.fingerprint not in models:
            models[model.fingerprint] = model
            league_of[model.fingerprint] = lid
    for ppr in (1.0, 0.5, 0.0):
        preset = ScoringModel.preset(ppr)
        if preset.fingerprint in keys:
            models.setdefault(preset.fingerprint, preset)
    return models, league_of


async def grade_week(db, season: int, week: int) -> dict:
    """Grade one finished week's logged projections into ``projection_accuracy``.

    Returns ``{week, graded, skipped_scoring, stats_rows}``. Nothing is written
    when Sleeper has no stat lines for the week yet.
    """
    rows = db.get_logged_week_rows(season, week)
    if not rows:
        return {"week": week, "graded": 0, "note": "no logged projections"}
    stats = await _week_stats(season, week)
    if not stats:
        return {"week": week, "graded": 0, "note": "no Sleeper stat lines for the week yet"}
    models, league_of = await _scoring_models(rows)
    # Sleeper's own per-player points for every league that logged the week.
    league_ids = sorted({str(r["league_id"]) for r in rows if r.get("league_id")})
    matchups = await asyncio.gather(*(_matchups(lid, week) for lid in league_ids),
                                    return_exceptions=True)
    points_by_league: dict[str, dict[str, float]] = {}
    for lid, resp in zip(league_ids, matchups, strict=True):
        if isinstance(resp, BaseException):
            continue
        pts: dict[str, float] = {}
        for m in resp or []:
            for pid, v in (m.get("players_points") or {}).items():
                if v is not None:
                    pts[str(pid)] = float(v)
        points_by_league[lid] = pts
    now = datetime.now(UTC).isoformat()
    out, skipped = [], set()
    for r in rows:
        key = r["scoring_key"]
        model = models.get(key)
        if model is None:
            skipped.add(key)
            continue
        pid = str(r["player_id"])
        line = stats.get(pid)
        lid = league_of.get(key)
        league_pts = (points_by_league.get(lid) or {}) if lid else {}
        if pid in league_pts:
            actual, source = league_pts[pid], "league_matchup"
        else:
            actual, source = price_actual(line, model), "stats" if line else "no_stat_line"
        sig = _signals(r)
        qb_mult = sig.get("qb_mult")
        out.append({
            "season": season, "week": week, "scoring_key": key, "player_id": pid,
            "league_id": r.get("league_id"), "player_name": r.get("player_name"),
            "position": normalize_position(r.get("position")) or r.get("position"),
            "team": r.get("team"),
            "projected": float(r["projected_points"]),
            "model_projection": sig.get("model_projection"),
            "sleeper_projection": sig.get("sleeper_projection"),
            "floor": r.get("floor"), "ceiling": r.get("ceiling"),
            "actual": round(float(actual), 2), "actual_half_ppr": _half_ppr(line),
            "actual_source": source, "played": int(_played(line) or abs(actual) > 0),
            "projection_source": sig.get("projection_source"),
            "injury_status": sig.get("injury_status"),
            "practice_status": sig.get("practice_status"),
            "practice_pattern": sig.get("practice_pattern"),
            "role_trend": sig.get("role_trend"),
            "returning_teammates": int(sig.get("returning_teammates") or 0) if sig else None,
            "qb_mult": float(qb_mult) if qb_mult is not None else None,
            "news_flags": sig.get("news_flags") or ([] if sig else None),
            "signals": sig or None,
            "log_source": r.get("source"), "projected_at": r.get("recorded_at"),
            "graded_at": now,
        })
    written = await asyncio.to_thread(db.upsert_projection_accuracy, out) if out else 0
    return {"week": week, "graded": written, "stats_rows": len(stats),
            **({"skipped_scoring": sorted(skipped)} if skipped else {})}


def _last_kickoff(db, season: int, week: int) -> datetime | None:
    try:
        kickoffs = db.get_week_kickoffs(season, week) or {}
    except Exception:
        return None
    times = []
    for k in kickoffs.values():
        try:
            times.append(datetime.fromisoformat(str(k).replace("Z", "+00:00")))
        except ValueError:
            continue
    return max(times) if times else None


def weeks_to_grade(db, season: int, through_week: int, now: datetime | None = None) -> list[int]:
    """Finished, logged weeks never graded, or graded before the stat
    corrections were in (sooner than :data:`REGRADE_AFTER` after the week's
    last kickoff) and now past that point."""
    now = now or datetime.now(UTC)
    graded = db.get_accuracy_graded_weeks(season)
    out = []
    for w in db.get_logged_weeks(season):
        if w > through_week:
            continue
        if w not in graded:
            out.append(w)
            continue
        last = _last_kickoff(db, season, w)
        if last is None:
            continue
        settled = last + REGRADE_AFTER
        try:
            at = datetime.fromisoformat(str(graded[w]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at < settled <= now:
            out.append(w)
    return out


async def refresh_accuracy(db, season: int | None = None, weeks: list[int] | None = None) -> dict:
    """Grade every week that needs it (the ``accuracy`` refresh scope)."""
    from .week_context import last_completed_week
    done = await last_completed_week(db)
    season = season or done["season"]
    through = done["week"] if season == done["season"] else 18
    todo = sorted(set(weeks)) if weeks else weeks_to_grade(db, season, through)
    todo = [w for w in todo if 1 <= w <= through]
    results = [await grade_week(db, season, w) for w in todo]
    # The weekly signal review of each newly graded week, stored so reading
    # it back (`get_weekly_signal_review`, the retro) is a lookup.
    from .signal_review import store_review
    reviewed = [r["week"] for r in results
                if r.get("graded") and await store_review(db, season, r["week"])]
    return {"fetched": len(todo), "written": sum(r.get("graded", 0) for r in results),
            "weeks": todo, "season": season, "results": results, "reviewed": reviewed}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _stats(rows: list[dict], key: str = "projected") -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "mae": None, "bias": None}
    errs = [float(r[key]) - float(r["actual"]) for r in rows]
    return {"n": n, "mae": round(sum(abs(e) for e in errs) / n, 2),
            "bias": round(sum(errs) / n, 2)}


def _has_news(flag: str):
    return lambda r: flag in (r.get("news_flags") or [])


def _flags(row: dict) -> list[str]:
    raw = row.get("news_flags")
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


_SIGNALS = {
    "role_down": lambda r: r.get("role_trend") == "role_down",
    "role_up": lambda r: r.get("role_trend") == "role_up",
    "returning_teammates": lambda r: (r.get("returning_teammates") or 0) > 0,
    "inherited_volume": lambda r: bool((r.get("_signals") or {}).get("inherited_volume")),
    "qb_coupling": lambda r: r.get("qb_mult") is not None and float(r["qb_mult"]) < 1.0,
    "practice_dnp": lambda r: (r.get("practice_status") or "").upper() == "DNP",
    "practice_limited": lambda r: (r.get("practice_status") or "").upper() == "LP",
    "questionable": lambda r: (r.get("injury_status") or "").lower() == "questionable",
    "doubtful": lambda r: (r.get("injury_status") or "").lower() == "doubtful",
    "model_only": lambda r: r.get("projection_source") == "model_only",
    "sleeper_disagreement": lambda r: (
        r.get("model_projection") is not None and r.get("sleeper_projection") is not None
        and abs(float(r["model_projection"]) - float(r["sleeper_projection"]))
        > DISAGREE_POINTS),
}


def accuracy_report(rows: list[dict], by_signal: bool = True) -> dict:
    """MAE / bias of graded rows (see the module doc); pure."""
    for r in rows:
        r["news_flags"] = _flags(r)
        r["_signals"] = _signals(r)
    graded = [r for r in rows if not (float(r["projected"]) == 0 and float(r["actual"]) == 0)]
    with_signals = [r for r in graded if r.get("_signals")]
    by_position: dict[str, list[dict]] = {}
    by_source: dict[str, list[dict]] = {}
    by_week: dict[int, list[dict]] = {}
    for r in graded:
        by_position.setdefault(r.get("position") or "?", []).append(r)
        by_source.setdefault(r.get("projection_source") or "unknown", []).append(r)
        by_week.setdefault(int(r["week"]), []).append(r)
    both = [r for r in graded if r.get("model_projection") is not None
            and r.get("sleeper_projection") is not None
            and r.get("projection_source") == "sleeper_blend"]
    components = {
        "n": len(both),
        "model": _stats(both, "model_projection"),
        "sleeper": _stats(both, "sleeper_projection"),
        "blend": _stats(both),
    } if both else None
    report = {
        "overall": _stats(graded),
        "half_ppr_actual_mean": (round(sum(float(r.get("actual_half_ppr") or 0)
                                           for r in graded) / len(graded), 2)
                                 if graded else None),
        "by_position": {p: _stats(rs) for p, rs in sorted(by_position.items())},
        "by_projection_source": {s: _stats(rs) for s, rs in sorted(by_source.items())},
        "components": components,
        "trend": [{"week": w, **_stats(rs)} for w, rs in sorted(by_week.items())],
        "rows_with_signals": len(with_signals),
        "excluded_zero_zero": len(rows) - len(graded),
    }
    if by_signal:
        report["by_signal"] = _by_signal(with_signals)
    report["interpretation"] = _interpret(report)
    for r in rows:
        r.pop("_signals", None)
    return report


def _by_signal(rows: list[dict]) -> dict:
    preds = dict(_SIGNALS)
    for flag in sorted({f for r in rows for f in r.get("news_flags") or []}):
        preds[f"news:{flag}"] = _has_news(flag)
    out = {}
    for name, pred in preds.items():
        hit = [r for r in rows if pred(r)]
        if not hit:
            continue
        rest = [r for r in rows if not pred(r)]
        w, wo = _stats(hit), _stats(rest)
        out[name] = {
            "with": w, "without": wo,
            "bias_gap": (round(w["bias"] - wo["bias"], 2)
                         if w["bias"] is not None and wo["bias"] is not None else None),
            "mae_gap": (round(w["mae"] - wo["mae"], 2)
                        if w["mae"] is not None and wo["mae"] is not None else None),
            "small_sample": w["n"] < MIN_SIGNAL_N,
        }
    return out


def _interpret(rep: dict) -> list[str]:
    lines = []
    o = rep["overall"]
    if not o["n"]:
        return ["No graded projections yet for these weeks."]
    lean = ("too high" if o["bias"] > 0.5 else "too low" if o["bias"] < -0.5 else "about right")
    lines.append(f"MAE {o['mae']} over {o['n']} player-weeks; on average projections ran "
                 f"{lean} ({o['bias']:+.2f} projected − actual).")
    pos = {p: s for p, s in rep["by_position"].items() if s["n"] >= MIN_SIGNAL_N}
    if len(pos) >= 2:
        worst = max(pos, key=lambda p: pos[p]["mae"])
        skew = max(pos, key=lambda p: abs(pos[p]["bias"]))
        lines.append(f"Largest error at {worst} (MAE {pos[worst]['mae']}); most skewed: "
                     f"{skew} ({pos[skew]['bias']:+.2f}).")
    c = rep.get("components")
    if c and c["n"] >= MIN_SIGNAL_N:
        ranked = sorted(("model", "sleeper", "blend"), key=lambda k: c[k]["mae"])
        lines.append(f"On {c['n']} rows with both numbers: " + ", ".join(
            f"{k} {c[k]['mae']}" for k in ranked) + f" MAE — {ranked[0]} was closest.")
    for name, s in (rep.get("by_signal") or {}).items():
        gap = s.get("bias_gap")
        if s["small_sample"] or gap is None or abs(gap) < SIGNAL_BIAS_GAP:
            continue
        lines.append(f"{name}: {s['with']['n']} rows ran {'higher' if gap > 0 else 'lower'} "
                     f"than the rest (bias {s['with']['bias']:+.2f} vs "
                     f"{s['without']['bias']:+.2f}) — the projection "
                     f"{'over' if gap > 0 else 'under'}-rates players with it; worth a "
                     "backtest of that signal's weight.")
    trend = [t for t in rep["trend"] if t["n"] >= MIN_SIGNAL_N]
    if len(trend) >= 3:
        first, last = trend[0]["mae"], trend[-1]["mae"]
        if abs(last - first) >= 0.5:
            lines.append(f"MAE {'improved' if last < first else 'worsened'} from {first} "
                         f"(week {trend[0]['week']}) to {last} (week {trend[-1]['week']}).")
    if rep["rows_with_signals"] < o["n"]:
        lines.append(f"{o['n'] - rep['rows_with_signals']} rows were logged before signals "
                     "were recorded (schema v18): they count in the totals but not by signal.")
    if o["n"] < 50:
        lines.append("Small sample: read the numbers as direction, not calibration.")
    return lines


# --------------------------------------------------------------------------
# MCP tool
# --------------------------------------------------------------------------

async def get_projection_accuracy(
    weeks: list[int] | None = None,
    position: str | None = None,
    by_signal: bool = True,
    season: int | None = None,
    league_id: str | None = None,
    db=None,
) -> dict:
    """Accuracy of the logged pre-kickoff projections (see the module doc).

    Weeks that are finished and logged but not graded yet are graded first.
    """
    from .database import get_shared_db
    from .week_context import last_completed_week

    db = db if db is not None else get_shared_db()
    done = await last_completed_week(db)
    season = season or done["season"]
    through = done["week"] if season == done["season"] else 18
    wanted = sorted({int(w) for w in weeks}) if weeks else None
    todo = [w for w in weeks_to_grade(db, season, through)
            if wanted is None or w in wanted]
    graded_now = []
    for w in todo:
        try:
            res = await grade_week(db, season, w)
            if res.get("graded"):
                graded_now.append(w)
        except Exception as e:  # report what is there
            logger.warning(f"accuracy grading failed for week {w}: {e}")
    pos = normalize_position(position) if position else None
    rows = db.get_projection_accuracy(season, weeks=wanted, position=pos, league_id=league_id)
    report = accuracy_report(rows, by_signal=bool(by_signal))
    return create_success_response({
        "season": season,
        "weeks": sorted({int(r["week"]) for r in rows}),
        "last_completed_week": done["week"] if season == done["season"] else None,
        "position": pos,
        "league_id": league_id,
        "graded_now": graded_now,
        **report,
        "definitions": {
            "projection": "the last pre-kickoff projection logged for the player that week",
            "actual": "points in the scoring the projection was made in (Sleeper's "
                      "players_points for a league, else the stat line priced in it)",
            "bias": "mean of projected − actual (+ = projections too high)",
            "excluded": "player-weeks projected 0 that scored 0 (byes, ruled-out players)",
            "by_signal": "rows with the signal vs rows without it (rows logged with signals only)",
            "components": "our model vs Sleeper vs the blend on the same Sleeper-blended rows",
        },
    })


__all__ = [
    "accuracy_report",
    "get_projection_accuracy",
    "grade_week",
    "price_actual",
    "refresh_accuracy",
    "weeks_to_grade",
]
