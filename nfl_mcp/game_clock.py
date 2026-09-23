"""How far along an NFL game is, from its kickoff time.

Mid-week, a fantasy matchup is part settled and part still to come. Treating
every player as a projection ignores points already banked; treating the whole
week as settled ignores everyone who has not played. This module draws the line.

The NFL publishes no live clock through the feeds this server uses, so progress
is derived from kickoff plus a nominal game length. That is accurate at the two
ends — not started, and finished — and approximate only during the roughly
three-hour window in between, which is the least decision-relevant time anyway:
lineups for those players are already locked.

Where the cached ESPN event carries a game state (``pre``/``in``/``post``), it
settles the ends exactly: a final is final even at 2h50, and an overtime game
still ``in`` at 3h30 is not. The state is only as fresh as the last schedule
fetch, so it can say "final" or "started" but never "not started yet" once the
clock says otherwise — a stale ``pre`` would unlock a player who is locked.

The same kickoff decides what a manager can still change: a player whose game
has started is locked in (or out of) the lineup, which is what `game_lock`
reports for the lineup and waiver tools.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .teams import normalize_team

logger = logging.getLogger(__name__)

# Wall-clock length of a televised NFL game, stoppages and halftime included.
GAME_LENGTH = timedelta(hours=3, minutes=15)

# An ESPN "in" state older than this after kickoff is a cache that stopped
# refreshing mid-game, not a six-hour game; the clock takes over again.
_STALE_IN_PROGRESS = timedelta(hours=5)

# Where the user reads kickoff times. Outputs carry UTC as well, so nothing
# downstream depends on this beyond display.
LOCAL_TZ = ZoneInfo("Europe/Vienna")


def parse_kickoff(value: str | None) -> datetime | None:
    """Parse an ISO-8601 kickoff into an aware UTC datetime, or None."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def game_progress(
    kickoff: str | datetime | None,
    now: datetime | None = None,
    state: str | None = None,
    completed: bool | None = None,
) -> float:
    """Fraction of a game that has been played: 0.0 upcoming, 1.0 final.

    An unknown kickoff returns 0.0 — treating a player as unplayed keeps him in
    the optimizer, which is the recoverable error; assuming he is finished would
    silently freeze a lineup that can still be changed.

    ``state``/``completed`` are ESPN's game status when cached: a completed
    game is 1.0 whatever the clock says, and a game still ``in`` progress is
    kept below 1.0 through overtime (until the state is plainly stale).
    """
    if completed:
        return 1.0
    start = kickoff if isinstance(kickoff, datetime) else parse_kickoff(kickoff)
    if start is None:
        return 0.0
    now = now or datetime.now(UTC)
    in_progress = (state or "").lower() == "in"
    if now <= start:
        # Clock skew or a kickoff moved up: ESPN saying it started wins.
        return 0.01 if in_progress else 0.0
    elapsed = now - start
    if in_progress and elapsed < _STALE_IN_PROGRESS:
        return min(elapsed / GAME_LENGTH, 0.99)
    if elapsed >= GAME_LENGTH:
        return 1.0
    return elapsed / GAME_LENGTH


def week_games(db, season: int | None, week: int | None) -> dict[str, dict]:
    """``{team: {kickoff, state, completed}}`` for a week, canonical team codes.

    Falls back to bare kickoffs for a database without the state reader, and
    to nothing at all when neither is cached — callers treat a missing team as
    "not started", the recoverable error.
    """
    if db is None or not season or not week:
        return {}
    raw: dict = {}
    try:
        if hasattr(db, "get_week_game_states"):
            raw = db.get_week_game_states(int(season), int(week)) or {}
        if not isinstance(raw, dict) or not raw:
            kickoffs = db.get_week_kickoffs(int(season), int(week)) if hasattr(
                db, "get_week_kickoffs") else {}
            raw = {t: {"kickoff": k} for t, k in (kickoffs or {}).items()} if isinstance(
                kickoffs, dict) else {}
    except Exception as e:
        logger.debug(f"game state lookup failed for {season} week {week}: {e}")
        return {}
    games: dict[str, dict] = {}
    for team, game in raw.items():
        canon = normalize_team(team)
        if canon and isinstance(game, dict) and game.get("kickoff"):
            games[canon] = game
    return games


def progress_of(game: dict | None, now: datetime | None = None) -> float:
    """`game_progress` for one `week_games` entry (None: not started)."""
    if not game:
        return 0.0
    return game_progress(game.get("kickoff"), now, game.get("state"), game.get("completed"))


def local_time(moment: datetime | None) -> str | None:
    """ISO-8601 in the user's zone (Europe/Vienna), offset included."""
    return moment.astimezone(LOCAL_TZ).isoformat() if moment else None


def game_lock(game: dict | None, now: datetime | None = None) -> dict:
    """Kickoff and lock fields for one player's game this week.

    ``locked`` means his game has started: he can no longer be moved into or
    out of a lineup, and cannot be dropped. An unknown game is not locked —
    keeping a player movable is the recoverable error. ``game_status`` is
    ``upcoming``, ``in_progress``, ``final`` or ``unknown``; ``status_source``
    says whether ESPN's state or the clock decided it.
    """
    start = parse_kickoff((game or {}).get("kickoff"))
    if start is None:
        return {"kickoff": None, "kickoff_local": None, "kickoff_weekday": None,
                "locked": False, "game_status": "unknown", "status_source": None}
    progress = progress_of(game, now)
    status = "upcoming" if progress <= 0.0 else "final" if progress >= 1.0 else "in_progress"
    state = (game.get("state") or "").lower()
    from_espn = bool(game.get("completed")) or (state == "in" and progress < 1.0)
    return {
        "kickoff": start.isoformat().replace("+00:00", "Z"),
        "kickoff_local": local_time(start),
        "kickoff_weekday": start.astimezone(LOCAL_TZ).strftime("%a"),
        "locked": progress > 0.0,
        "game_status": status,
        "status_source": "espn" if from_espn else "clock",
    }


def settle(projection: float, actual: float | None, progress: float) -> tuple[float, float]:
    """``(expected_total, share_still_uncertain)`` for one player.

    - Not started: the projection, fully uncertain.
    - Final: the actual score, no uncertainty left.
    - In progress: what he has already scored plus the untouched share of his
      projection, with the variance scaled to that share.
    """
    if progress <= 0.0:
        return projection, 1.0
    banked = float(actual or 0.0)
    if progress >= 1.0:
        return banked, 0.0
    remaining = 1.0 - progress
    return banked + projection * remaining, remaining
