"""What a league's waiver settings actually mean for "can I add him now?".

Sleeper exposes the raw knobs (``waiver_type``, ``daily_waivers``,
``waiver_day_of_week``, ``waiver_clear_days``, ``daily_waivers_days``) and
nothing that answers the only question a manager has before kickoff: is this an
instant add, or a claim that processes later — and when?

This module reports the settings in interpreted form and stops there. It does
not answer "instant or claim?", because that was attempted twice in live use
and was wrong both times:

1. "Never dropped, therefore not on waivers, therefore instant" — false under
   weekly waivers, where the whole free-agent pool is locked during the week.
2. "`daily_waivers=1`, therefore instant" — also false; the league's own app
   showed the claim processing on the waiver day anyway. Which days daily
   waivers actually run is encoded in `daily_waivers_days` as a bitmask, and
   the observed processing time (~09:05 CEST) matches none of the exposed
   fields.

So the honest output is the configuration plus a pointer to the app, which
shows the real answer per player. Inventing the verdict here is what caused the
wrong advice in the first place.

Both failures had the same root: reading the verdict off the settings alone.
What decides it per player is the game lock and the drop, and those — checked
against both leagues' processed claims — are modelled in `waiver_status`, with
every time marked as an estimate and the app still the authority. On top of it
`priority_strategy` gives rolling-waiver advice (claim now / wait / leave him),
shared by get_waiver_targets and recommend_faab_bid so the two never disagree
about the same player.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .game_clock import local_time

# Sleeper's `waiver_day_of_week`, verified against a league whose app shows
# Wednesday for the value 2.
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]

FAAB = "faab"
PRIORITY = "priority"


def waiver_rules(league: dict | None) -> dict:
    """Interpret a league's waiver configuration.

    Returns:
        waiver_type: "faab" or "priority"
        processing: "daily" or "weekly"
        waiver_day: weekday name for weekly processing, else None
        clear_days: days a dropped player stays on waivers
        note: what the configuration implies, and what it does not settle
        how_to_confirm: where the authoritative answer lives

    Deliberately does NOT return an "instant vs claim" verdict — see the
    comment at the return statement.
    """
    settings = (league or {}).get("settings") or {}
    budget = settings.get("waiver_budget") or 0
    is_faab = settings.get("waiver_type") == 2 and budget > 0
    daily = bool(settings.get("daily_waivers"))
    day_index = settings.get("waiver_day_of_week")
    day = _WEEKDAYS[day_index] if isinstance(day_index, int) and 0 <= day_index < 7 else None

    if daily:
        note = (
            "Daily waivers are enabled, but which days they run is encoded in "
            f"`daily_waivers_days` ({settings.get('daily_waivers_days')}) as a "
            "bitmask this module does not decode. An add may still be a claim "
            f"processed on {day or 'the waiver day'}."
        )
    else:
        note = (
            "Weekly waivers: once a player's game kicks off he is locked on "
            f"waivers until the {day or 'waiver day'} run, so from Sunday to that "
            "run nearly every add is a claim — being unowned all season does not "
            "make a player an instant add. See each player's `waiver_strategy`."
        )

    return {
        "waiver_type": FAAB if is_faab else PRIORITY,
        "processing": "daily" if daily else "weekly",
        "waiver_day": day,
        "clear_days": settings.get("waiver_clear_days"),
        "budget": budget if is_faab else None,
        "daily_waivers_days": settings.get("daily_waivers_days") if daily else None,
        "note": note,
        # Deliberately no `add_mode` field. Deriving "instant vs claim" from
        # these settings was tried twice in live use and was wrong both times —
        # once by reasoning "never dropped, therefore instant" (false under
        # weekly waivers), once by reading `daily_waivers=1` as instant (the
        # league's own app showed the claim processing on the waiver day
        # regardless). Asserting it here would reproduce that error inside the
        # fix meant to prevent it.
        "how_to_confirm": (
            "Whether a specific add is instant or a claim — and when a claim "
            "processes — is shown in the league app on the add dialog and under "
            "pending claims. Neither is derivable from these settings, and "
            "pending claims are not exposed by the API at all, so their absence "
            "from a transaction list does not mean none exist."
        ),
    }


# ---------------------------------------------------------------------------
# When a claim processes, and what that means for priority
# ---------------------------------------------------------------------------
#
# Observed in both leagues' processed claims (Sept 2026): waivers run every
# day at about midnight US Pacific (06:20-07:10 UTC, ~09:00 Vienna), whether or
# not `daily_waivers` is set, and `daily_waivers_hour` (0 in both) matches that
# hour in Pacific time. What a run holds:
#
# - a dropped player clears on the run `waiver_clear_days` calendar days (US
#   Pacific) after the drop: dropped Wednesday 00:34 PT with 2 clear days, he
#   cleared in Friday's run;
# - a player whose game has kicked off is locked on waivers until the next run
#   on `waiver_day_of_week` — last week's game counts until that run;
# - everyone else is a free agent, an instant add (free_agent transactions
#   appear in both leagues between runs, daily waivers or not).
#
# Every time here is an estimate to within the run's own spread, which never
# matters against a kickoff. `daily_waivers_days` is still not decoded; if it
# adds earlier runs for game-locked players, a claim lands sooner than stated.
SLEEPER_TZ = ZoneInfo("America/Los_Angeles")

# Trending-add rank (Sleeper's league-agnostic list, most added first) that
# marks a player as contested. Same cut-offs as the FAAB demand multiplier.
_HIGH_DEMAND_RANK = 10
_MODERATE_DEMAND_RANK = 30

CLAIM_NOW = "claim_now"
ADD_NOW = "add_now"
WAIT = "wait"
DONT_BOTHER = "dont_bother"


def _run_hour(settings: dict) -> int:
    hour = settings.get("daily_waivers_hour")
    return hour if isinstance(hour, int) and 0 <= hour < 24 else 0


def _run_on(day, hour: int) -> datetime:
    """The waiver run on a US-Pacific calendar day, in UTC."""
    return datetime.combine(day, time(hour), tzinfo=SLEEPER_TZ).astimezone(UTC)


def next_run(league: dict | None, after: datetime, weekly: bool = False) -> datetime:
    """The first waiver run strictly after `after` — any day, or only on the
    league's waiver day with ``weekly``."""
    settings = (league or {}).get("settings") or {}
    hour = _run_hour(settings)
    wday = settings.get("waiver_day_of_week")
    wday = wday if isinstance(wday, int) and 0 <= wday < 7 else 2
    day = after.astimezone(SLEEPER_TZ).date()
    for _ in range(15):
        run = _run_on(day, hour)
        if run > after and (not weekly or day.weekday() == wday):
            return run
        day += timedelta(days=1)
    return _run_on(day, hour)  # unreachable with a valid weekday


