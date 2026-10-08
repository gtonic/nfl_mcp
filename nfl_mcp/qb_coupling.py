"""Quarterback <-> pass-catcher coupling: who throws to him, who he throws to.

A receiver's week depends on his quarterback. With the starter Out the
weekly projection used to be unchanged (Egbuka at 8.3 with Mayfield out),
because neither share of the blend sees it: our model prices the receiver's
trailing volume, which came with the starter, and Sleeper's stat line did not
move either -- Egbuka's Sleeper projection went 9.7 -> 10.8 -> 10.2 over
weeks 3-5 of 2026 with Mayfield (week 3) and then Jalon Daniels throwing.

``receiver_context`` reads, for a WR or TE, the team's starting quarterback
(the highest-valued QB in the market depth, ``projections._depth_map``):
when he is Out (or IR / suspended) the receiver takes ``RECEIVER_MULT`` for
his position and the backup's tier (``BACKUP_TIERS``: the backup's market
rank at QB); Doubtful counts at ``DOUBTFUL_WEIGHT`` of it. The multiplier is
applied after the model is built, on both shares of the blend:

    sleeper_share x sleeper_mult                 # the full multiplier
    model_share   x (1 - (1 - m) x with_share)   # what his volume has not seen

``with_share`` is the share of his trailing games the starter played (pass
attempts >= ``QB_PLAYED_ATTEMPTS``): weeks already played with the backup are
in the model's volume. A starter who has not played any of them is not this
offense's quarterback any more, and nothing is applied.

Backtest (``evals/backtest/sleeper_blend.py --qb-coupling``, 2023-25 weeks
3+, Sleeper-matched rows; "out" = the team's starter over its last six games
did not start, nflverse ``games.csv``; tier = the backup's previous-season
rank, the backtest's stand-in for the market): WR with a low/mid backup
(n=396) scored 0.916 of the blend's total against 1.033 for every other WR
(n=2851) -- 11% over-projected, Sleeper's share as much as ours. With the
constants below their blend MAE goes 5.103 -> 4.887 (bias +0.82 -> -0.34;
low 5.111 -> 4.893, mid 5.044 -> 4.842), every WR with his starter out
(n=476) 5.148 -> 4.968. A uniform cut on the *other* WRs does not help
(5.504 -> 5.466 at 0.9, the skew of fantasy points; 5.496 at 0.85), so the
gain is the quarterback, not shrinkage. A starter-grade backup (n=80) cost
nothing (1.069), and tight ends gained: TE with a backup scored 1.17 of the
blend (n=228; more check-downs), so they are flagged, never cut.

``catchers_context`` is the inverse, for a QB: his top two pass catchers by
market value (WR/TE) Out or Doubtful. The backtest found no effect worth
pricing -- QBs whose top-two catcher sat (n=191) scored 1.035 of the blend
against 1.019 for the rest, and any cut raised the MAE (6.108 -> 6.153 at
0.95) -- so it is reported with a confidence cut (those rows are noisier:
MAE 6.11 vs 5.86) and ``CATCHER_MULT`` stays 1.0.

Pure: the depth map, a ``status_of(name, team)`` lookup and the nflverse
name index come from ``projections.project_many``.
"""
from __future__ import annotations

from . import opportunity_tools
from .opportunity import DEFAULT_LOOKBACK

# Backup quarterback tiers by his market rank at QB (positional rank); an
# unranked backup is "low".
BACKUP_TIERS: tuple[tuple[int | None, str], ...] = (
    (24, "starter_grade"), (36, "mid"), (None, "low"))
# Receiver multiplier with the starter out, by position and backup tier.
# From the backtest (module doc): WR low 0.87 / mid 0.90 (total-points ratio
# 0.928 / 0.833 vs 1.033 for other WRs; mid is the smaller sample, so kept
# above low); starter-grade and every TE are not cut.
RECEIVER_MULT: dict[str, dict[str, float]] = {
    "WR": {"starter_grade": 1.0, "mid": 0.90, "low": 0.87},
    "TE": {"starter_grade": 1.0, "mid": 1.0, "low": 1.0},
}
MIN_MULT = 0.80
MAX_MULT = 1.0
# A doubtful starter sits most weeks; the multiplier counts at this weight.
DOUBTFUL_WEIGHT = 0.75
# Pass attempts that make a quarterback's game one he played in.
QB_PLAYED_ATTEMPTS = 10
# The inverse (a QB's top-two pass catchers out): flagged, not priced -- see
# the module doc. Confidence comes down: the projection is less certain.
CATCHER_MULT = 1.0
TOP_CATCHERS = 2
CATCHERS_OUT_CONFIDENCE = -5


