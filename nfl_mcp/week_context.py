"""Which week a question is about, and whether a team plays in it.

A player whose team has no game projects nothing, but every projection path
used to price him at his full baseline: the opponent came in as "" or "BYE",
the matchup and Vegas lookups quietly fell back to neutral, and start/sit
called a receiver on bye a 17-point must-start. Only the briefing noticed,
because it builds players from the schedule and drops a team that is not in
it. This module is that knowledge in one place, for every tool.

The schedule is ``schedule_games`` (one row per team per game, both
directions). A team missing from a week that *is* cached is on bye; a week
that is not cached says nothing, and nothing is assumed from it.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from .game_clock import GAME_LENGTH, game_progress, parse_kickoff
from .teams import normalize_team

logger = logging.getLogger(__name__)

# What callers write for "no game". Compared upper-cased and stripped.
BYE_MARKERS = frozenset({"BYE", "ON BYE", "BYE WEEK"})

# Fewer teams than this in a cached week means the cache is partial rather
# than that the rest are on bye. The heaviest bye week has six teams off, so a
# complete week lists at least 26.
MIN_TEAMS_FOR_KNOWN_WEEK = 20

BYE = "bye"
PLAYING = "playing"
UNKNOWN = "unknown"


def is_bye_marker(opponent: str | None) -> bool:
    """True when an explicit opponent string means "no game"."""
    return (opponent or "").strip().upper() in BYE_MARKERS


def week_opponents(db, season: int | None, week: int | None) -> dict[str, str]:
    """``{team: opponent}`` for one week, canonical codes, whatever is cached.

    The schedule mixes ESPN and Sleeper spellings (`WSH`/`WAS`), so both sides
    are normalized; a row that does not normalize is dropped rather than
    matched by accident.
    """
    if db is None or not season or not week or not hasattr(db, "get_week_opponents"):
        return {}
    try:
        raw = db.get_week_opponents(season, week)
    except Exception as e:
        logger.debug(f"schedule lookup failed for {season} week {week}: {e}")
        return {}
    if not isinstance(raw, dict):
        return {}
    opponents: dict[str, str] = {}
    for team, opp in raw.items():
        canon_team, canon_opp = normalize_team(team), normalize_team(opp)
        if canon_team and canon_opp:
            opponents[canon_team] = canon_opp
    return opponents


def week_schedule(db, season: int | None, week: int | None) -> dict[str, str] | None:
    """The week's opponents when the cached week is complete enough to prove a
    bye, otherwise None ("unknown", not "nobody plays")."""
    opponents = week_opponents(db, season, week)
    if len(opponents) < MIN_TEAMS_FOR_KNOWN_WEEK:
        return None
    return opponents


def bye_check(
    team: str | None, opponent: str | None, schedule: dict[str, str] | None,
    week: int | None = None,
) -> dict:
    """Whether `team` plays this week.

    Returns ``{status, opponent, source, reason}`` with status one of
    ``"bye"``, ``"playing"``, ``"unknown"``:

    - an explicit bye marker from the caller is a bye;
    - with a complete cached week, a team absent from it is on bye — even when
      the caller named an opponent, because a stale or wrong-week opponent is
      exactly how a bye slips through; the conflict is stated in `reason`;
    - a team the schedule lists is playing against the scheduled opponent; a
      blank opponent is filled in, a conflicting one is overridden (in `reason`);
    - with no usable schedule, a named opponent is taken at its word and a
      blank one is "unknown". A bye is never assumed from missing data.
    """
    given = (opponent or "").strip().upper()
    wk = f"week {week}" if week else "this week"
    if is_bye_marker(given):
        return {"status": BYE, "opponent": None, "source": "caller",
                "reason": f"on bye {wk} (opponent given as {given!r})"}
    canon = normalize_team(team)
    if schedule is not None and canon:
        if canon in schedule:
            # The schedule wins over a caller's opponent (often last week's or
            # a guess); a disagreement is stated rather than silently used.
            scheduled = schedule[canon]
            if given and (normalize_team(given) or given) != (normalize_team(scheduled) or scheduled):
                return {"status": PLAYING, "opponent": scheduled, "source": "schedule",
                        "reason": (f"opponent given as {given}, but the schedule has "
                                   f"{canon} vs {scheduled} {wk} — using the schedule")}
            return {"status": PLAYING, "opponent": scheduled or given,
                    "source": "caller" if given else "schedule", "reason": None}
        reason = f"on bye {wk}: the schedule has no {canon} game"
        if given:
            reason += f" (opponent was given as {given}, which the schedule does not confirm)"
        return {"status": BYE, "opponent": None, "source": "schedule", "reason": reason}
    if given:
        return {"status": PLAYING, "opponent": given, "source": "caller", "reason": None}
    return {"status": UNKNOWN, "opponent": None, "source": None,
            "reason": f"no opponent given and no cached schedule for {wk} — "
                      "bye status unknown, projected as if playing"}


async def resolve_season_week(
    season: int | None, week: int | None, db=None
) -> tuple[int | None, int | None, bool]:
    """Fill in season/week from the current NFL week when the caller omitted them.

    Omitting them is the common case — an agent rarely knows the current week —
    and it silently downgraded every projection to the positional-rank baseline:
    six static values per position, so a workhorse RB came out at 16.8 instead
    of 31.1 for the same week. It also leaves nothing to check a bye against.

    Built on ``current_season_week`` (live state, last good state, cached
    schedule, calendar), so a Sleeper outage no longer lands on week 0.

    Returns ``(season, week, inferred)`` so callers can report which values were
    used rather than leaving it to be guessed from the numbers.
    """
    if season is not None and week is not None:
        return season, week, False
    if db is None:
        try:
            from .database import get_shared_db
            db = get_shared_db()
        except Exception as e:
            logger.debug(f"no database for season/week inference: {e}")
    try:
        current = await current_season_week(db)
    except Exception as e:
        logger.debug(f"season/week inference failed: {e}")
        return season, week, False
    resolved_season = season if season is not None else current["season"]
    resolved_week = week if week is not None else current["week"]
    return resolved_season, resolved_week, True


# The last NFL state that came back intact, for when the feed is unreachable.
# Process-local: after a restart the cached schedule answers instead. Kept
# with the time it was seen: past `LAST_STATE_MAX_AGE` a long outage would
# otherwise pin the server to that week forever, so the schedule answers.
_last_state: dict | None = None
LAST_STATE_MAX_AGE = timedelta(hours=12)


def _usable_state(nfl_state: dict | None) -> tuple[int, int] | None:
    """``(season, week)`` from a Sleeper NFL state, or None when unusable."""
    if not isinstance(nfl_state, dict):
        return None
    try:
        season = int(nfl_state.get("season") or nfl_state.get("league_season") or 0)
        week = int(nfl_state.get("week") or nfl_state.get("display_week") or 0)
    except (TypeError, ValueError):
        return None
    if season < 2000:
        return None
    # Sleeper reports week 0 in the offseason; the first week is the one a
    # lineup question can be about.
    return season, max(1, week)


def infer_from_schedule(db, now: datetime | None = None) -> tuple[int, int] | None:
    """``(season, week)`` read off the cached schedule's kickoffs.

    The current week is the first week of the newest cached season that still
    has a game to finish; past the last one, the last one. None without a
    usable schedule.
    """
    if db is None or not hasattr(db, "get_schedule_week_spans"):
        return None
    try:
        spans = db.get_schedule_week_spans()
    except Exception as e:
        logger.debug(f"schedule spans unavailable: {e}")
        return None
    if not spans:
        return None
    now = now or datetime.now(UTC)
    season = max(int(s["season"]) for s in spans)
    weeks = sorted(
        (s for s in spans if int(s["season"]) == season), key=lambda s: int(s["week"])
    )
    for span in weeks:
        last = parse_kickoff(span.get("last_kickoff"))
        if last is not None and last + GAME_LENGTH > now:
            return season, int(span["week"])
    return season, int(weeks[-1]["week"])


def infer_from_calendar(now: datetime | None = None) -> tuple[int, int]:
    """A last-resort ``(season, week)`` from the date alone.

    The regular season opens the week after Labor Day (the first Monday of
    September). Weeks are counted from that Wednesday, the day Sleeper moves on
    to the next week once Monday night is over. Before that it is week 1 of
    the coming season, and Jan/Feb belong to the previous one.
    """
    today = (now or datetime.now(UTC)).date()
    season = today.year if today.month >= 3 else today.year - 1
    sept1 = date(season, 9, 1)
    labor_day = sept1 + timedelta(days=(7 - sept1.weekday()) % 7)
    opener = labor_day + timedelta(days=2)
    if today < opener:
        return season, 1
    return season, min(18, (today - opener).days // 7 + 1)


async def current_season_week(db=None) -> dict:
    """The current ``{season, week, source}``, never season 0.

    Order: the live NFL state, the last good state this process saw, the
    cached schedule's kickoffs, the calendar. An outage used to leave the
    briefing on season 0 / week 1 ("No schedule available for season 0,
    week 1"), which priced every player off an empty week.
    """
    global _last_state
    try:
        from . import sleeper_tools
        state = await sleeper_tools.get_nfl_state()
        got = _usable_state((state or {}).get("nfl_state"))
    except Exception as e:
        logger.debug(f"NFL state unavailable: {e}")
        got = None
    if got:
        _last_state = {"season": got[0], "week": got[1], "at": datetime.now(UTC)}
        return {"season": got[0], "week": got[1], "source": "nfl_state"}
    seen = (_last_state or {}).get("at")
    if _last_state and (seen is None or datetime.now(UTC) - seen <= LAST_STATE_MAX_AGE):
        return {"season": _last_state["season"], "week": _last_state["week"],
                "source": "cached_state"}
    inferred = infer_from_schedule(db)
    if inferred:
        return {"season": inferred[0], "week": inferred[1], "source": "schedule"}
    season, week = infer_from_calendar()
    return {"season": season, "week": week, "source": "calendar"}


def week_is_final(db, season: int, week: int, now: datetime | None = None) -> bool | None:
    """Whether every cached game of a week is over; None when not cached."""
    if db is None or not hasattr(db, "get_week_kickoffs"):
        return None
    try:
        kickoffs = db.get_week_kickoffs(season, week)
    except Exception:
        return None
    if not kickoffs:
        return None
    return all(game_progress(k, now) >= 1.0 for k in kickoffs.values())


async def last_completed_week(db=None) -> dict:
    """``{season, week, source}`` of the most recent fully played week.

    Sleeper moves its ``week`` on to the next one early in the week, so the
    current week counts as completed only once the schedule says every game
    is over. ``week`` is 0 before the first week has finished.
    """
    current = await current_season_week(db)
    season, week = current["season"], current["week"]
    final = week_is_final(db, season, week)
    return {"season": season, "week": week if final else week - 1,
            "source": current["source"]}
