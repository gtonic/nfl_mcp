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
rank at QB; unranked is "low", see ``TIER_UNRANKED_BY_SLEEPER``); Doubtful
counts at ``DOUBTFUL_WEIGHT`` of it. The backup is the next healthy QB on
Sleeper's depth chart (``depth_chart_order``), then in the market depth, so
he is named even when the market does not value him. The multiplier is
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

A Questionable starter used to cost nothing, whatever the week looked like
(Lamar Jackson, week 5 2026: Questionable, DNP Wednesday and Thursday,
"only an outside chance to play" -- Zay Flowers still projected 14.1 with no
context). ``starter_sit_weight`` reads how likely the starter is to sit:
Questionable with ``QUESTIONABLE_DNP_DAYS`` DNP days ending on a DNP (or a
recent "ruled out" / "unlikely to play" report and no limited or full day
since) counts like Doubtful; a full latest practice is no cut, a limited one
or none at all is the share of questionable starters who sat (2023-25,
`evals/backtest/practice_backtest.py`: the starter of the team's last game on
the official report, did he start? Q + LP 47% [31%, 64%] of 32, Q + FP 0 of
6, Q + DNP 3 of 5, Doubtful 8 of 9, Out 32 of 34).
The practice line is `practice_reports.summarize` of the stored days plus
the day the starter's current report blurb names. When the report text says
he will miss more than this week ("multiple games", "week-to-week";
`multi_game_absence`) ROS keeps the cut for that long. The coupling backtest
itself (`sleeper_blend.py --qb-coupling`) reads realised non-starts, so its
numbers do not move with these weights.

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

# Backup quarterback tiers by his rank at QB (positional rank); an unranked
# backup is "low".
BACKUP_TIERS: tuple[tuple[int | None, str], ...] = (
    (24, "starter_grade"), (36, "mid"), (None, "low"))
# Where the backup's tier comes from: his market rank (FantasyCalc
# position_rank); a backup the market does not value (most of them) is
# "low". His rank among the week's Sleeper QB projections was tried as the
# tier for those (backtest `--qb-sleeper-tier`, 2023-25: 193 WR rows whose
# backup was unranked the season before but projected by Sleeper that week)
# and rejected: it runs the wrong way -- Sleeper's QB<=24 backups left their
# WRs at 0.772 of the blend, QB25-36 at 0.945, both below the 1.034 of other
# WRs and together (0.85) where "low" (0.87) already prices them; tiering
# them up un-cut them and raised the WR MAE 4.968 -> 5.112. It is reported
# (``backup_sleeper_rank``), not used for the tier.
TIER_SOURCE_MARKET = "market_rank"
TIER_SOURCE_SLEEPER = "sleeper_week_rank"
TIER_SOURCE_NONE = "unranked"
TIER_UNRANKED_BY_SLEEPER = False
# The Sleeper stat key the week's QBs are ranked by (a rank, so the format
# hardly matters for a quarterback).
SLEEPER_RANK_KEY = "pts_ppr"
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
# How likely the starter is to sit, as the weight his absence's multiplier
# counts at (`starter_sit_weight`). Out / IR / suspended: all of it.
OUT_WEIGHT = 1.0
# A doubtful starter sits most weeks; the multiplier counts at this weight.
# 2023-25 (`evals/backtest/practice_backtest.py`): 8 of 9 doubtful starters
# sat, 89% [57%, 98%] -- consistent, too few to move it.
DOUBTFUL_WEIGHT = 0.75
# Questionable used to be "a coin flip that usually plays: no cut". The
# 2023-25 reports say otherwise: of the starters of a team's last game who
# were questionable the next week, 42% [28%, 57%] did not start (n=43); with
# a limited latest practice 47% [31%, 64%] (n=32), with a full one none of 6.
# Questionable without a practice line (or one DNP so far) counts at
# QUESTIONABLE_WEIGHT, with a limited latest day at QUESTIONABLE_LIMITED_WEIGHT,
# with a full one (or rest) at QUESTIONABLE_FULL_WEIGHT.
QUESTIONABLE_WEIGHT = 0.4
QUESTIONABLE_LIMITED_WEIGHT = 0.45
QUESTIONABLE_FULL_WEIGHT = 0.0
# Questionable with no practice all week reads like Doubtful: did not
# practise on at least QUESTIONABLE_DNP_DAYS report days, the latest of them
# included (Lamar Jackson, week 5 2026: Questionable, DNP Wed and Thu,
# "only an outside chance to play"). 2023-25: 3 of 5 sat (60% [23%, 88%]) --
# too few to move it from the doubtful weight.
QUESTIONABLE_DNP_DAYS = 2
QUESTIONABLE_DNP_WEIGHT = DOUBTFUL_WEIGHT
# Questionable with a recent report that he will not / is unlikely to play
# (`news_signals` flags, at recency weight >= SIT_FLAG_MIN_WEIGHT) and no
# practice line that says otherwise: the same weight.
QUESTIONABLE_NEWS_WEIGHT = DOUBTFUL_WEIGHT
SIT_FLAGS = ("ruled_out", "unlikely_to_play")
# A recent "expected to play" keeps a Questionable starter uncut whatever his
# practice line (a veteran's rest days, a walkthrough week).
PLAY_FLAGS = ("expected_to_play",)
SIT_FLAG_MIN_WEIGHT = 0.5
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


