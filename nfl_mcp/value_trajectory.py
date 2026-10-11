"""Value trajectory: where a player's trade value is headed over the next few weeks.

A rest-of-season projection prices what a player will score; a trade is made
at what the *market* thinks he is worth. The market reads what he has been
producing, and the two drift apart in predictable ways -- the sell-high /
buy-low the tools used to miss.

The core read (``ros_rate``) compares two rates:

* **current**: what he has been producing, his trailing opportunity rate
  before any regression (``per_game_trailing``, from `ros`) -- the market's
  read of him;
* **future**: the mean of his blended ROS points over the next
  `FUTURE_WINDOW_GAMES` games he plays (``weekly_points``: Sleeper-first,
  ``ros.ROS_MODEL_WEIGHT`` x our model + the rest x Sleeper's projection for
  that week). Sleeper's later weeks already carry the depth chart -- a
  teammate back from injury, a starter's absence ending -- which our model
  alone priced from the rank prior: a backup with no games next to the
  returning starter fell back to the prior for his post-return rate, and most
  of the drop vanished (Emanuel Wilson, week 5 2026: Sleeper ~1.5-4 points a
  week once Charbonnet and Price are back, the old trajectory a hold).

Every player regresses from a trailing rate, so the change is read against
the position's norm: ``(future / current) / baseline - 1``, the baseline the
median of that ratio over the league pool (`position_baselines`; the
backtest's medians without a pool, ``DEFAULT_RATIO``), and sized by
``RATE_CHANGE_SCALE`` (backtest-calibrated, see the constants). The
structural reasons the old trajectory priced one by one -- a better teammate
back within the horizon (``returning_teammates``), a share of an absent
starter's volume ending (``inherited_per_game``) -- are what moves the
blended rate, so they are kept as the explanation of a ``ros_rate`` change,
not added to it. Without a current rate (fewer than ``MIN_TRAILING_GAMES``
games) or a future one, they are priced as before (the model's rates).

On top of the rate read, as before:

* a player coming back from a multi-week absence is cheap while he sits
  (``injury_return``);
* a role that just changed (``role_trend``, from `role_shift`) moves the
  market a week or two after the snaps and carries do;
* our blended rate can rank a player well above or below the market's
  positional rank (FantasyCalc, ``market_position_rank``);
* the report text (``news_flags``, `news_signals`): "benched", "gonna
  rotate", "out-snapped by" or "the lead back" is a role change the usage
  has not shown yet, and a player of his own "designated to return" from a
  reserve list is due back sooner than the reserve minimum says.

Each signal is a share of his current value expected to move within
`TRAJECTORY_HORIZON_GAMES` / `FUTURE_WINDOW_GAMES`. The rate read and his
own return are structural and add up. The role, news and market reads are
softer and correlated -- a role change is what moves our rate away from the
market's -- so they count once: the larger when they agree (plus
`SOFT_AGREEMENT_BONUS`), their sum when they disagree; and at
`SOFT_WITH_RATE_WEIGHT` when they point the way the rate read already does
(the same change, seen twice). Two corrections keep them honest:

* a role that grew while a teammate was out is the inflated role itself, not
  a rising one, and is not counted up;
* for a player whose role is rising a market rank above ours is the market
  being early, not him being overvalued, and is not counted down.

The total is ``expected_value_change.pct``. Past `TRAJECTORY_MIN_CHANGE`
either way the trajectory is ``rising`` (signal ``buy_low``) or ``falling``
(``sell_high``); otherwise ``stable`` / ``hold``. Pure: it reads the fields
`ros.ros_projections` attaches, no network.
"""
from __future__ import annotations

from bisect import bisect_right

from .injury_status import normalize as normalize_status
from .ros import SEASON_ENDING_WEEKS

# Reserve lists a player cannot practise from until his window opens.
RESERVE_STATUSES = {"IR", "PUP", "NFI"}