def waiver_status(
    league: dict | None,
    *,
    kickoff: datetime | None = None,
    previous_kickoff: datetime | None = None,
    dropped_at: datetime | None = None,
    now: datetime | None = None,
) -> dict:
    """Whether an unrostered player is on waivers now, and until when.

    ``kickoff`` is his game this week, ``previous_kickoff`` last week's (both
    lock him until the next waiver day once started), ``dropped_at`` his
    latest drop.

    Returns ``{on_waivers, reason, clears_at, clears_at_local, instant_add,
    claim_processes_at, claim_processes_at_local, in_time_for_kickoff,
    estimated}``. ``claim_processes_at`` is when an add actually lands — now
    for a free agent — and ``in_time_for_kickoff`` whether that is before this
    week's game (None without a kickoff, False once it has started).
    """
    now = now or datetime.now(UTC)
    settings = (league or {}).get("settings") or {}
    holds: list[tuple[datetime, str]] = []
    for game, label in ((previous_kickoff, "last week's game"), (kickoff, "his game")):
        if game is not None and game <= now:
            clears = next_run(league, game, weekly=True)
            if clears > now:
                holds.append((clears, f"locked on waivers since {label} kicked off, "
                                      "until the waiver-day run"))
    if dropped_at is not None:
        clear_days = settings.get("waiver_clear_days")
        clear_days = clear_days if isinstance(clear_days, int) and clear_days >= 0 else 2
        drop_day = dropped_at.astimezone(SLEEPER_TZ).date()
        clears = _run_on(drop_day + timedelta(days=clear_days), _run_hour(settings))
        if clears <= dropped_at:
            clears = next_run(league, dropped_at)
        if clears > now:
            holds.append((clears, f"dropped {local_time(dropped_at)[:16]}, on waivers "
                                  f"for {clear_days} clear day(s)"))
    # Without either kickoff the game lock cannot be ruled out, so "free
    # agent" would be a guess — the first failure mode in the module docstring.
    known = bool(holds) or kickoff is not None or previous_kickoff is not None
    if holds:
        clears_at, reason = max(holds)
        processes = clears_at
    elif known:
        clears_at, reason, processes = None, "free agent — no waiver period running", now
    else:
        clears_at, processes = None, None
        reason = "no cached kickoffs — whether he is locked on waivers is unknown; check the app"
    if kickoff is None or processes is None:
        in_time = False if kickoff is not None and kickoff <= now else None
    elif kickoff <= now:
        in_time = False
    else:
        in_time = processes < kickoff
    return {
        "on_waivers": bool(holds) if known else None,
        "reason": reason,
        "clears_at": clears_at.isoformat() if clears_at else None,
        "clears_at_local": local_time(clears_at),
        "instant_add": known and not holds,
        "claim_processes_at": processes.isoformat() if processes else None,
        "claim_processes_at_local": local_time(processes),
        "in_time_for_kickoff": in_time,
        "estimated": True,
    }


