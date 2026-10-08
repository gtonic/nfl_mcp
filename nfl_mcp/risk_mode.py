"""Risk-aware decisions: when to chase variance and when to protect a floor.

Expected points is the right objective only for a team that is neither behind
nor far ahead. A 0-4 team at 9% playoff odds facing a better roster needs the
lineup most likely to *win*, which is the boom/bust one; a heavy favourite
needs the one least likely to blow it. This module decides which of those a
team is in (``risk_mode``) and turns it into a lineup objective.

Modes:

* ``neutral`` — expected points (the classic optimizer).
* ``seek_variance`` — maximise P(score > T) for a target T at or above the
  points-optimal lineup's own expectation (the opponent's projection when you
  trail): a higher ceiling is worth some mean.
* ``protect_floor`` — maximise P(score > T) for a target at or below it: a
  higher floor is worth some upside.
* ``auto`` (default) — derived from this week's win probability (the
  points-optimal lineup against the opponent's projected starters) and the
  season's playoff odds (``playoff_tools``, cached). With an opponent known it
  maximises P(win) itself, which already tilts ceiling-first for an underdog
  and floor-first for a favourite; a long-shot season adds a further ceiling
  tilt in a close game, because a team that needs to win out (and the points
  tiebreak) gains from the upside more than it loses on the mean.

Team totals are sums of Normal(mean, sd) players — sd from the projection's
floor/ceiling band — with the QB↔pass-catcher stack covariance of
``win_probability``; P(score > T) is then exact, so no simulation is needed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

logger = logging.getLogger(__name__)

RISK_MODES = ("auto", "neutral", "seek_variance", "protect_floor")

# auto: this week's P(win) of the points-optimal lineup below which a team is
# the underdog (chase variance) and above which it is the favourite (protect).
UNDERDOG_P_WIN = 0.45
FAVOURITE_P_WIN = 0.55
# Playoff odds (percent) below which a season is a long shot, at or above
# which a playoff spot is as good as safe.
LONG_SHOT_PLAYOFF_PCT = 20.0
SAFE_PLAYOFF_PCT = 90.0
# A long-shot team keeps the extra ceiling tilt only while this week is not
# already comfortably in hand.
LONG_SHOT_MAX_P_WIN = 0.55
# How far (in sd of the score difference) the explicit / long-shot target sits
# above (seek) or below (protect) the points-optimal lineup's expectation.
TARGET_SHIFT_SD = 0.5
# Generic spread of a team's weekly score when no matchup is known, used to
# put a single player's variance in proportion (compare_players_for_slot).
GENERIC_TEAM_SD = 20.0

# Season playoff odds per (league, week): get_playoff_odds runs a Monte Carlo
# over projected strengths, far too slow to repeat for every lineup call.
ODDS_CACHE_TTL = 1800.0
ODDS_SIMS = 2000
_odds_cache: dict[tuple, tuple[float, dict]] = {}
_odds_locks: dict[tuple, asyncio.Lock] = {}


def normalize_risk_mode(mode: str | None) -> str:
    """``auto`` for None/empty; raises ValueError for an unknown mode."""
    if mode is None or str(mode).strip() == "":
        return "auto"
    m = str(mode).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"ceiling": "seek_variance", "variance": "seek_variance", "upside": "seek_variance",
               "floor": "protect_floor", "safe": "protect_floor", "mean": "neutral",
               "expected": "neutral", "points": "neutral"}
    m = aliases.get(m, m)
    if m not in RISK_MODES:
        raise ValueError(f"risk_mode must be one of {', '.join(RISK_MODES)} (got {mode!r})")
    return m


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_exceed(mean: float, var: float, target: float) -> float:
    """P(Normal(mean, var) > target)."""
    if var <= 0:
        return 1.0 if mean > target else 0.5 if mean == target else 0.0
    return phi((mean - target) / math.sqrt(var))


# --------------------------------------------------------------------------
# Season odds (cached)
# --------------------------------------------------------------------------

async def season_odds(league_id: str | None, week: int | None = None, db=None,
                      num_sims: int = ODDS_SIMS) -> dict:
    """``{"by_roster": {roster_id: odds row}, "playoff_teams", "source"}`` or ``{}``.

    One cached ``get_playoff_odds`` per league and week (fewer sims than the
    tool's default: the percentage only has to tell a long shot from a lock).
    Never raises — a lineup call must not fail on the season view.
    """
    if not league_id:
        return {}
    key = (str(league_id), week)
    hit = _odds_cache.get(key)
    if hit and time.monotonic() - hit[0] < ODDS_CACHE_TTL:
        return hit[1]
    lock = _odds_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _odds_cache.get(key)
        if hit and time.monotonic() - hit[0] < ODDS_CACHE_TTL:
            return hit[1]
        try:
            res = await _fetch_odds(str(league_id), num_sims, db)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"playoff odds unavailable for risk mode: {e}")
            return {}
        if not (res or {}).get("success") or not res.get("odds"):
            return {}
        out = {
            "by_roster": {o["roster_id"]: o for o in res["odds"]},
            "playoff_teams": res.get("playoff_teams"),
            "current_week": res.get("current_week"),
            "num_sims": res.get("num_sims"),
            "source": "get_playoff_odds",
        }
        _odds_cache[key] = (time.monotonic(), out)
        return out


async def _fetch_odds(league_id: str, num_sims: int, db) -> dict | None:
    """The playoff-odds read itself; a seam for tests."""
    from . import playoff_tools
    from .database import get_shared_db
    return await playoff_tools.get_playoff_odds(
        league_id, num_sims=num_sims, seed=7, db=db or get_shared_db())


def clear_odds_cache() -> None:
    _odds_cache.clear()
    _odds_locks.clear()


async def playoff_pct_for(league_id: str | None, roster_id: int | None,
                          week: int | None = None, db=None) -> float | None:
    """One roster's playoff odds in percent, or None when unknown."""
    if not league_id or roster_id is None:
        return None
    odds = await season_odds(league_id, week, db=db)
    row = (odds.get("by_roster") or {}).get(roster_id)
    return float(row["playoff_pct"]) if row and row.get("playoff_pct") is not None else None


def team_situation(playoff_pct: float | None) -> str:
    """contender / bubble / long_shot from playoff odds (unknown without)."""
    if playoff_pct is None:
        return "unknown"
    if playoff_pct >= 60.0:
        return "contender"
    if playoff_pct >= LONG_SHOT_PLAYOFF_PCT:
        return "bubble"
    return "long_shot"


# --------------------------------------------------------------------------
# Mode resolution
# --------------------------------------------------------------------------

def resolve(requested: str | None, p_win: float | None = None,
            playoff_pct: float | None = None) -> dict:
    """The effective mode, why, and how the target is set.

    ``p_win`` is this week's P(win) (0..1) of the points-optimal lineup, None
    without an opponent. Returns ``{risk_mode, requested, reason, target}``
    where ``target`` is ``"mean"`` (expected points), ``"p_win"`` (beat the
    opponent's projection) or ``"shifted"`` (a target TARGET_SHIFT_SD above /
    below one's own expectation).
    """
    req = normalize_risk_mode(requested)
    pw = f"{p_win * 100:.0f}%" if p_win is not None else None
    po = f"{playoff_pct:.0f}%" if playoff_pct is not None else None
    if req == "neutral":
        return {"risk_mode": "neutral", "requested": req, "target": "mean",
                "reason": "neutral requested: expected points only"}
    if req in ("seek_variance", "protect_floor"):
        word = "ceiling" if req == "seek_variance" else "floor"
        return {"risk_mode": req, "requested": req, "target": "shifted",
                "reason": f"{req} requested: favour {word} over expected points"
                          + (f" (P(win) {pw})" if pw else "")}
    # auto
    long_shot = playoff_pct is not None and playoff_pct < LONG_SHOT_PLAYOFF_PCT
    safe = playoff_pct is not None and playoff_pct >= SAFE_PLAYOFF_PCT
    if p_win is None:
        if long_shot:
            return {"risk_mode": "seek_variance", "requested": req, "target": "shifted",
                    "reason": f"long-shot season ({po} playoff odds): every week is "
                              "must-win, so ceiling beats floor (no opponent known)"}
        return {"risk_mode": "neutral", "requested": req, "target": "mean",
                "reason": "no opponent projection to weigh risk against — expected points"
                          + (f" (playoff odds {po})" if po else "")}
    if long_shot and p_win < LONG_SHOT_MAX_P_WIN:
        why = (f"underdog this week (P(win) {pw}) and a long-shot season ({po} playoff odds)"
               if p_win < UNDERDOG_P_WIN else
               f"long-shot season ({po} playoff odds) in a close game (P(win) {pw})")
        return {"risk_mode": "seek_variance", "requested": req, "target": "shifted",
                "reason": why + ": chase ceiling — a loss costs the season either way"}
    if p_win < UNDERDOG_P_WIN:
        return {"risk_mode": "seek_variance", "requested": req, "target": "p_win",
                "reason": f"underdog this week (P(win) {pw}): maximise P(win), "
                          "which favours ceiling over mean"}
    if p_win > FAVOURITE_P_WIN:
        extra = f"; playoff spot nearly safe ({po})" if safe else ""
        return {"risk_mode": "protect_floor", "requested": req, "target": "p_win",
                "reason": f"favourite this week (P(win) {pw}){extra}: maximise P(win), "
                          "which favours floor over upside"}
    return {"risk_mode": "neutral", "requested": req, "target": "p_win",
            "reason": f"close game (P(win) {pw}): maximise P(win), which here is "
                      "≈ the points-optimal lineup"}


def lineup_target(resolution: dict, mo_mean: float, mo_var: float,
                  opp_mean: float | None, opp_var: float) -> float | None:
    """The score to beat for this resolution, or None for plain expected points.

    ``mo_*`` describe the points-optimal lineup (the reference expectation).
    """
    kind = resolution["target"]
    if kind == "mean":
        return None
    if kind == "p_win" and opp_mean is not None:
        return opp_mean
    shift = TARGET_SHIFT_SD * math.sqrt(max(0.0, mo_var + opp_var))
    if resolution["risk_mode"] == "seek_variance":
        own = mo_mean + shift
        return max(opp_mean, own) if opp_mean is not None else own
    if resolution["risk_mode"] == "protect_floor":
        own = mo_mean - shift
        return min(opp_mean, own) if opp_mean is not None else own
    return opp_mean


def score_fn(target: float | None, opp_var: float):
    """``f(mean, var) -> objective`` for a lineup: its mean, or P(beat target)."""
    if target is None:
        return lambda mean, var: mean
    return lambda mean, var: p_exceed(mean, var + opp_var, target)


# --------------------------------------------------------------------------
# Describing a risk-adjusted choice
# --------------------------------------------------------------------------

def _name(p: dict) -> str:
    return p.get("name") or p.get("player") or "?"


def describe_difference(risk_players: list[dict], mean_players: list[dict], *,
                        p_win_risk: float | None, p_win_mean: float | None,
                        mean_risk: float, mean_mean: float,
                        mean_of, sd_of) -> dict | None:
    """``{starts, sits, summary, ...}`` when the two lineups differ, else None.

    Pairs who comes in with who goes out (highest-projected first on both
    sides), and states the trade-off: "starting X over Y raises P(win)
    41%→44% although mean −0.6".
    """
    risk_ids = {id(p) for p in risk_players}
    mean_ids = {id(p) for p in mean_players}
    ins = sorted((p for p in risk_players if id(p) not in mean_ids), key=mean_of, reverse=True)
    outs = sorted((p for p in mean_players if id(p) not in risk_ids), key=mean_of, reverse=True)
    if not ins and not outs:
        return None
    pairs = []
    for i in range(max(len(ins), len(outs))):
        a = ins[i] if i < len(ins) else None
        b = outs[i] if i < len(outs) else None
        pairs.append({
            "start": _name(a) if a else None,
            "start_mean": round(mean_of(a), 1) if a else None,
            "start_sd": round(sd_of(a), 1) if a else None,
            "over": _name(b) if b else None,
            "over_mean": round(mean_of(b), 1) if b else None,
            "over_sd": round(sd_of(b), 1) if b else None,
        })
    starts = " and ".join(_name(p) for p in ins) or "nobody new"
    sits = " and ".join(_name(p) for p in outs) or "nobody"
    delta = mean_risk - mean_mean
    if p_win_risk is not None and p_win_mean is not None:
        effect = (f"{'raises' if p_win_risk >= p_win_mean else 'lowers'} P(win) "
                  f"{p_win_mean * 100:.1f}%→{p_win_risk * 100:.1f}%")
    else:
        effect = "trades mean for a different risk profile"
    summary = (f"Starting {starts} over {sits} {effect}"
               + (f" although mean {delta:+.1f}" if delta < 0 else f" (mean {delta:+.1f})"))
    return {
        "swaps": pairs,
        "mean_delta": round(delta, 1),
        "p_win_points_optimal": round(p_win_mean * 100, 1) if p_win_mean is not None else None,
        "p_win_risk_adjusted": round(p_win_risk * 100, 1) if p_win_risk is not None else None,
        "summary": summary,
    }


def resolve_trade(requested: str | None, playoff_pct: float | None) -> dict:
    """Risk mode for a trade search, from the season alone.

    auto: a long shot (< LONG_SHOT_PLAYOFF_PCT) seeks variance — ceiling over
    steady points; a contender (>= 60%) protects — fantasy-playoff weeks and
    floor weigh more; anyone else (or unknown odds) is neutral.
    """
    req = normalize_risk_mode(requested)
    po = f"{playoff_pct:.0f}%" if playoff_pct is not None else None
    if req != "auto":
        return {"risk_mode": req, "requested": req, "reason": f"{req} requested"}
    situation = team_situation(playoff_pct)
    if situation == "long_shot":
        return {"risk_mode": "seek_variance", "requested": req,
                "reason": f"long-shot season ({po} playoff odds): weight lineup ceiling — "
                          "a steady small gain does not change the season"}
    if situation == "contender":
        return {"risk_mode": "protect_floor", "requested": req,
                "reason": f"contender ({po} playoff odds): weight fantasy-playoff-week points"}
    return {"risk_mode": "neutral", "requested": req,
            "reason": (f"bubble team ({po} playoff odds): rest-of-season points as they are"
                       if po else "playoff odds unknown: rest-of-season points as they are")}


def slot_choice(candidates: list[dict], requested: str | None, *,
                playoff_pct: float | None = None, context: dict | None = None) -> dict:
    """Which of several players to start in ONE slot under a risk mode.

    ``candidates``: ``[{key, mean, sd}]``. ``context`` (optional) is the
    matchup: ``{my_mean, my_var, opp_mean, opp_var, ref_key}`` — the team's
    lineup with ``ref_key`` (the compared player it already starts, else the
    points favourite) in the slot. With it every candidate gets his exact
    P(win) — rest of the lineup + him vs the opponent. Without it auto stays
    on expected points and an explicit mode sets the slot against a generic
    even game (GENERIC_TEAM_SD a side), so only the variance trade-off counts.

    Returns ``{resolution, best_key, points_best_key, by_key {key: {p_win,
    objective}}, has_matchup}``.
    """
    if not candidates:
        return {}
    points_best = max(candidates, key=lambda c: c["mean"])
    if context:
        ref = next((c for c in candidates if c["key"] == context.get("ref_key")), points_best)
        rest_mean = context["my_mean"] - ref["mean"]
        rest_var = max(0.0, context["my_var"] - ref["sd"] ** 2)
        opp_mean, opp_var = context["opp_mean"], context["opp_var"]
    else:
        rest_mean, rest_var = -points_best["mean"], GENERIC_TEAM_SD ** 2
        opp_mean, opp_var = 0.0, GENERIC_TEAM_SD ** 2

    def _p(c: dict) -> float:
        return p_exceed(rest_mean + c["mean"] - opp_mean, rest_var + c["sd"] ** 2 + opp_var, 0.0)

    resolution = resolve(requested, _p(points_best) if context else None, playoff_pct)
    target = lineup_target(resolution, rest_mean + points_best["mean"],
                           rest_var + points_best["sd"] ** 2,
                           opp_mean if (context or resolution["target"] == "shifted") else None,
                           opp_var)
    f = score_fn(target, opp_var)
    by_key = {c["key"]: {"p_win": round(_p(c) * 100, 1) if context else None,
                         "objective": f(rest_mean + c["mean"], rest_var + c["sd"] ** 2)}
              for c in candidates}
    best = max(candidates, key=lambda c: (by_key[c["key"]]["objective"], c["mean"]))
    return {"resolution": resolution, "best_key": best["key"],
            "points_best_key": points_best["key"], "by_key": by_key,
            "has_matchup": bool(context)}


__all__ = [
    "RISK_MODES", "describe_difference", "lineup_target", "normalize_risk_mode", "p_exceed",
    "playoff_pct_for", "resolve", "resolve_trade", "score_fn", "season_odds", "slot_choice", "team_situation",
]