# How far ahead a change counts: a teammate back, an inherited role ending or
# a player of his own returning within this many games moves his value now.
TRAJECTORY_HORIZON_GAMES = 3
# Total expected change (share of value) that makes a trajectory rising or
# falling; below it the signals are reported but the call is "hold".
#
# Calibration (2026 week 5, the two live 12-team leagues, every rostered
# skill player): with the blended-rate read the share flagged is kept in
# 10-25% -- see the PR / CHANGELOG for the distribution. (Before it, on the
# model's rates: 19% / 20%, about 70% of them hard signals.)
TRAJECTORY_MIN_CHANGE = 0.10
# The blended rate of his next games (`future_rate`): this many games he is
# expected to play (byes and his own absence skipped) -- the backtest's
# horizon (the next four games).
FUTURE_WINDOW_GAMES = 4
# A current rate needs this many games in the trailing window.
MIN_TRAILING_GAMES = 2
# The rate read (`ros_rate`): ``(future / current) / baseline - 1``, counted
# from RATE_MIN_REL, capped at RATE_REL_CAP, scaled by the position's
# RATE_CHANGE_SCALE. Backtest (`evals/backtest/trend_calibration.py --only
# trajectory --cross-position`, 2023-25, 7,941 player-weeks; future = 0.25
# model + 0.75 Sleeper's week-W line, the leak-free stand-in for its later
# weeks): realised change over the next four games on the predicted one,
# through the origin, RB 0.94 / WR 0.97 / TE 0.85 / QB 1.14 (corr 0.38);
# binned, the realised median tracks the prediction up to about -40% / +40%
# and flattens past it (predicted -52%: realised median -38%), hence the cap.
RATE_MIN_REL = 0.20
RATE_MIN_POINTS = 2.5
RATE_MIN_REL_EXPLAINED = 0.10
RATE_MIN_CURRENT = 5.0
RATE_REL_CAP = 0.45
RATE_CHANGE_SCALE = {"RB": 0.95, "WR": 0.95, "TE": 0.85, "QB": 1.0}
# The same read on the rows with a better teammate back that week (1,966):
# RB 1.01, WR 0.89, TE 0.62 -- a tight end loses less to a returning pass
# catcher than the blend predicts. Used when a teammate back inside the
# horizon is the reason. (Replaces RETURNING_CHANGE_SCALE on this path.)
RETURNING_RATE_SCALE = {"RB": 1.0, "WR": 0.9, "TE": 0.6, "QB": 0.9}
# The future / current ratio's median per position in that backtest: the
# baseline when the league pool is too small to supply one.
DEFAULT_RATIO = {"QB": 0.88, "RB": 0.86, "WR": 0.86, "TE": 0.88}
# A rate change the market already prices (its positional rank is at or
# past our rank of his next games, the way the rate moves) counts at this
# share.
MARKET_PRICED_WEIGHT = 0.4
# A soft read (role, news, market gap) pointing the way the rate read does
# counts at this share: the rate already moved for the same reason.
SOFT_WITH_RATE_WEIGHT = 0.5
# Without a current or a future rate the structural signals are priced from
# the model's rates, as before the blended read. The returning-teammate one
# is read against the unregressed trailing rate (``per_game_recent``) --
# `evals/backtest/trend_calibration.py --only returning --cross-position`
# (2023-25, 2,006 player-weeks with the teammate back): realised drop 0.84 x
# the predicted one, RB 0.94, WR 0.76, TE 0.94 -- and scaled by the
# position's factor (QB: the pooled one).
RETURNING_CHANGE_SCALE = {"RB": 0.95, "WR": 0.75, "TE": 0.95, "QB": 0.85}
# A role that just moved (role_shift role_up / role_down), held for two
# games; one game counts for ROLE_ONE_WEEK_WEIGHT of it (a single game can be
# game script). Games he left injured are not part of the read (see
# `role_shift.injury_exit_weeks`). Corroborates; never a call on its own.
ROLE_SHIFT_CHANGE = 0.06
ROLE_ONE_WEEK_WEIGHT = 0.5
# Added when the role and the market gap point the same way.
SOFT_AGREEMENT_BONUS = 0.03
# A structural change smaller than this is not worth a reason line.
MIN_REPORTED_CHANGE = 0.02
# A player out for at least INJURY_RETURN_MIN_GAMES and due back inside the
# horizon: his value is depressed while he sits.
INJURY_RETURN_CHANGE = 0.10
INJURY_RETURN_MIN_GAMES = 2
# Our rank (blended rate of his next games among the pool at his position)
# against the market's positional rank. The gap counts once it is at least
# MARKET_GAP_MIN_RANKS places and MARKET_GAP_MIN_RATIO of the larger rank; it
# moves the value by MARKET_GAP_SCALE x that ratio, at most
# MARKET_GAP_MAX_CHANGE -- never a call alone.
MARKET_GAP_MIN_RANKS = 4
MARKET_GAP_MIN_RATIO = 0.25
MARKET_GAP_SCALE = 0.12
MARKET_GAP_MAX_CHANGE = 0.07
# News flags (`news_signals`, recency-weighted): benched / committee /
# snap_share_drop read as a shrinking role, lead_role as a growing one, at
# NEWS_ROLE_CHANGE x the flag's weight -- a soft signal in the role's place,
# never added to a role read the same way (the same change, seen twice).
# Kept below ROLE_SHIFT_CHANGE: the text is untested and one blurb is one
# quote.
NEWS_ROLE_CHANGE = 0.04
NEWS_ROLE_FLAGS = {"benched": -1.0, "committee": -1.0, "snap_share_drop": -1.0,
                   "lead_role": 1.0}