def qb_tier(rank: int | None) -> str:
    """The backup's tier from his positional rank (None: unranked)."""
    r = rank if isinstance(rank, int) and rank > 0 else None
    for last, tier in BACKUP_TIERS:
        if last is None or (r is not None and r <= last):
            return tier
    return BACKUP_TIERS[-1][1]


def _kind(status: str | None) -> str:
    from .injury_status import availability
    return availability(status)


def _with_share(opp_index: dict | None, name: str, starter: str, week: int | None) -> float | None:
    """Share of his trailing games the starter played; None without logs."""
    if not opp_index or not week:
        return None
    window = [g["week"] for g in opportunity_tools.prior_games(opp_index, name, week)
              [-DEFAULT_LOOKBACK:]]
    if not window:
        return None
    played = {g["week"] for g in opportunity_tools.prior_games(opp_index, starter, week)
              if float(g.get("attempts") or 0) >= QB_PLAYED_ATTEMPTS}
    return round(sum(1 for w in window if w in played) / len(window), 2)


def receiver_context(depth: dict, team: str, position: str, name: str, status_of,
                     opp_index: dict | None = None, week: int | None = None) -> dict | None:
    """``{starter, starter_status, backup, backup_rank, backup_tier,
    with_starter_share, sleeper_mult, model_mult, applied, reason}`` for a
    WR/TE whose starting QB is Out or Doubtful; None otherwise."""
    position = (position or "").upper()
    if position not in RECEIVER_MULT or not depth or status_of is None:
        return None
    qbs = [e for e in depth.get((team, "QB"), []) if e.get("name")]
    if not qbs:
        return None
    starter = qbs[0]["name"]
    status = status_of(starter, team)
    kind = _kind(status)
    if kind not in ("out", "doubtful"):
        return None
    backup = next((e for e in qbs[1:]
                   if _kind(status_of(e["name"], team)) not in ("out", "doubtful")), None)
    rank = (backup or {}).get("position_rank")
    tier = qb_tier(rank)
    weight = 1.0 if kind == "out" else DOUBTFUL_WEIGHT
    full = RECEIVER_MULT[position][tier]
    mult = round(max(MIN_MULT, min(MAX_MULT, 1.0 - (1.0 - full) * weight)), 3)
    share = _with_share(opp_index, name, starter, week)
    who = backup["name"] if backup else "an unranked backup"
    ctx = {"starter": starter, "starter_status": status, "backup": (backup or {}).get("name"),
           "backup_rank": rank, "backup_tier": tier, "with_starter_share": share,
           "sleeper_mult": 1.0, "model_mult": 1.0, "applied": False}
    if share == 0.0:
        ctx["reason"] = (f"{starter} ({status}) has not played in his trailing games — "
                         "his numbers are already the backup's")
        return ctx
    model = round(1.0 - (1.0 - mult) * (1.0 if share is None else share), 3)
    ctx.update(sleeper_mult=mult, model_mult=model, applied=mult < 1.0)
    if mult < 1.0:
        ctx["reason"] = (f"{starter} {status} — {who} ({tier.replace('_', '-')} backup) "
                         f"throwing: x{mult}")
    elif position == "TE":
        ctx["reason"] = (f"{starter} {status} — {who} throwing; tight ends hold their "
                         "volume with a backup (backtest), not cut")
    else:
        ctx["reason"] = f"{starter} {status} — {who} is a starter-grade backup, not cut"
    return ctx


def catchers_context(depth: dict, team: str, status_of) -> dict | None:
    """``{top_pass_catchers, missing, questionable, multiplier,
    confidence_delta, reason}`` for a QB whose top-two pass catchers (WR/TE
    by market value) include one Out/Doubtful or Questionable; else None."""
    if not depth or status_of is None:
        return None
    catchers = sorted(
        (e for pos in ("WR", "TE") for e in depth.get((team, pos), []) if e.get("name")),
        key=lambda e: -float(e.get("value") or 0))[:TOP_CATCHERS]
    missing, doubtful_q = [], []
    for e in catchers:
        status = status_of(e["name"], team)
        kind = _kind(status)
        if kind in ("out", "doubtful"):
            missing.append({"name": e["name"], "status": status})
        elif kind == "questionable":
            doubtful_q.append({"name": e["name"], "status": status})
    if not missing and not doubtful_q:
        return None
    parts = [f"{m['name']} {m['status']}" for m in (*missing, *doubtful_q)]
    return {
        "top_pass_catchers": [e["name"] for e in catchers],
        "missing": missing,
        "questionable": doubtful_q,
        "multiplier": CATCHER_MULT,
        "confidence_delta": CATCHERS_OUT_CONFIDENCE if missing else 0,
        "reason": ("top pass catchers: " + ", ".join(parts)
                   + " — reported, not priced (no effect on QB scoring in the backtest)"),
    }
