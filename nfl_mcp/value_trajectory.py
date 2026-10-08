"""Value trajectory: where a player's trade value is headed over the next few weeks.

A rest-of-season projection prices what a player will score; a trade is made
at what the *market* thinks he is worth. The two drift apart in predictable
ways, and a trade made on the gap is the sell-high / buy-low the tools used to
miss:

* a backup whose recent games came while a better teammate was out
  (``returning_teammates``) keeps that rate only until the teammate is back —
  his market value is still the inflated one (Warren's 90% carries share
  without Dowdle);
* a share of an absent starter's volume (``inherited_per_game``) ends with the
  starter's absence;
* a player coming back from a multi-week absence is cheap while he sits;
* a role that just changed (``role_trend``, from `role_shift`) moves the
  market a week or two after the snaps and carries do;
* our per-game rate can rank a player well above or below the market's
  positional rank (FantasyCalc, ``market_position_rank``);
* the report text (``news_flags``, `news_signals`): "benched", "gonna
  rotate" or "the lead back" is a role change the usage has not shown yet,
  and a player of his own "designated to return" from a reserve list is due
  back sooner than the reserve minimum says.

Each signal is a share of his current value expected to move within
`TRAJECTORY_HORIZON_GAMES`. The first three are structural (a date, a rate
before and after) and add up. The last two are softer and correlated -- a
role change is what moves our rate away from the market's -- so they count
once: the larger of the two when they agree (plus `SOFT_AGREEMENT_BONUS`),
their sum when they disagree. Two corrections keep them honest:

* a role that grew while a teammate was out is the inflated role itself, not
  a rising one, and is not counted up;
* our rate does not price a gained role (`role_shift` multiplies only a lost
  one), so for a player whose role is rising a market rank above ours is the
  market being early, not him being overvalued, and is not counted down.

The total is ``expected_value_change.pct``. Past `TRAJECTORY_MIN_CHANGE`
either way the trajectory is ``rising`` (signal ``buy_low``) or ``falling``
(``sell_high``); otherwise ``stable`` / ``hold``. Pure: it reads the fields
`ros.ros_projections` attaches, no network.
"""
from __future__ import annotations

from bisect import bisect_right

from .ros import SEASON_ENDING_WEEKS

# How far ahead a change counts: a teammate back, an inherited role ending or
# a player of his own returning within this many games moves his value now.
TRAJECTORY_HORIZON_GAMES = 3
# Total expected change (share of value) that makes a trajectory rising or
# falling; below it the signals are reported but the call is "hold".
#
# Calibration (2026 week 5, two live 12-team leagues): the soft signals are
# sized so that neither makes a call alone -- only a role shift and a market
# gap that agree do -- and the hard ones (a teammate back, inherited volume
# ending, his own return) carry most calls. Before: 31% / 32% of rostered
# players flagged, most on a role shift alone; after: 15% / 15%, about
# two thirds of them hard signals. With a teammate's drop read from the
# recent (unregressed) rate and the first return gating it: 19% / 20%,
# about 70% of them hard.
TRAJECTORY_MIN_CHANGE = 0.08
# A role that just moved (role_shift role_up / role_down), held for two
# games; one game counts for ROLE_ONE_WEEK_WEIGHT of it (a single game can be
# game script). Games he left injured are not part of the read (see
# `role_shift.injury_exit_weeks`). Corroborates; never a call on its own.
ROLE_SHIFT_CHANGE = 0.06
ROLE_ONE_WEEK_WEIGHT = 0.5
# Added when the role and the market gap point the same way.
SOFT_AGREEMENT_BONUS = 0.02
# A structural change smaller than this is not worth a reason line.
MIN_REPORTED_CHANGE = 0.02
# A player out for at least INJURY_RETURN_MIN_GAMES and due back inside the
# horizon: his value is depressed while he sits.
INJURY_RETURN_CHANGE = 0.10
INJURY_RETURN_MIN_GAMES = 2
# Our rank (per-game ROS rate among the pool at his position) against the
# market's positional rank. The gap counts once it is at least
# MARKET_GAP_MIN_RANKS places and MARKET_GAP_MIN_RATIO of the larger rank; it
# moves the value by MARKET_GAP_SCALE x that ratio, at most
# MARKET_GAP_MAX_CHANGE -- never a call alone; with a role shift the same way
# from a gap of about half the rank (our RB12 vs the market's RB24).
MARKET_GAP_MIN_RANKS = 4
MARKET_GAP_MIN_RATIO = 0.25
MARKET_GAP_SCALE = 0.12
MARKET_GAP_MAX_CHANGE = 0.07
# News flags (`news_signals`, recency-weighted): benched / committee read as
# a shrinking role, lead_role as a growing one, at NEWS_ROLE_CHANGE x the
# flag's weight -- a soft signal in the role's place, never added to a role
# read the same way (the same change, seen twice). Kept below
# ROLE_SHIFT_CHANGE: the text is untested and one blurb is one quote.
NEWS_ROLE_CHANGE = 0.04
NEWS_ROLE_FLAGS = {"benched": -1.0, "committee": -1.0, "lead_role": 1.0}
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


def _rank_pool(pool: list[dict]) -> dict[str, list[float]]:
    """``{position: [per_game, ...] ascending}`` for positions with a pool
    big enough to rank against."""
    seen: set = set()
    by_pos: dict[str, list[float]] = {}
    for e in pool or []:
        key = e.get("player_id") or (e.get("player"), e.get("team"))
        pos = (e.get("position") or "").upper()
        if key in seen or pos not in _SKILL or e.get("per_game") is None:
            continue
        seen.add(key)
        by_pos.setdefault(pos, []).append(float(e["per_game"]))
    return {p: sorted(v) for p, v in by_pos.items()
            if len(v) >= MARKET_GAP_MIN_POOL}