def sleeper_qb_ranks(index: dict | None) -> dict[tuple[str, str], int]:
    """``{(normalized name, team): rank}`` of every QB Sleeper projects this
    week (`sleeper_projections` index), best first. Pure."""
    from .opportunity_tools import norm_name
    rows = [r for r in ((index or {}).get("by_id") or {}).values()
            if (r.get("position") or "").upper() == "QB" and r.get("name") and r.get("team")]
    rows.sort(key=lambda r: -float((r.get("stats") or {}).get(SLEEPER_RANK_KEY) or 0.0))
    return {(norm_name(r["name"]), r["team"]): i for i, r in enumerate(rows, 1)}


def sleeper_rank_of(name: str | None, team: str, sleeper_ranks: dict | None) -> int | None:
    """His rank among the week's Sleeper QB projections (None: not projected)."""
    from .opportunity_tools import norm_name
    return (sleeper_ranks or {}).get((norm_name(name), team)) if name else None


def backup_rank(name: str | None, team: str, market_rank: int | None,
                sleeper_ranks: dict | None,
                by_sleeper: bool = TIER_UNRANKED_BY_SLEEPER) -> tuple[int | None, str]:
    """``(rank, source)`` for the backup's tier: his market rank; unranked
    there, Sleeper's week rank only with `by_sleeper` (the rejected variant,
    see `TIER_UNRANKED_BY_SLEEPER`)."""
    if isinstance(market_rank, int) and market_rank > 0:
        return market_rank, TIER_SOURCE_MARKET
    rank = sleeper_rank_of(name, team, sleeper_ranks) if by_sleeper else None
    if rank:
        return rank, TIER_SOURCE_SLEEPER
    return None, TIER_SOURCE_NONE


def _pick_backup(qbs: list[dict], order: list[str], team: str, status_of) -> dict | None:
    """The quarterback throwing in the starter's place: the team's depth chart
    (Sleeper ``depth_chart_order``) first, then the market depth, skipping the
    starter and anyone else Out/Doubtful. ``{name, position_rank}``."""
    from .opportunity_tools import norm_name
    starter = norm_name(qbs[0]["name"])
    market = {norm_name(e["name"]): e for e in qbs}
    names = [n for n in order if n and norm_name(n) != starter]
    names += [e["name"] for e in qbs[1:] if norm_name(e["name"]) not in {norm_name(n) for n in names}]
    for n in names:
        if _kind(status_of(n, team)) in ("out", "doubtful"):
            continue
        entry = market.get(norm_name(n)) or {}
        return {"name": entry.get("name") or n, "position_rank": entry.get("position_rank")}
    return None


def _kind(status: str | None) -> str:
    from .injury_status import availability
    return availability(status)