# A player of his own designated to return (practice window opened): back
# within this many games, whatever the reserve minimum still says. The same
# reading as `projections.DESIGNATED_RETURN_GAMES` for a teammate.
DESIGNATED_RETURN_GAMES = 2
# Fewer players than this at a position and a rank among them means nothing.
MARKET_GAP_MIN_POOL = 12
# Bounds on the summed change.
MAX_CHANGE = 0.5
# A season-ending absence: no trajectory to read.
SEASON_ENDING_GAMES = SEASON_ENDING_WEEKS

_SKILL = ("QB", "RB", "WR", "TE")
_VOLUME_LABELS = {"carries": "carries", "targets": "targets", "attempts": "pass attempts",
                  "receptions": "receptions"}


def future_rate(entry: dict, week: int | None = None,
                games: int = FUTURE_WINDOW_GAMES) -> tuple[float | None, list[int]]:
    """``(rate, weeks)``: the mean blended ROS points of his next `games`
    games after `week` (``weekly_points``), byes and his own absence
    skipped. ``(None, [])`` without weekly points."""
    weekly = entry.get("weekly_points") or {}
    skip = {int(w) for w in (entry.get("bye_weeks") or [])} \
        | {int(w) for w in (entry.get("injury_weeks") or [])} \
        | {int(w) for w in (entry.get("sleeper_absence_weeks") or [])}
    later = sorted((int(w), float(p or 0.0)) for w, p in weekly.items()
                   if (week is None or int(w) > int(week)) and int(w) not in skip)
    picked = [(w, p) for w, p in later if p > 0][:games]
    if not picked:
        return None, []
    return round(sum(p for _, p in picked) / len(picked), 2), [w for w, _ in picked]


def current_rate(entry: dict) -> float | None:
    """What he has been producing: the trailing opportunity rate
    (``per_game_trailing``) once he has `MIN_TRAILING_GAMES` games in it.
    None otherwise."""
    rate = entry.get("per_game_trailing")
    if rate is None or int(entry.get("trailing_games") or 0) < MIN_TRAILING_GAMES:
        return None
    return float(rate) if float(rate) > 0 else None