def our_rank(entry: dict, ranked: dict[str, list[float]]) -> int | None:
    """His positional rank by per-game ROS rate among the pool (himself
    counted once whether or not he is in it)."""
    values = ranked.get((entry.get("position") or "").upper())
    if not values or entry.get("per_game") is None:
        return None
    # Everyone in the pool strictly better than him, plus one.
    return len(values) - bisect_right(values, float(entry["per_game"])) + 1


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


def assess(entry: dict, *, week: int | None = None, rank: int | None = None) -> dict:
    """The value trajectory of one ROS entry (see the module docstring).

    Returns ``{trajectory, signal, expected_value_change: {pct, per_game,
    ros_points}, change_week, market_gap, reasons, signals}``.
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
                "change_week": None, "market_gap": None,
                "reasons": [f"{name}: season-ending absence — no trade value to time"],
                "signals": []}

    # 1) A better teammate back within the horizon.
    absence_role: list[str] = []  # whose absence his recent role came from
    due = entry.get("returning_teammates") or []
    until = entry.get("per_game_until_return")
    if due and until:
        absence_role = [r["name"] for r in due]
        # The first return inside the horizon is when the market re-prices
        # him, not the last one: a second teammate out longer (Price on IR)
        # must not hide the one back next week (Charbonnet).
        soon = [r for r in due
                if int(r.get("games_until_return") or 0) <= TRAJECTORY_HORIZON_GAMES]
        later = [r for r in due if r not in soon]
        # Against what he has been producing without them (the unregressed
        # trailing rate the market sees), not the ROS rate until the return:
        # that one is regressed toward the rank prior, and for a backup with
        # no games next to the starter the post-return rate is that same
        # prior -- post against post, no visible drop.
        until = max(float(until), float(entry.get("per_game_recent") or 0.0))
        change = (per_game - until) / until if until > 0 else 0.0
        if soon and abs(change) >= MIN_REPORTED_CHANGE:
            games = min(int(r.get("games_until_return") or 0) for r in soon)
            rate_change += per_game - until
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
            reasons.append(
                f"{who} {when} — {name}'s {until:.1f} pts/game came without {them}; "
                f"{per_game:.1f} with {them}" + (f" ({volume})" if volume else "")
                + (f"; {also}" if also else ""))
            signals.append({"kind": "returning_teammate", "change": round(change, 3)})

    # 2) Inherited volume that ends inside the horizon.
    inherited = float(entry.get("inherited_per_game") or 0.0)
    inherited_games = int(entry.get("inherited_games") or 0)
    if inherited > 0:
        absence_role += list(entry.get("inherited_from") or [])
    if inherited > 0 and inherited_games <= TRAJECTORY_HORIZON_GAMES:
        base = per_game + inherited
        change = -inherited / base if base > 0 else 0.0
        if abs(change) >= MIN_REPORTED_CHANGE:
            rate_change -= inherited
            ends = (week + inherited_games) if week else None
            change_week = min(x for x in (change_week, ends) if x) if (change_week or ends) else None
            who = ", ".join(entry.get("inherited_from") or []) or "an absent starter"
            reasons.append(
                f"{inherited:.1f} of his pts/game is {who}'s volume while out — ends after "
                f"~{inherited_games} game(s)" + (f" (week {ends})" if ends else ""))
            signals.append({"kind": "inherited_volume_ends", "change": round(change, 3)})

    # 3) His own return from a multi-week absence. A reserve player whose
    #    practice window is open is back sooner than the minimum says.
    news = {f.get("flag"): f for f in entry.get("news_flags") or []}
    if absent > TRAJECTORY_HORIZON_GAMES and "designated_to_return" in news:
        absent = DESIGNATED_RETURN_GAMES
        reasons.append(f"{name} designated to return: \"{news['designated_to_return'].get('snippet', '')[:90]}\"")
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
                f"market ranks him {pos}{gap['market_rank']}, his ROS rate ranks "
                f"{pos}{gap['our_rank']} — the market values him "
                f"{'lower' if sign > 0 else 'higher'} than we do")
            signals.append({"kind": "market_gap", "change": round(gap_change, 3)})
    if role_change and gap_change and (role_change > 0) == (gap_change > 0):
        soft = max(role_change, gap_change, key=abs)
        soft += SOFT_AGREEMENT_BONUS if soft > 0 else -SOFT_AGREEMENT_BONUS
    else:
        soft = role_change + gap_change

    structural = sum(s["change"] for s in signals
                     if s["kind"] not in ("role_up", "role_down", "market_gap", "news_role"))
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
        "reasons": reasons,
        "signals": signals,
    }


def annotate(entries: list[dict], *, week: int | None = None,
             pool: list[dict] | None = None) -> dict[str, dict]:
    """Attach ``value_trajectory`` to each ROS entry; ranks are read against
    `pool` (every rostered player in the league, ideally) or the entries
    themselves. Returns ``{player_id: trajectory}``."""
    ranked = _rank_pool(list(pool or []) + list(entries))
    out: dict[str, dict] = {}
    for e in entries:
        if (e.get("position") or "").upper() not in _SKILL:
            continue
        t = assess(e, week=week, rank=our_rank(e, ranked))
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


__all__ = ["annotate", "assess", "compact", "market_gap", "our_rank", "side_notes",
           "timing_score"]
