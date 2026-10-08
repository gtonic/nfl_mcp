"""Role shifts: has a player's role just changed, and by how much?

`usage_trends` lays out a player's weekly usage and fits a trend across the
window. A trend is the wrong shape for what moves a projection most: an abrupt
change of role. A back benched after a fumble (snaps 69% -> 36%, carries share
57% -> 28% in one week) or moved into a committee shows up in a least-squares
slope only once it is weeks old, and the projection's linear recency weights
(1..n over six games) dilute it the same way.

So this compares the latest one or two *played* weeks (the recent window)
with the up to three played weeks before them (the prior window), metric by
metric, on the rows `usage_trends.week_row` builds:

* snap share (Sleeper ``off_snp / tm_off_snp``),
* carries share (nflverse carries / team carries) — backs only,
* target share (nflverse) — backs and receivers,
* red-zone opportunities (Sleeper) — corroborates, never decides alone.

Byes and missed weeks are not rows of play and are skipped, exactly as
`usage_trends.metric_trends` skips them, so a week out injured is not read as
a lost role. A shift needs both an absolute move past the metric's threshold
and a relative one past `MIN_RELATIVE_SHIFT`. The classification is

    role_down / role_up   the role metrics moved past their thresholds one way
                          and none moved the other way
    stable                nothing moved enough (or the metrics disagree)
    insufficient_data     fewer than `MIN_PRIOR_WEEKS` + 1 played weeks with data

and comes with a bounded multiplier (`MIN_MULTIPLIER`..`MAX_MULTIPLIER`), a
confidence delta, the week the new role started (``break_week``, which the
opportunity base weights from), and human-readable flags such as
"carries share 57%→28% (week 4)".

The multiplier is applied to the Sleeper share of the blended weekly
projection (see ``projections._apply_sleeper_blend``): our own model reacts
through change-point weighting of its trailing volume instead
(``opportunity.POST_BREAK_WEIGHT``), so neither share is charged twice. Only a
lost role is priced -- see `DOWN_STRENGTH` for the backtest
(``evals/backtest/sleeper_blend.py --role-shift``; nflverse shares only, the
backtest has no snap history). Pure; no network.
"""
from __future__ import annotations

from . import usage_trends

# Played weeks compared: the latest one or two against up to three before.
RECENT_WEEKS = (1, 2)
PRIOR_WEEKS = 3
MIN_PRIOR_WEEKS = 2
# How far back the projection path reads weekly usage: enough team weeks to
# hold RECENT + PRIOR played weeks around a bye or a missed game.
LOOKBACK_WEEKS = 6

# Metric -> absolute move (percentage points; a count per game for red-zone
# opportunities) that counts as a shift. Much wider than `usage_trends`'s
# trend thresholds: those are for a drift over a window, these for a one- or
# two-week change that has to beat ordinary week-to-week noise. At 12 / 15 / 6
# points 45% of player-weeks read as a shift in the backtest; at these, 30%.
SHIFT_THRESHOLDS: dict[str, float] = {
    "snap_share": 15.0,
    "carries_share": 20.0,
    "target_share": 8.0,
    "rz_opportunities": 2.0,
}
# ...and the move relative to the prior level: 20% -> 32% is a shift, 70% ->
# 82% for a back already on the field every down is not much of one.
MIN_RELATIVE_SHIFT = 0.25
# Which metrics carry a position's volume (and so its multiplier). Snap share
# is a role metric for everyone; carries share only for backs. Quarterbacks
# are not classified: a benched QB is a depth-chart change Sleeper already
# zeroes, and a starter's shares do not move.
VOLUME_METRICS: dict[str, tuple[str, ...]] = {
    "RB": ("carries_share", "target_share"),
    "WR": ("target_share",),
    "TE": ("target_share",),
}
ROLE_METRICS: dict[str, tuple[str, ...]] = {
    pos: ("snap_share", *metrics) for pos, metrics in VOLUME_METRICS.items()
}
_LABELS = {m: usage_trends.TREND_METRICS[m][0] for m in SHIFT_THRESHOLDS}

# Multiplier on a lost role = 1 + strength x relative volume change, bounded.
# A one-week shift counts for `ONE_WEEK_WEIGHT` of a two-week one: a single
# game can be game script. Backtest (evals/backtest/sleeper_blend.py
# --role-shift, 2023-25 weeks 3+, Sleeper-matched rows), blend MAE:
#   role_down at strength 0.3: RB 5.404 -> 5.393, WR 5.450 -> 5.438; tight
#   ends got worse (4.748 -> 4.755), so a TE's lost role is flagged and
#   reweighted but not multiplied;
#   role_up at any strength > 0 was worse (5.554 -> 5.65+ on those rows):
#   Sleeper already prices a bigger role, and one big week regresses. A role
#   gained is reported, never multiplied.
DOWN_STRENGTH: dict[str, float] = {"RB": 0.3, "WR": 0.3, "TE": 0.0}
ONE_WEEK_WEIGHT = 0.75
MIN_MULTIPLIER = 0.80
MAX_MULTIPLIER = 1.15
# A role in motion is a less certain projection than a settled one.
SHIFT_CONFIDENCE_DELTA = -5


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _played(rows: list[dict]) -> list[dict]:
    return sorted((r for r in rows or [] if r.get("status") == "played"),
                  key=lambda r: r["week"])


def _fmt(metric: str, value: float) -> str:
    return f"{value:.1f}" if metric == "rz_opportunities" else f"{value:.0f}%"