def position_baselines(pool: list[dict] | None, week: int | None = None) -> dict[str, float]:
    """``{position: median future / current ratio}`` over the pool, for
    positions with at least `MARKET_GAP_MIN_POOL` players to read."""
    ratios: dict[str, list[float]] = {}
    seen: set = set()
    for e in pool or []:
        key = e.get("player_id") or (e.get("player"), e.get("team"))
        pos = (e.get("position") or "").upper()
        if key in seen or pos not in _SKILL:
            continue
        seen.add(key)
        cur, (fut, _) = current_rate(e), future_rate(e, week)
        if cur and fut:
            ratios.setdefault(pos, []).append(fut / cur)
    out = {}
    for pos, xs in ratios.items():
        if len(xs) < MARKET_GAP_MIN_POOL:
            continue
        xs.sort()
        n = len(xs)
        out[pos] = round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2, 3)
    return out


def _rank_rate(e: dict, week: int | None) -> float | None:
    """The rate a player is ranked by: the blended rate of his next games,
    else the model's per-game ROS rate."""
    fut, _ = future_rate(e, week)
    if fut is not None:
        return fut
    return float(e["per_game"]) if e.get("per_game") is not None else None


def _rank_pool(pool: list[dict], week: int | None = None) -> dict[str, list[float]]:
    """``{position: [rate, ...] ascending}`` for positions with a pool
    big enough to rank against."""
    seen: set = set()
    by_pos: dict[str, list[float]] = {}
    for e in pool or []:
        key = e.get("player_id") or (e.get("player"), e.get("team"))
        pos = (e.get("position") or "").upper()
        rate = _rank_rate(e, week)
        if key in seen or pos not in _SKILL or rate is None:
            continue
        seen.add(key)
        by_pos.setdefault(pos, []).append(rate)
    return {p: sorted(v) for p, v in by_pos.items()
            if len(v) >= MARKET_GAP_MIN_POOL}


def our_rank(entry: dict, ranked: dict[str, list[float]], week: int | None = None) -> int | None:
    """His positional rank by the blended rate of his next games (else the
    per-game ROS rate) among the pool (himself counted once whether or not
    he is in it)."""
    values = ranked.get((entry.get("position") or "").upper())
    rate = _rank_rate(entry, week)
    if not values or rate is None:
        return None
    # Everyone in the pool strictly better than him, plus one.
    return len(values) - bisect_right(values, rate) + 1


def market_gap(entry: dict, rank: int | None) -> dict | None:
    """``{our_rank, market_rank, gap, read}``; gap > 0: we rank him better
    than the market does (``market_lower``), gap < 0: the market ranks him
    better (``market_higher``). None without both ranks."""
    market = entry.get("market_position_rank")
    if rank is None or not market:
        return None
    gap = int(market) - int(rank)
    ratio = gap / max(int(market), int(rank))
    significant = abs(gap) >= MARKET_GAP_MIN_RANKS and abs(ratio) >= MARKET_GAP_MIN_RATIO
    read = ("in_line" if not significant else "market_lower" if gap > 0 else "market_higher")
    return {"our_rank": int(rank), "market_rank": int(market), "gap": gap,
            "ratio": round(ratio, 2), "read": read}


def _volume_text(deflated: dict | None) -> str:
    parts = []
    for field, v in sorted((deflated or {}).items(),
                           key=lambda kv: -(kv[1].get("trailing", 0) - kv[1].get("with_teammate", 0))):
        label = _VOLUME_LABELS.get(field, field.replace("_", " "))
        parts.append(f"{label} {v.get('trailing', 0):.1f}→{v.get('with_teammate', 0):.1f}/game")
    return ", ".join(parts[:2])


def _two_week_role(flags: list[str]) -> bool:
    """Whether the role read held for two games: `role_shift` labels a
    two-game window "weeks a-b" and a one-game one "week a"."""
    return any("(weeks " in f for f in flags or [])