def trend_demand(rank: int | None) -> str:
    """How contested a player is from his trending-add rank (0 = most added):
    "high", "moderate", "light", or "low" when he is not trending at all.

    League-agnostic — Sleeper publishes no per-league rostered share — so it
    says how many managers everywhere want him, a proxy for your league.
    """
    if rank is None:
        return "low"
    if rank < _HIGH_DEMAND_RANK:
        return "high"
    if rank < _MODERATE_DEMAND_RANK:
        return "moderate"
    return "light"


# What a claim adds to your best lineup, in points. This week: the same 3.0 /
# 1.5 bars get_waiver_targets has always used (1.5 is inside a weekly
# projection's noise, 3.0 is well clear of it). Rest of season: the gain per
# remaining week, byes and injuries covered by whoever is next best.
WEEK_HIGH_GAIN = 3.0
WEEK_MEDIUM_GAIN = 1.5
ROS_HIGH_GAIN_PER_WEEK = 2.0
ROS_MEDIUM_GAIN_PER_WEEK = 0.75

_WORTH_RANK = {"low": 0, "medium": 1, "high": 2}


def sits_out_week(entry: dict, week: int) -> dict:
    """A ROS entry with ``week`` zeroed: a reserve (IR) player belongs in the
    rest-of-season roster — his ROS models the absence and the return — but
    cannot hold a slot in this week's lineup."""
    weekly = dict(entry.get("weekly_points") or {})
    if week in weekly:
        weekly[week] = 0.0
    return {**entry, "weekly_points": weekly}


def _gain_level(gain: float | None, high: float, medium: float) -> str | None:
    if gain is None:
        return None
    return "high" if gain >= high else "medium" if gain >= medium else "low"


