"""Risk-weighted trade ranking: what a long shot and a contender should want.

A trade's rest-of-season lineup delta (`your_gain`) is the honest headline and
stays the both-sides-gain bar. What it does not say is *which* points: a 0-4
team at 9% playoff odds gains nothing from a steady +0.5 a week — it needs the
weeks it can still win and a lineup with a ceiling — while a 97% contender
cares most about the fantasy-playoff weeks and a reliable floor. Under
``risk_mode`` (see `risk_mode.resolve_trade`) proposals are re-ranked on

* ``seek_variance`` (long shot): ``rank_score + UPSIDE_WEIGHT × upside_gain``,
  the change in the weekly lineup's ceiling beyond its mean (z = 1 sd, summed
  over the remaining regular-season weeks; player sd = weekly points × the
  position volatility of the projections);
* ``protect_floor`` (contender): ``rank_score + PLAYOFF_WEIGHT ×
  playoff_gain``, the lineup change over the fantasy-playoff weeks;

reported per proposal as ``risk {risk_mode, upside_gain, playoff_gain,
risk_score}``. ``your_gain`` is never changed.
"""
from __future__ import annotations

import math

# Points of ranking per point of weekly-ceiling gain (long shots).
UPSIDE_WEIGHT = 0.5
# Extra weight on fantasy-playoff-week lineup points (contenders): their
# points then count 1.5x in the ranking.
PLAYOFF_WEIGHT = 0.5
UPSIDE_Z = 1.0


def _vol(position: str | None) -> float:
    from .projections import _VOLATILITY
    return _VOLATILITY.get((position or "").upper(), 0.35)


def _week_lineup(players: list[dict], slots: dict[str, int], week: int) -> tuple[float, float]:
    """(mean, sd) of the best legal lineup in `week`."""
    from .roster_needs import starting_lineup
    week_players = [{"position": p.get("position"),
                     "projected_points": float((p.get("weekly_points") or {}).get(week, 0.0) or 0.0)}
                    for p in players]
    lineup = starting_lineup(week_players, slots)
    mean = sum(p["projected_points"] for p in lineup)
    var = sum((p["projected_points"] * _vol(p["position"])) ** 2 for p in lineup)
    return mean, math.sqrt(var)


def lineup_profile(players: list[dict], slots: dict[str, int], weeks: list[int]) -> dict[int, tuple]:
    """``{week: (mean, sd)}`` of the best lineup each week."""
    return {w: _week_lineup(players, slots, w) for w in weeks}


def risk_components(before: list[dict], after: list[dict], slots: dict[str, int],
                    regular_weeks: list[int], playoff_weeks: list[int],
                    before_profile: dict[int, tuple] | None = None) -> dict:
    """``{upside_gain, playoff_gain}`` of replacing roster `before` by `after`."""
    weeks = sorted(set(regular_weeks) | set(playoff_weeks))
    prof_b = before_profile or lineup_profile(before, slots, weeks)
    prof_a = lineup_profile(after, slots, weeks)
    upside = sum(UPSIDE_Z * (prof_a[w][1] - prof_b[w][1]) for w in regular_weeks)
    playoff = sum(prof_a[w][0] - prof_b[w][0] for w in playoff_weeks)
    return {"upside_gain": round(upside, 1), "playoff_gain": round(playoff, 1)}


def risk_score(rank_score: float, mode: str, components: dict) -> float:
    if mode == "seek_variance":
        return round(rank_score + UPSIDE_WEIGHT * components["upside_gain"], 1)
    if mode == "protect_floor":
        return round(rank_score + PLAYOFF_WEIGHT * components["playoff_gain"], 1)
    return round(rank_score, 1)


def adjust_proposals(proposals: list[dict], *, mine: list[dict], by_pid: dict[str, dict],
                     slots: dict[str, int], regular_weeks: list[int],
                     playoff_weeks: list[int], resolution: dict) -> list[dict]:
    """Attach ``risk`` to each proposal and re-rank by ``risk.risk_score``
    (``rank_score`` when neutral). ``mine``: the requesting roster's scored
    entries; ``by_pid``: every scored entry by player id."""
    mode = resolution["risk_mode"]
    if not proposals:
        return proposals
    weeks = sorted(set(regular_weeks) | set(playoff_weeks))
    base = lineup_profile(mine, slots, weeks)
    for p in proposals:
        gives = p["you_give"] if isinstance(p["you_give"], list) else [p["you_give"]]
        gets = p["you_get"] if isinstance(p["you_get"], list) else [p["you_get"]]
        give_ids = {str(x.get("player_id")) for x in gives}
        after = [e for e in mine if str(e.get("player_id")) not in give_ids]
        after += [by_pid[str(x.get("player_id"))] for x in gets if str(x.get("player_id")) in by_pid]
        comps = risk_components(mine, after, slots, regular_weeks, playoff_weeks, base)
        p["risk"] = {"risk_mode": mode, **comps,
                     "risk_score": risk_score(p.get("rank_score", p.get("your_gain", 0.0)),
                                              mode, comps)}
    if mode in ("seek_variance", "protect_floor"):
        proposals.sort(key=lambda p: (p["risk"]["risk_score"], p.get("mutual_gain", 0.0)),
                       reverse=True)
    return proposals


__all__ = ["adjust_proposals", "lineup_profile", "risk_components", "risk_score"]