def _recent(flags: list[dict] | None, wanted: tuple[str, ...]) -> list[dict]:
    return [f for f in flags or [] if f.get("flag") in wanted
            and float(f.get("weight") or 0.0) >= SIT_FLAG_MIN_WEIGHT]


def _dnp_days(practice: dict | None) -> list[str]:
    return [d.get("day") or d.get("date") for d in (practice or {}).get("days") or []
            if d.get("status") == "DNP"]


def starter_sit_weight(status: str | None, practice: dict | None = None,
                       flags: list[dict] | None = None) -> dict:
    """``{weight, basis, detail}``: how much of the backup's multiplier a
    receiver takes, from the starter's status, this week's practice line
    (`practice_reports.summarize`) and his report-text flags
    (`news_signals.signals_for`).

    Out / IR / suspended: ``OUT_WEIGHT``; Doubtful: ``DOUBTFUL_WEIGHT``;
    Questionable: ``QUESTIONABLE_DNP_WEIGHT`` with ``QUESTIONABLE_DNP_DAYS``
    DNP days ending on a DNP, ``QUESTIONABLE_NEWS_WEIGHT`` with a recent
    "ruled out" / "unlikely to play" and no limited or full latest day,
    ``QUESTIONABLE_FULL_WEIGHT`` with a full (or rest) latest day or a recent
    "expected to play", ``QUESTIONABLE_LIMITED_WEIGHT`` with a limited one,
    else ``QUESTIONABLE_WEIGHT``. ``basis`` is "status", "practice" or "news".
    Pure."""
    kind = _kind(status)
    if kind == "out":
        return {"weight": OUT_WEIGHT, "basis": "status", "detail": None}
    if kind == "doubtful":
        return {"weight": DOUBTFUL_WEIGHT, "basis": "status", "detail": None}
    if kind != "questionable":
        return {"weight": 0.0, "basis": "status", "detail": None}
    if _recent(flags, PLAY_FLAGS):
        return {"weight": QUESTIONABLE_FULL_WEIGHT, "basis": "news",
                "detail": "report: expected to play"}
    none = {"weight": QUESTIONABLE_WEIGHT, "basis": "status", "detail": None}
    days = (practice or {}).get("days") or []
    latest = days[-1].get("status") if days else None
    if latest in ("FP", "REST"):
        return {"weight": QUESTIONABLE_FULL_WEIGHT, "basis": "practice",
                "detail": latest}
    if latest == "LP":
        return {"weight": QUESTIONABLE_LIMITED_WEIGHT, "basis": "practice", "detail": "LP"}
    dnp = _dnp_days(practice)
    if latest == "DNP" and len(dnp) >= QUESTIONABLE_DNP_DAYS:
        return {"weight": QUESTIONABLE_DNP_WEIGHT, "basis": "practice",
                "detail": "DNP " + "/".join(dnp)}
    sit = _recent(flags, SIT_FLAGS)
    if sit:
        word = sit[0]["flag"].replace("_", " ")
        return {"weight": QUESTIONABLE_NEWS_WEIGHT, "basis": "news",
                "detail": (f"DNP {'/'.join(dnp)}, " if dnp else "") + f"report: {word}"}
    return none


