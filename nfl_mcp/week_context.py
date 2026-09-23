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
    - a team the schedule lists is playing, and a blank opponent is filled in;
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
            return {"status": PLAYING, "opponent": given or schedule[canon],
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
    season: int | None, week: int | None
) -> tuple[int | None, int | None, bool]:
    """Fill in season/week from NFL state when the caller omitted them.

    Omitting them is the common case — an agent rarely knows the current week —
    and it silently downgraded every projection to the positional-rank baseline:
    six static values per position, so a workhorse RB came out at 16.8 instead
    of 31.1 for the same week. It also leaves nothing to check a bye against.

    Returns ``(season, week, inferred)`` so callers can report which values were
    used rather than leaving it to be guessed from the numbers.
    """
    if season is not None and week is not None:
        return season, week, False
    try:
        from .nfl_tools import get_current_season_and_week
        got_season, got_week = await get_current_season_and_week()
    except Exception as e:
        logger.debug(f"season/week inference failed: {e}")
        return season, week, False
    resolved_season = season if season is not None else got_season
    resolved_week = week if week is not None else got_week
    return resolved_season, resolved_week, True