def horizon_worth(week_gain: float | None, ros_gain: float | None,
                  ros_weeks: int | None) -> dict:
    """How much a claim is worth, judged on both horizons at once.

    get_waiver_targets used to judge this week only and recommend_faab_bid the
    season only, so the same player could be "claim now" in one and "wait" in
    the other. Both now call this: ``worth`` is the better of the two
    horizons, and ``this_week_only`` is True when that worth rests on this
    week's game alone — the only case in which a claim that lands after his
    kickoff is worth nothing (``priority_strategy(this_week=...)``).

    Returns ``{worth, week_gain, week_worth, ros_gain, ros_gain_per_week,
    ros_weeks, ros_worth, this_week_only, driven_by}``; ``worth`` is None
    when neither gain is known.
    """
    per_week = (round(ros_gain / ros_weeks, 2)
                if ros_gain is not None and ros_weeks else None)
    week_worth = _gain_level(week_gain, WEEK_HIGH_GAIN, WEEK_MEDIUM_GAIN)
    ros_worth = _gain_level(per_week, ROS_HIGH_GAIN_PER_WEEK, ROS_MEDIUM_GAIN_PER_WEEK)
    known = [w for w in (week_worth, ros_worth) if w is not None]
    worth = max(known, key=_WORTH_RANK.__getitem__) if known else None
    this_week_only = ros_worth is None or (
        week_worth is not None and _WORTH_RANK[ros_worth] < _WORTH_RANK[week_worth])
    if worth is None:
        driven_by = None
    elif ros_worth is None:
        driven_by = "this_week"
    elif week_worth is None:
        driven_by = "rest_of_season"
    elif _WORTH_RANK[week_worth] == _WORTH_RANK[ros_worth]:
        driven_by = "both"
    else:
        driven_by = "this_week" if this_week_only else "rest_of_season"
    return {
        "worth": worth,
        "week_gain": week_gain,
        "week_worth": week_worth,
        "ros_gain": ros_gain,
        "ros_gain_per_week": per_week,
        "ros_weeks": ros_weeks,
        "ros_worth": ros_worth,
        "this_week_only": this_week_only,
        "driven_by": driven_by,
    }


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def priority_strategy(
    league: dict | None,
    roster: dict | None,
    *,
    worth: str,
    demand: str,
    kickoff: datetime | None = None,
    previous_kickoff: datetime | None = None,
    dropped_at: datetime | None = None,
    now: datetime | None = None,
    this_week: bool = True,
) -> dict | None:
    """Claim now, wait for free agency, or leave him — for a non-FAAB league.

    ``worth`` is "high"/"medium"/"low" (how much the player helps you),
    ``demand`` a `trend_demand` label. ``this_week`` says whether the value is
    this week's game (get_waiver_targets) or rest-of-season
    (recommend_faab_bid): only the former is lost when the add lands after
    his kickoff.

    Under rolling waivers (``waiver_type`` 0) a successful claim sends you to
    the back of the order, so its cost is the places you fall; a failed one
    costs nothing. Returns None for a FAAB league.

    Returns ``{recommendation, reason, waiver_position, teams_ahead,
    num_teams, rolling, claim_cost_places, wait_days, demand, availability}``
    with recommendation one of ``claim_now``, ``add_now`` (free agent, no
    priority needed), ``wait`` (let him clear, then add him as a free agent)
    and ``dont_bother``.
    """
    if waiver_rules(league)["waiver_type"] == FAAB:
        return None
    now = now or datetime.now(UTC)
    settings = (league or {}).get("settings") or {}
    rolling = settings.get("waiver_type") == 0
    num_teams = int((league or {}).get("total_rosters") or 0) or None
    position = ((roster or {}).get("settings") or {}).get("waiver_position")
    position = position if isinstance(position, int) and position > 0 else None
    teams_ahead = position - 1 if position else None
    # Places you fall if the claim succeeds: none outside rolling waivers,
    # none when you are already last, unknown without a position.
    if not rolling:
        cost = 0
    elif position and num_teams:
        cost = max(0, num_teams - position)
    else:
        cost = None

    status = waiver_status(league, kickoff=kickoff, previous_kickoff=previous_kickoff,
                           dropped_at=dropped_at, now=now)
    wait_days = None
    wait_for = "until he clears"
    if status["clears_at"]:
        clears = datetime.fromisoformat(status["clears_at"])
        hours = max(0.0, (clears - now).total_seconds() / 3600)
        wait_days = math.ceil(hours / 24)
        span = f"about {max(1, round(hours))} hour(s)" if hours < 24 else f"{wait_days} day(s)"
        wait_for = f"{span} until he clears ({status['clears_at_local'][:16]} Vienna)"
    contested = demand in ("high", "moderate")
    who = (f"you are {_ordinal(position)} of {num_teams} ({teams_ahead} team(s) ahead)"
           if position and num_teams else "your waiver position is unknown")
    if not rolling:
        spend = ""
    elif cost == 0:
        spend = " — costs nothing, you are already last"
    elif cost:
        spend = f" — a successful claim drops you {cost} place(s) to the back"
    else:
        spend = " — a successful claim sends you to the back"

    def result(rec: str, why: str) -> dict:
        return {
            "recommendation": rec,
            "reason": why,
            "waiver_position": position,
            "teams_ahead": teams_ahead,
            "num_teams": num_teams,
            "rolling": rolling,
            "claim_cost_places": cost,
            "wait_days": wait_days or 0,
            "demand": demand,
            "availability": status,
        }

    # A one-week pickup whose add lands after his kickoff is worth nothing
    # this week, however good the matchup.
    if this_week and status["in_time_for_kickoff"] is False:
        started = kickoff is not None and kickoff <= now
        return result(DONT_BOTHER, (
            "Too late for this week: " + ("his game has already started."
                                          if started else
                                          f"the claim processes {status['claim_processes_at_local'][:16]}, "
                                          "after his kickoff.")))
    if worth == "low":
        if status["on_waivers"] and not contested:
            return result(WAIT, (f"Not worth priority. If you still want him, wait {wait_for} "
                                 "and add him as a free agent."))
        if status["on_waivers"] is None and not contested:
            return result(WAIT, ("Not worth priority. If you still want him, wait until he "
                                 "clears waivers and add him as a free agent."))
        return result(DONT_BOTHER, "Not worth a roster move." if status["instant_add"]
                      else f"Not worth a claim — {who}{spend}.")
    if status["instant_add"]:
        return result(ADD_NOW, "Free agent: add him now, no waiver priority needed.")
    if cost == 0:
        return result(CLAIM_NOW, f"Claim him: {who}{spend}.")
    if worth == "high":
        return result(CLAIM_NOW, (f"Worth your priority: {who}{spend}."
                                  + (" Contested — waiting would hand him to a team ahead "
                                     "of you." if contested else "")))
    if contested:
        return result(CLAIM_NOW, (f"Contested ({demand} demand), so he will not reach free "
                                  f"agency: claim now if you want him — {who}{spend}."))
    # Uncontested and only a moderate help: let him clear and take him for
    # free — unless clearing comes too late for the game that matters.
    if not status["clears_at"]:  # timing unknown
        return result(WAIT, (f"Little demand: wait until he clears waivers and add him as a "
                             f"free agent, keeping your priority — {who}."))
    clears_in_time = not this_week or kickoff is None or (
        datetime.fromisoformat(status["clears_at"]) < kickoff)
    if clears_in_time:
        return result(WAIT, (f"Little demand: wait {wait_for} and add him as a free agent, "
                             f"keeping your priority — {who}."))
    return result(CLAIM_NOW, (f"He clears only after his kickoff, so a claim is the only way "
                              f"to have him this week: {who}{spend}."))


def latest_drops(transactions: list[dict] | None) -> dict[str, datetime]:
    """``{player_id: when he was last dropped}`` from Sleeper transactions.

    Only completed moves count (a failed claim drops nobody); the time is
    ``status_updated``, when the drop actually took effect, in UTC.
    """
    drops: dict[str, datetime] = {}
    for txn in transactions or []:
        if (txn.get("status") or "complete") != "complete":
            continue
        stamp = txn.get("status_updated") or txn.get("created")
        if not isinstance(stamp, (int, float)):
            continue
        when = datetime.fromtimestamp(stamp / 1000, UTC)
        for pid in (txn.get("drops") or {}):
            if str(pid) not in drops or when > drops[str(pid)]:
                drops[str(pid)] = when
    return drops