def multi_game_absence(detail: dict | None,
                       flags: list[dict] | None = None) -> tuple[int, str | None]:
    """``(games, phrase)`` for a starter the report expects to miss more than
    this week while he is only Questionable / Doubtful ("could miss multiple
    games", "week-to-week"; `ros.multi_game_phrase`, or a recent
    ``week_to_week`` news flag). ``(0, None)`` otherwise: an Out starter's
    absence is `ros.expected_absence`'s."""
    from .ros import WEEK_TO_WEEK_GAMES, multi_game_phrase
    phrase = multi_game_phrase((detail or {}).get("description"))
    if not phrase and _recent(flags, ("week_to_week",)):
        phrase = "week-to-week"
    return (WEEK_TO_WEEK_GAMES, phrase) if phrase else (0, None)


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
                     opp_index: dict | None = None, week: int | None = None,
                     qb_order: dict[str, list[str]] | None = None,
                     sleeper_ranks: dict | None = None) -> dict | None:
    """``{starter, starter_status, backup, backup_rank, backup_tier,
    backup_tier_source, with_starter_share, sleeper_mult, model_mult,
    applied, reason, starter_sit_weight, starter_sit_basis, starter_practice}``
    for a WR/TE whose starting QB is Out, Doubtful, or Questionable without
    practising (`starter_sit_weight`); None otherwise.

    `qb_order` is ``{team: [QB names in depth-chart order]}`` (Sleeper's
    ``depth_chart_order``), which names the backup when the market does not
    value him; `sleeper_ranks` (`sleeper_qb_ranks`) is reported as
    ``backup_sleeper_rank`` (see `TIER_UNRANKED_BY_SLEEPER`)."""
    position = (position or "").upper()
    if position not in RECEIVER_MULT or not depth or status_of is None:
        return None
    qbs = [e for e in depth.get((team, "QB"), []) if e.get("name")]
    if not qbs:
        return None
    starter = qbs[0]["name"]
    status = status_of(starter, team)
    kind = _kind(status)
    if kind not in ("out", "doubtful", "questionable"):
        return None
    # The starter's practice line and report flags, when the lookup has them
    # (`projections._status_lookup`): a Questionable starter who has not
    # practised all week is priced like a Doubtful one.
    practice = flags = None
    if kind == "questionable":
        week_of = getattr(status_of, "practice_week", None)
        practice = week_of(starter, team) if callable(week_of) else None
        from . import news_signals
        flags = news_signals.signals_for(getattr(status_of, "news", None), starter, team)
    sit = starter_sit_weight(status, practice, flags)
    weight = sit["weight"]
    if weight <= 0:
        return None
    backup = _pick_backup(qbs, (qb_order or {}).get(team) or [], team, status_of)
    rank, source = backup_rank((backup or {}).get("name"), team,
                               (backup or {}).get("position_rank"), sleeper_ranks)
    tier = qb_tier(rank)
    full = RECEIVER_MULT[position][tier]
    mult = round(max(MIN_MULT, min(MAX_MULT, 1.0 - (1.0 - full) * weight)), 3)
    share = _with_share(opp_index, name, starter, week)
    who = backup["name"] if backup else "an unnamed backup"
    # "Lamar Jackson Questionable, DNP Wed/Thu — 75% weight"
    head = f"{starter} {status}" + (f", {sit['detail']}" if sit["detail"] else "")
    if weight < OUT_WEIGHT:
        head += f" — {round(weight * 100)}% weight"
    ctx = {"starter": starter, "starter_status": status,
           "starter_sit_weight": weight, "starter_sit_basis": sit["basis"],
           **({"starter_practice": (practice or {}).get("pattern")} if practice else {}),
           "backup": (backup or {}).get("name"),
           "backup_rank": rank, "backup_tier": tier, "backup_tier_source": source,
           "backup_sleeper_rank": sleeper_rank_of((backup or {}).get("name"), team,
                                                  sleeper_ranks),
           "with_starter_share": share,
           "sleeper_mult": 1.0, "model_mult": 1.0, "applied": False}
    if share == 0.0:
        ctx["reason"] = (f"{starter} ({status}) has not played in his trailing games — "
                         "his numbers are already the backup's")
        return ctx
    model = round(1.0 - (1.0 - mult) * (1.0 if share is None else share), 3)
    ctx.update(sleeper_mult=mult, model_mult=model, applied=mult < 1.0)
    if mult < 1.0:
        basis = {TIER_SOURCE_MARKET: f"market QB{rank}",
                 TIER_SOURCE_SLEEPER: f"Sleeper's QB{rank} this week",
                 TIER_SOURCE_NONE: "unranked by the market"}[source]
        ctx["reason"] = (f"{head}: {who} ({tier.replace('_', '-')} backup, "
                         f"{basis}) throwing: x{mult}")
    elif position == "TE":
        ctx["reason"] = (f"{head}: {who} throwing; tight ends hold their "
                         "volume with a backup (backtest), not cut")
    else:
        ctx["reason"] = f"{head}: {who} is a starter-grade backup, not cut"
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
