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
            # What was active when it was made, for the accuracy loop
            # (`projection_accuracy`); the briefing passes them precomputed.
            "signals": p.get("signals") or signals_of(p),
        })
    if not rows:
        return 0
    try:
        return db.record_projections(season, week, model.fingerprint, rows,
                                     league_id=league_id, source=source, ppr=model.rec)
    except Exception as e:  # logging is a side effect; never fail the caller on it
        logger.warning(f"projection log write failed: {e}")
        return 0


def signals_of(proj: dict) -> dict:
    """The signals active on a projection row, compact (Nones dropped).

    What the accuracy loop (`projection_accuracy`) groups errors by: the
    projection's source and its two inputs (ours, Sleeper's), the injury and
    practice read, the role trend (`role_shift`), returning teammates and
    inherited volume, the backup-QB multiplier (`qb_coupling`), the news
    flags (`news_signals`) and the matchup tier. Also the multipliers each
    signal actually applied (practice share on Sleeper's side, the role
    multiplier, the availability and news multipliers on ours), the gameday
    decision, the QB coupling's sit weight and its basis, and whether an
    injury-shortened game was left out of the volume — what the weekly signal
    review (`signal_review`) reads to say whether a multiplier is calibrated.
    """
    bd = proj.get("breakdown") or {}
    qb = proj.get("qb_context") or {}
    flags = [f.get("flag") if isinstance(f, dict) else str(f)
             for f in proj.get("news_flags") or []]
    gain = proj.get("role_gain") or {}

    def _moved(value):
        """A multiplier only when it moved the number (1.0 = no signal)."""
        try:
            return None if value is None or float(value) == 1.0 else round(float(value), 3)
        except (TypeError, ValueError):
            return None

    out = {
        "projection_source": proj.get("projection_source"),
        "model_projection": proj.get("model_projection"),
        "sleeper_projection": proj.get("sleeper_projection"),
        "injury_status": proj.get("injury_status"),
        "practice_status": proj.get("practice_status"),
        "practice_pattern": proj.get("practice_pattern"),
        "role_trend": proj.get("role_trend"),
        "returning_teammates": len(bd.get("returning_teammates") or []) or None,
        "inherited_volume": True if bd.get("inherited_from") else None,
        "qb_mult": qb.get("model_mult") if qb.get("applied") else None,
        "qb_sleeper_mult": qb.get("sleeper_mult") if qb.get("applied") else None,
        "news_flags": [f for f in flags if f] or None,
        "matchup_tier": proj.get("matchup_tier"),
        "gameday_status": proj.get("gameday_status"),
        "injury_exit": True if bd.get("injury_exit_weeks") else None,
        "role_mult": _moved(bd.get("role_mult")),
        "role_gain_priced": True if gain.get("priced") else None,
        "practice_blend_mult": _moved(bd.get("practice_blend_mult")),
        "injury_mult": _moved(bd.get("injury_mult")),
        "news_model_mult": _moved(bd.get("news_model_mult")),
        "qb_sit_weight": qb.get("starter_sit_weight") if qb.get("applied") else None,
        "qb_sit_basis": qb.get("starter_sit_basis") if qb.get("applied") else None,
    }
    return {k: v for k, v in out.items() if v is not None}


def scoring_key(scoring) -> str:
    """The key stored projections are filed under for this scoring."""
    return scoring_fingerprint(scoring)