def assess(entry: dict, *, week: int | None = None, rank: int | None = None,
           baseline: float | None = None) -> dict:
    """The value trajectory of one ROS entry (see the module docstring).

    `baseline`: the position's median future / current ratio over the
    league pool (`position_baselines`); ``DEFAULT_RATIO`` without one.
    Returns ``{trajectory, signal, expected_value_change: {pct, per_game,
    ros_points}, change_week, market_gap, rates, reasons, signals}``.
    """
    name = entry.get("player") or entry.get("name") or "He"
    pos = (entry.get("position") or "").upper()
    team = entry.get("team") or ""
    per_game = float(entry.get("per_game") or 0.0)
    signals: list[dict] = []
    reasons: list[str] = []
    rate_change = 0.0
    change_week = None

    absent = int(entry.get("expected_absence_games") or 0)
    if absent >= SEASON_ENDING_GAMES:
        return {"trajectory": "stable", "signal": "hold",
                "expected_value_change": {"pct": 0.0, "per_game": 0.0, "ros_points": 0.0},
                "change_week": None, "market_gap": None, "rates": None,
                "reasons": [f"{name}: season-ending absence — no trade value to time"],
                "signals": []}

    cur = current_rate(entry)
    fut, fut_weeks = future_rate(entry, week)
    # Only with Sleeper in his later weeks: our model alone prices a teammate
    # back from the rank prior, the gap this read closes.
    blended = (cur is not None and fut is not None and cur >= RATE_MIN_CURRENT
               and entry.get("ros_source") in ("sleeper_blend", "mixed"))
    base = float(baseline or DEFAULT_RATIO.get(pos, 0.87))
    explain: list[str] = []  # the structural reasons behind a rate change

    # 1) A better teammate back within the horizon.
    absence_role: list[str] = []  # whose absence his recent role came from
    due = entry.get("returning_teammates") or []
    until = entry.get("per_game_until_return")
    returning_soon = False
    if due and until:
        absence_role = [r["name"] for r in due]
        # The first return inside the horizon is when the market re-prices
        # him, not the last one: a second teammate out longer (Price on IR)
        # must not hide the one back next week (Charbonnet).
        soon = [r for r in due
                if int(r.get("games_until_return") or 0) <= TRAJECTORY_HORIZON_GAMES]
        later = [r for r in due if r not in soon]
        # Against what he has been producing without them (the unregressed
        # trailing rate the market sees), not the ROS rate until the return.
        until = max(float(until), float(entry.get("per_game_recent") or 0.0))
        after = fut if blended else per_game
        change = (after - until) / until if until > 0 else 0.0
        if soon and (blended or abs(change) >= MIN_REPORTED_CHANGE):
            returning_soon = True
            games = min(int(r.get("games_until_return") or 0) for r in soon)
            weeks = [r.get("expected_return_week") for r in soon if r.get("expected_return_week")]
            change_week = min(weeks) if weeks else week
            who = ", ".join(
                f"{r['name']} ({team}{', ' + r['status'] if r.get('status') else ''})" for r in soon)
            when = ("back this week" if games == 0
                    else f"due back week {change_week}" if change_week else "due back soon")
            them = "them" if len(due) > 1 else "him"
            volume = _volume_text(entry.get("deflated_volume"))
            also = "; ".join(
                f"{r['name']} back later" + (f" (week {r['expected_return_week']})"
                                             if r.get("expected_return_week") else "")
                for r in later)
            text = (f"{who} {when} — {name}'s {until:.1f} pts/game came without {them}; "
                    f"{after:.1f} with {them}" + (f" ({volume})" if volume else "")
                    + (f"; {also}" if also else ""))
            if blended:
                explain.append(text)
                signals.append({"kind": "returning_teammate", "change": 0.0,
                                "explains": "ros_rate"})
            else:
                rate_change += per_game - until
                reasons.append(text)
                signals.append({"kind": "returning_teammate",
                                "change": round(RETURNING_CHANGE_SCALE.get(pos, 0.85) * change,
                                                3)})

    # 2) Inherited volume that ends inside the horizon.
    inherited = float(entry.get("inherited_per_game") or 0.0)
    inherited_games = int(entry.get("inherited_games") or 0)
    if inherited > 0:
        absence_role += list(entry.get("inherited_from") or [])
    if inherited > 0 and inherited_games <= TRAJECTORY_HORIZON_GAMES:
        total_rate = per_game + inherited
        change = -inherited / total_rate if total_rate > 0 else 0.0
        if abs(change) >= MIN_REPORTED_CHANGE:
            ends = (week + inherited_games) if week else None
            change_week = min(x for x in (change_week, ends) if x) if (change_week or ends) else None
            who = ", ".join(entry.get("inherited_from") or []) or "an absent starter"
            text = (f"{inherited:.1f} of his pts/game is {who}'s volume while out — ends after "
                    f"~{inherited_games} game(s)" + (f" (week {ends})" if ends else ""))
            if blended:
                explain.append(text)
                signals.append({"kind": "inherited_volume_ends", "change": 0.0,
                                "explains": "ros_rate"})
            else:
                rate_change -= inherited
                reasons.append(text)
                signals.append({"kind": "inherited_volume_ends", "change": round(change, 3)})

    # 1+2) The blended rate of his next games against what he has produced,
    #    relative to the position's norm. Sleeper's later weeks carry the
    #    teammate back / the starter's absence ending, so those explain it.
    rates = None
    if blended:
        rel = (fut / cur) / base - 1 if base > 0 else 0.0
        rel = max(-RATE_REL_CAP, min(RATE_REL_CAP, rel))
        scale = (RETURNING_RATE_SCALE if returning_soon else RATE_CHANGE_SCALE).get(pos, 0.9)
        rates = {"current": round(cur, 2), "future": fut, "future_weeks": fut_weeks,
                 "baseline_ratio": round(base, 3), "relative_change": round(rel, 3)}
        min_rel = RATE_MIN_REL_EXPLAINED if explain else RATE_MIN_REL
        min_pts = RATE_MIN_POINTS / 2 if explain else RATE_MIN_POINTS
        if abs(rel) >= min_rel and abs(fut - cur * base) >= min_pts:
            rate_change = fut - cur * base
            change_week = change_week or (fut_weeks[0] if fut_weeks else None)
            way = "down" if rel < 0 else "up"
            line = (f"{name}: {cur:.1f} pts/game so far, {fut:.1f} over his next "
                    f"{len(fut_weeks)} game(s) on the blended ROS (Sleeper-weighted) — "
                    f"{abs(rel):.0%} {way} on the {pos} norm")
            reasons.append(line + (" — " + "; ".join(explain) if explain else ""))
            signals.append({"kind": "ros_rate", "change": round(scale * rel, 3)})
        elif explain:
            reasons.extend(explain)

    # 3) His own return from a multi-week absence. A reserve player whose
    #    practice window is open is back sooner than the minimum says.
    #    A plain return to practice counts only for a reserve-list player
    #    (who cannot practise until his window opens); for anyone else it is
    #    this week's practice news, not a return (`news_signals`).
    news = {f.get("flag"): f for f in entry.get("news_flags") or []}
    cue = "designated_to_return" if "designated_to_return" in news else (
        "practice_progress" if "practice_progress" in news
        and normalize_status(entry.get("injury_status")) in RESERVE_STATUSES else None)
    if absent > TRAJECTORY_HORIZON_GAMES and cue:
        absent = DESIGNATED_RETURN_GAMES
        what = "designated to return" if cue == "designated_to_return" else "back at practice"
        reasons.append(f"{name} {what}: \"{news[cue].get('snippet', '')[:90]}\"")
    if INJURY_RETURN_MIN_GAMES <= absent <= TRAJECTORY_HORIZON_GAMES:
        back = entry.get("injury_weeks") or []
        back_week = (max(back) + 1) if back else (week + absent if week else None)
        reasons.append(
            f"{name} back from {entry.get('injury_status') or 'injury'} in ~{absent} game(s)"
            + (f" (week {back_week})" if back_week else "")
            + " — his value is depressed while he sits")
        signals.append({"kind": "injury_return", "change": INJURY_RETURN_CHANGE})
        change_week = change_week or back_week

    # 4) Soft: a role that just moved, and our rank against the market's.
    role = entry.get("role_trend")
    flags = list(entry.get("role_flags") or [])
    role_change = 0.0
    if role in ("role_up", "role_down"):
        sign = 1 if role == "role_up" else -1
        text = "; ".join(flags) or role.replace("_", " ")
        if sign > 0 and absence_role:
            # The bigger role *is* the absence: it goes when they are back.
            reasons.append(f"role grew while {', '.join(absence_role)} was out: {text}")
        else:
            role_change = sign * ROLE_SHIFT_CHANGE * (
                1.0 if _two_week_role(flags) else ROLE_ONE_WEEK_WEIGHT)
            reasons.append(f"role {'rising' if sign > 0 else 'shrinking'}: {text}")
            signals.append({"kind": role, "change": round(role_change, 3)})
    # The report text's role read: in place of a usage read, never on top of
    # one the same way.
    # (A lead role while a teammate is out is the absence again, not counted.)
    news_change = sum(sign * NEWS_ROLE_CHANGE * float(news[flag].get("weight") or 0.0)
                      for flag, sign in NEWS_ROLE_FLAGS.items()
                      if flag in news and not (sign > 0 and absence_role))
    news_change = max(-NEWS_ROLE_CHANGE, min(NEWS_ROLE_CHANGE, news_change))
    if news_change and role_change and (news_change > 0) == (role_change > 0):
        news_change = 0.0  # the usage already shows it
    if abs(news_change) >= MIN_REPORTED_CHANGE / 2:
        snippet = next((news[f].get("snippet", "") for f in NEWS_ROLE_FLAGS if f in news), "")
        reasons.append(f"news: {'growing' if news_change > 0 else 'shrinking'} role — "
                       f"\"{snippet[:90]}\"")
        signals.append({"kind": "news_role", "change": round(news_change, 3)})
        role_change += news_change

    gap = market_gap(entry, rank)
    gap_change = 0.0
    if gap and gap["read"] != "in_line":
        sign = 1 if gap["gap"] > 0 else -1
        if not (sign < 0 and role_change > 0):
            # (A rising role: our trailing rate has not caught up with it, so a
            # market rank above ours is the market being early.)
            gap_change = sign * min(MARKET_GAP_MAX_CHANGE, MARKET_GAP_SCALE * abs(gap["ratio"]))
            reasons.append(
                f"market ranks him {pos}{gap['market_rank']}, his blended rate ranks "
                f"{pos}{gap['our_rank']} — the market values him "
                f"{'lower' if sign > 0 else 'higher'} than we do")
            signals.append({"kind": "market_gap", "change": round(gap_change, 3)})
    if role_change and gap_change and (role_change > 0) == (gap_change > 0):
        soft = max(role_change, gap_change, key=abs)
        soft += SOFT_AGREEMENT_BONUS if soft > 0 else -SOFT_AGREEMENT_BONUS
    else:
        soft = role_change + gap_change

    # A rate change the market has already priced: it ranks him where his
    # blended rate does (or past it) the way the rate is moving -- a "rise"
    # for a player the market already ranks at or above our rank of his
    # next games, a "drop" for one it already ranks at or below it.
    rate_sig = next((s for s in signals if s["kind"] == "ros_rate"), None)
    if rate_sig and gap and (gap["gap"] <= 0 if rate_sig["change"] > 0 else gap["gap"] >= 0):
        rate_sig["change"] = round(rate_sig["change"] * MARKET_PRICED_WEIGHT, 3)
        rate_sig["market_priced"] = True
        reasons.append(f"the market already ranks him {pos}{gap['market_rank']} (his blended "
                       f"rate: {pos}{gap['our_rank']}) — the rate change is partly priced in")
    structural = sum(s["change"] for s in signals
                     if s["kind"] not in ("role_up", "role_down", "market_gap", "news_role"))
    rate_read = rate_sig["change"] if rate_sig else 0.0
    if soft and rate_read and (soft > 0) == (rate_read > 0):
        soft *= SOFT_WITH_RATE_WEIGHT  # the rate already moved for it
    total = max(-MAX_CHANGE, min(MAX_CHANGE, structural + soft))
    if total >= TRAJECTORY_MIN_CHANGE:
        trajectory, signal = "rising", "buy_low"
    elif total <= -TRAJECTORY_MIN_CHANGE:
        trajectory, signal = "falling", "sell_high"
    else:
        trajectory, signal = "stable", "hold"
    # The ROS points the rate change is worth: the games he is counted for
    # from the change on, at the difference.
    weekly = entry.get("weekly_points") or {}
    games_after = sum(1 for w, pts in weekly.items()
                      if pts and (change_week is None or int(w) >= change_week))
    return {
        "trajectory": trajectory,
        "signal": signal,
        "expected_value_change": {
            "pct": round(100 * total, 1),
            "per_game": round(rate_change, 2),
            "ros_points": round(rate_change * games_after, 1),
        },
        "change_week": change_week,
        "market_gap": gap,
        "rates": rates,
        "reasons": reasons,
        "signals": signals,
    }