def _shifts(played: list[dict], recent_n: int, metrics: tuple[str, ...]) -> dict[str, dict]:
    """Per metric: prior/recent means and the move, for one split of the weeks."""
    recent = played[-recent_n:]
    prior = played[:-recent_n][-PRIOR_WEEKS:]
    out: dict[str, dict] = {}
    for metric in metrics:
        before = [r[metric] for r in prior if r.get(metric) is not None]
        after = [r[metric] for r in recent if r.get(metric) is not None]
        if len(before) < MIN_PRIOR_WEEKS or len(after) < recent_n:
            continue
        b, a = _mean(before), _mean(after)
        delta = a - b
        rel = delta / b if b > 0 else (1.0 if delta > 0 else 0.0)
        moved = abs(delta) >= SHIFT_THRESHOLDS[metric] and abs(rel) >= MIN_RELATIVE_SHIFT
        out[metric] = {"prior": round(b, 1), "recent": round(a, 1), "delta": round(delta, 1),
                       "relative": round(rel, 3), "direction": (
                           (1 if delta > 0 else -1) if moved else 0)}
    return out


def _verdict(shifts: dict[str, dict], position: str) -> int:
    """+1 role up, -1 role down, 0 neither, from one split's shifts."""
    role = [m for m in ROLE_METRICS[position] if m in shifts]
    ups = [m for m in role if shifts[m]["direction"] > 0]
    downs = [m for m in role if shifts[m]["direction"] < 0]
    volume = set(VOLUME_METRICS[position])
    # A move needs the position's volume to be part of it: a snap-share swing
    # with the same carries is a different personnel package, not a new role.
    if downs and not ups and volume & set(downs):
        return -1
    if ups and not downs and volume & set(ups):
        return 1
    return 0


def classify(rows: list[dict], position: str | None) -> dict:
    """The role-shift read for one player from his weekly usage rows. Pure.

    ``rows`` are `usage_trends.week_row` dicts, any order; only played weeks
    count. Returns ``{role_trend, role_multiplier, confidence_delta,
    break_week, reweight_from_week, recent_weeks, metrics, role_flags}``.
    """
    pos = (position or "").upper()
    played = _played(rows)
    neutral = {"role_trend": "insufficient_data", "role_multiplier": 1.0,
               "confidence_delta": 0, "break_week": None, "reweight_from_week": None,
               "recent_weeks": 0,
               "metrics": {}, "role_flags": []}
    if pos not in ROLE_METRICS or len(played) < MIN_PRIOR_WEEKS + 1:
        return neutral
    metrics = (*ROLE_METRICS[pos], "rz_opportunities")
    # Prefer the two-week read (the new role held for two games); fall back
    # to the latest week alone when only it moved.
    best = None
    for recent_n in sorted(RECENT_WEEKS, reverse=True):
        if len(played) < recent_n + MIN_PRIOR_WEEKS:
            continue
        shifts = _shifts(played, recent_n, metrics)
        if not any(m in shifts for m in VOLUME_METRICS[pos]):
            continue
        verdict = _verdict(shifts, pos)
        if best is None:
            best = (recent_n, shifts, verdict)
        if verdict:
            best = (recent_n, shifts, verdict)
            break
    if best is None:
        return neutral
    recent_n, shifts, verdict = best
    break_week = played[-recent_n]["week"]
    if not verdict:
        return {**neutral, "role_trend": "stable", "recent_weeks": recent_n,
                "metrics": shifts}

    # The volume metrics that moved this way set the size of the change; the
    # snap share is averaged in when it agrees, so a bench role reads as one.
    moved = [m for m in (*VOLUME_METRICS[pos], "snap_share")
             if m in shifts and shifts[m]["direction"] == verdict]
    rel = _mean([shifts[m]["relative"] for m in moved])
    weight = 1.0 if recent_n >= 2 else ONE_WEEK_WEIGHT
    strength = DOWN_STRENGTH.get(pos, 0.0) if verdict < 0 else 0.0
    mult = 1.0 + strength * weight * rel
    mult = round(max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, mult)), 3)
    when = (f"week {break_week}" if recent_n == 1
            else f"weeks {break_week}-{played[-1]['week']}")
    flags = [
        f"{_LABELS[m]} {_fmt(m, s['prior'])}→{_fmt(m, s['recent'])} ({when})"
        for m, s in shifts.items() if s["direction"] == verdict
    ]
    return {
        "role_trend": "role_up" if verdict > 0 else "role_down",
        "role_multiplier": mult,
        "confidence_delta": SHIFT_CONFIDENCE_DELTA,
        "break_week": break_week,
        # The opportunity base weights its volume from here -- a lost role
        # only: the backtest's gained roles were better priced without it.
        "reweight_from_week": break_week if verdict < 0 else None,
        "recent_weeks": recent_n,
        "metrics": shifts,
        "role_flags": flags,
    }


def player_rows(
    entry: dict | None,
    team: str,
    weeks: list[int],
    team_carries: dict[tuple[str, int], float],
    played_teams: dict[int, set[str]],
    week_stats: dict[int, dict] | None = None,
    sleeper_id: str | None = None,
) -> list[dict]:
    """His `usage_trends.week_row` rows over `weeks`, from data already loaded:
    the nflverse logs entry (``build_name_index`` value) and, when given, the
    Sleeper weekly stat lines ``{week: {sleeper_id: stats}}``. Pure."""
    games = {g["week"]: g for g in (entry or {}).get("games", [])}
    stats = week_stats or {}
    return [
        usage_trends.week_row(
            wk, games.get(wk), (stats.get(wk) or {}).get(str(sleeper_id or "")),
            team, team_carries, played_teams.get(wk))
        for wk in weeks
    ]
