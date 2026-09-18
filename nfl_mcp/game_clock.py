"""How far along an NFL game is, from its kickoff time.

Mid-week, a fantasy matchup is part settled and part still to come. Treating
every player as a projection ignores points already banked; treating the whole
week as settled ignores everyone who has not played. This module draws the line.

The NFL publishes no live clock through the feeds this server uses, so progress
is derived from kickoff plus a nominal game length. That is accurate at the two
ends — not started, and finished — and approximate only during the roughly
three-hour window in between, which is the least decision-relevant time anyway:
lineups for those players are already locked.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

# Wall-clock length of a televised NFL game, stoppages and halftime included.
GAME_LENGTH = timedelta(hours=3, minutes=15)


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


def game_progress(kickoff: str | datetime | None, now: datetime | None = None) -> float:
    """Fraction of a game that has been played: 0.0 upcoming, 1.0 final.

    An unknown kickoff returns 0.0 — treating a player as unplayed keeps him in
    the optimizer, which is the recoverable error; assuming he is finished would
    silently freeze a lineup that can still be changed.
    """
    start = kickoff if isinstance(kickoff, datetime) else parse_kickoff(kickoff)
    if start is None:
        return 0.0
    now = now or datetime.now(UTC)
    if now <= start:
        return 0.0
    elapsed = now - start
    if elapsed >= GAME_LENGTH:
        return 1.0
    return elapsed / GAME_LENGTH


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