def annotate(entries: list[dict], *, week: int | None = None,
             pool: list[dict] | None = None) -> dict[str, dict]:
    """Attach ``value_trajectory`` to each ROS entry; ranks and the
    positions' rate baselines are read against `pool` (every rostered player
    in the league, ideally) plus the entries themselves. Returns
    ``{player_id: trajectory}``."""
    everyone = list(pool or []) + list(entries)
    ranked = _rank_pool(everyone, week)
    baselines = position_baselines(everyone, week)
    out: dict[str, dict] = {}
    for e in entries:
        pos = (e.get("position") or "").upper()
        if pos not in _SKILL:
            continue
        t = assess(e, week=week, rank=our_rank(e, ranked, week), baseline=baselines.get(pos))
        e["value_trajectory"] = t
        if e.get("player_id"):
            out[str(e["player_id"])] = t
    return out


def compact(t: dict | None) -> dict | None:
    """The fields a trade proposal carries per player."""
    if not t:
        return None
    return {"trajectory": t["trajectory"], "signal": t["signal"],
            "expected_value_change_pct": t["expected_value_change"]["pct"],
            "reasons": t["reasons"]}


def side_notes(gives: list[dict], gets: list[dict]) -> list[str]:
    """Plain-language timing notes for one side of a trade. `gives` / `gets`
    are dicts with ``name`` and ``value_trajectory``."""
    notes = []
    for p in gives:
        t = p.get("value_trajectory") or {}
        why = f" ({t['reasons'][0]})" if t.get("reasons") else ""
        if t.get("signal") == "sell_high":
            notes.append(f"You are selling high on {p['name']}{why}.")
        elif t.get("signal") == "buy_low":
            notes.append(f"You are selling low on {p['name']} — his value is rising{why}.")
    for p in gets:
        t = p.get("value_trajectory") or {}
        why = f" ({t['reasons'][0]})" if t.get("reasons") else ""
        if t.get("signal") == "buy_low":
            notes.append(f"You are buying low on {p['name']}{why}.")
        elif t.get("signal") == "sell_high":
            notes.append(f"You are buying high on {p['name']} — his value is falling{why}.")
    return notes


def timing_score(gives: list[dict], gets: list[dict]) -> int:
    """Net count of well-timed pieces: +1 for each sell_high given or buy_low
    received, -1 for each buy_low given or sell_high received."""
    score = 0
    for p in gives:
        s = (p.get("value_trajectory") or {}).get("signal")
        score += 1 if s == "sell_high" else -1 if s == "buy_low" else 0
    for p in gets:
        s = (p.get("value_trajectory") or {}).get("signal")
        score += 1 if s == "buy_low" else -1 if s == "sell_high" else 0
    return score


__all__ = ["annotate", "assess", "compact", "current_rate", "future_rate", "market_gap",
           "our_rank", "position_baselines", "side_notes", "timing_score"]
