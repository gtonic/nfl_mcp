"""Keep the projections a lineup was set against, so they can be graded later.

A projection is only worth grading if it was made before the game. Recompute
one for last week today and it already knows who was ruled out on Sunday
morning and, through the trailing usage, part of how the game went. So every
briefing and ``project_players`` run appends what it projected to
``projection_log`` — but only for players whose game has not kicked off, and
only when the number moved since the last row. The first row of a week is
then the pre-game projection a retro grades; the later rows are what changed
since, which is what "what changed since I last looked" reports.
"""
from __future__ import annotations

import logging

from .game_clock import progress_of, week_games
from .scoring import resolve_scoring, scoring_fingerprint
from .teams import normalize_team

logger = logging.getLogger(__name__)


def log_projections(
    db, season: int | None, week: int | None, scoring,
    projections: list[dict], league_id: str | None = None,
    source: str | None = None, games: dict[str, dict] | None = None,
) -> int:
    """Append pre-kickoff projections to the log; returns rows written.

    ``scoring`` is whatever priced them — pass the league's own
    (``league_scoring(league)``) when a league is known. Rows are keyed by its
    fingerprint, so a retro reads back only numbers made under the same
    scoring. ``games`` is ``game_clock.week_games`` when the caller has it.

    ``projections`` are projection-engine rows (``player``, ``team``,
    ``projected_points``, ``floor``, ``ceiling``) carrying a ``player_id``;
    rows without one are skipped, since the log is joined to Sleeper's
    ``players_points`` by id. A player whose kickoff is unknown is logged:
    the schedule is cached for the whole season, so a missing kickoff means a
    gap in the cache, not a game already under way.
    """
    if db is None or not season or not week or not hasattr(db, "record_projections"):
        return 0
    model = resolve_scoring(scoring)
    if games is None:
        games = week_games(db, season, week)
    rows = []
    for p in projections or []:
        pid = p.get("player_id")
        if not pid or p.get("projected_points") is None:
            continue
        if progress_of(games.get(normalize_team(p.get("team")) or "")) > 0.0:
            continue  # kicked off: whatever it says now is not a pre-game number
        rows.append({
            "player_id": pid,
            "projected_points": p.get("projected_points"),
            "floor": p.get("floor"),
            "ceiling": p.get("ceiling"),
            "name": p.get("player") or p.get("name"),
            "position": p.get("position"),
            "team": p.get("team"),
        })
    if not rows:
        return 0
    try:
        return db.record_projections(season, week, model.fingerprint, rows,
                                     league_id=league_id, source=source, ppr=model.rec)
    except Exception as e:  # logging is a side effect; never fail the caller on it
        logger.warning(f"projection log write failed: {e}")
        return 0


def scoring_key(scoring) -> str:
    """The key stored projections are filed under for this scoring."""
    return scoring_fingerprint(scoring)
