"""A game designation is about one game; after that game it is history.

Sleeper's ``injury_status`` and the ESPN report's ``status`` are set for the
coming game (or during it: "Out" for a player who left and did not return).
Neither is cleared at the final whistle. Until the team's next practice week
produces a report, both feeds keep saying what was true of the game already
played -- and every tool that looks ahead read it as next week's verdict.

Monday of week 10 2026, planning week 11: Kenneth Walker (held out Sunday as
a precaution, "expected to be able to go next Sunday") and Brock Bowers (ankle
rolled in the first quarter, "day-to-day") were both ``Out``, projected 0.0
and benched, while Emmett Johnson was priced as Walker's replacement. That is
the window in which waiver claims and trades are decided.

``carried_teams`` names the teams inside that window: the last game has
kicked off, no practice report of the next game week is stored yet, and the
next kickoff is more than ``CARRY_ENDS_BEFORE_KICKOFF`` away. For those teams

* the ESPN report's status gives way to its own forward read,
  ``game_status`` (``details.fantasyStatus``: Walker ``Out`` / ``Questionable``)
  when that is milder and the return date does not rule the next game out
  (`forward_reports`);
* Sleeper's Out / Doubtful is not a verdict on the next game: the report
  speaks where there is one, else he is Questionable (`sleeper_forward`).

Reserve lists (IR, PUP, NFI, suspensions) are never touched, nor is a report
that says Out on both fields (a multi-week injury reads that way).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from . import injury_status
from .game_clock import parse_kickoff
from .teams import normalize_team

logger = logging.getLogger(__name__)

# From here on the statuses are about the coming game even without a stored
# practice report (a prefetch that missed it must not keep the window open):
# Thursday afternoon for a Sunday game, Monday evening for a Thursday one.
CARRY_ENDS_BEFORE_KICKOFF = timedelta(hours=72)
# With no next game on the schedule the status is stale only this long.
CARRY_MAX_WITHOUT_NEXT_GAME = timedelta(days=7)
# What a carried Out / Doubtful with no forward read is: an open question.
CARRIED_STATUS = "Questionable"

_OUT_KINDS = ("out", "doubtful")


def _eastern_date(moment: datetime) -> date:
    from .practice_reports import to_eastern
    return to_eastern(moment).date()


def _is_reserve(status: str | None) -> bool:
    from .ros import is_reserve  # deferred: ros is heavy
    return is_reserve(status)


def carried_teams(db, now: datetime | None = None) -> dict[str, dict]:
    """``team -> {last_kickoff, last_week, next_kickoff, next_week}`` for every
    team whose statuses still describe a game that has kicked off.

    Empty without a schedule: unknown means "trust the status", the old
    behaviour.
    """
    if db is None or not hasattr(db, "get_schedule_week_spans") or not hasattr(db, "get_week_kickoffs"):
        return {}
    now = now or datetime.now(UTC)
    try:
        spans = db.get_schedule_week_spans() or []
        if not spans:
            return {}
        season = max(int(s["season"]) for s in spans)
        weeks = sorted({int(s["week"]) for s in spans if int(s["season"]) == season})
        last: dict[str, tuple[datetime, int]] = {}
        upcoming: dict[str, tuple[datetime, int]] = {}
        for week in weeks:
            for team, kickoff in (db.get_week_kickoffs(season, week) or {}).items():
                code, when = normalize_team(team), parse_kickoff(kickoff)
                if not code or when is None:
                    continue
                if when <= now:
                    if code not in last or when > last[code][0]:
                        last[code] = (when, week)
                elif code not in upcoming or when < upcoming[code][0]:
                    upcoming[code] = (when, week)
    except Exception as e:
        logger.debug(f"status carry-over: schedule unavailable: {e}")
        return {}

    practice: dict[int, dict] = {}

    def practice_begun(team: str, week: int, since: datetime) -> bool:
        if not hasattr(db, "get_team_practice_snapshots"):
            return False
        if week not in practice:
            try:
                raw = db.get_team_practice_snapshots(season, week) or {}
            except Exception:
                raw = {}
            practice[week] = {normalize_team(t) or t: days for t, days in raw.items()}
        played = _eastern_date(since).isoformat()
        return any(str(day)[:10] > played for day in (practice[week].get(team) or {}))

    out: dict[str, dict] = {}
    for team, (kicked, week) in last.items():
        nxt = upcoming.get(team)
        if nxt is None:
            if now - kicked > CARRY_MAX_WITHOUT_NEXT_GAME:
                continue
        elif now >= nxt[0] - CARRY_ENDS_BEFORE_KICKOFF or practice_begun(team, nxt[1], kicked):
            continue
        out[team] = {
            "last_kickoff": kicked.isoformat(), "last_week": week,
            "next_kickoff": nxt[0].isoformat() if nxt else None,
            "next_week": nxt[1] if nxt else None,
        }
    return out


def _misses_next_game(return_date: str | None, window: dict) -> bool:
    """Whether the report's return date lies after the team's next game."""
    nxt = parse_kickoff(window.get("next_kickoff"))
    if not return_date or nxt is None:
        return False
    try:
        back = date.fromisoformat(str(return_date)[:10])
    except ValueError:
        return False
    return back > _eastern_date(nxt)


def forward_status(row: dict, window: dict | None) -> str | None:
    """The report row's status for the team's NEXT game.

    Inside the window (``window``) an Out / Doubtful gives way to the row's
    milder ``game_status``; outside it, on a reserve list, with a return date
    past the next game or without a milder forward read, the status stands.
    """
    status = row.get("injury_status")
    if not window or injury_status.availability(status) not in _OUT_KINDS:
        return status
    forward = row.get("game_status")
    if _is_reserve(status) or _is_reserve(forward) or _misses_next_game(row.get("return_date"), window):
        return status
    canonical = injury_status.normalize(forward)
    if not canonical or injury_status.severity(canonical) >= injury_status.severity(status):
        return status
    # "Active" here is ESPN's "no designation", not a clean bill of health for
    # a player its own status line still calls Out.
    return CARRIED_STATUS if injury_status.is_healthy(canonical) else canonical


def forward_reports(rows: list[dict] | None, carried: dict[str, dict] | None) -> list[dict]:
    """The stored report rows with `forward_status` applied; a changed row is a
    copy that keeps the original under ``carried_from``."""
    out = []
    for row in rows or []:
        window = (carried or {}).get(normalize_team(row.get("team_id")) or "")
        status = forward_status(row, window) if window else row.get("injury_status")
        if status != row.get("injury_status"):
            row = {**row, "injury_status": status, "carried_from": row.get("injury_status"),
                   "carried_over": True}
        out.append(row)
    return out


def current_reports(db, now: datetime | None = None) -> list[dict]:
    """``db.get_all_current_injuries()`` read for the coming game."""
    rows = list(db.get_all_current_injuries() or [])
    try:
        return forward_reports(rows, carried_teams(db, now))
    except Exception as e:
        logger.debug(f"status carry-over skipped: {e}")
        return rows


def injury_index(db, team_names=None, now: datetime | None = None):
    """`injury_match.build_injury_index` over `current_reports`, carrying the
    window (``index.carried``) so `injury_match.resolve_injury` reads Sleeper's
    side the same way."""
    from .injury_match import build_injury_index
    try:
        carried = carried_teams(db, now)
    except Exception as e:
        logger.debug(f"status carry-over skipped: {e}")
        carried = {}
    index = build_injury_index(forward_reports(list(db.get_all_current_injuries() or []), carried),
                               team_names)
    index.carried = carried  # type: ignore[attr-defined]
    return index


def sleeper_forward(sleeper_status: str | None, has_report: bool, window: dict | None) -> str | None:
    """Sleeper's status for the team's next game.

    Inside the window an Out / Doubtful game designation is about the game
    already played: where the report has a row for him it speaks instead
    (None), otherwise he is `CARRIED_STATUS`. Everything else stands.
    """
    if (not window or _is_reserve(sleeper_status)
            or injury_status.availability(sleeper_status) not in _OUT_KINDS):
        return sleeper_status
    return None if has_report else CARRIED_STATUS
