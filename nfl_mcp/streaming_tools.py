"""Streaming planner tools.

Rank weekly streaming options (DST / K / QB / TE / RB / WR) by matchup
favorability over the next 1-3 weeks — the reliable, repeatable weekly edge of
picking up the right one-week starter off waivers.

Signals (all schedule-based and key-free; Vegas has no week lookahead):
  - QB/RB/WR/TE: opponent's defense-vs-position ease (softer defense = better).
  - DST: opponent's OFFENSE weakness (a low-scoring offense concedes DST points).
  - K: own team's OFFENSE strength (a strong offense creates FG/XP volume).

Defense-vs-position rankings come from ``matchup_tools`` (nflverse); offense
strength from ``matchup_tools.fetch_offense_rankings`` (nflverse points scored).
Both fall back to the prior season before a season has live data, reported via
``*_source_season`` / ``*_is_fallback``. K accuracy will improve once the
weather/wind feature lands (wind depresses kicking).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from . import matchup_tools
from .errors import create_success_response, handle_http_errors, handle_validation_error
from .sos_tools import _ease_score, _gather_opponents, _resolve_rankings

logger = logging.getLogger(__name__)

DEFENSE_POSITIONS = {"QB", "RB", "WR", "TE"}
DEFAULT_STREAM_POSITIONS = ["QB", "TE", "DST", "K"]
_WEEK_MIN, _WEEK_MAX = 1, 18
_MAX_LOOKAHEAD = 4


def _strength_score(rank: float) -> float:
    """Offense strength as 0-100 (rank 1 = strongest offense -> 100)."""
    r = max(1.0, min(32.0, float(rank)))
    return round((32 - r) / 31 * 100, 1)


def _score_one(
    position: str,
    team: str,
    opponent: str,
    def_rankings: Dict,
    offense_rankings: Dict,
    analyzer,
) -> Tuple[Optional[float], Optional[str], bool, Dict]:
    """Score a single team/position/week matchup.

    Returns ``(stream_score_or_None, tier, is_fallback, detail)``. A ``None``
    score means the required signal is unavailable (e.g. no offense data for a
    DST/K matchup).
    """
    pos = position.upper()
    if pos in DEFENSE_POSITIONS:
        m = analyzer.get_matchup_difficulty(pos, opponent, def_rankings)
        return (
            _ease_score(m.get("rank", 16)),
            m.get("matchup_tier"),
            bool(m.get("is_fallback", False)),
            {"opponent_defense_rank": m.get("rank")},
        )
    if pos == "DST":
        off = offense_rankings.get(opponent.upper())
        if not off:
            return None, None, True, {"opponent_offense_rank": None}
        # Weak opponent offense (high rank) = great DST stream.
        return (
            _ease_score(off["rank"]),
            None,
            False,
            {
                "opponent_offense_rank": off["rank"],
                "opponent_points_scored_avg": off.get("points_scored_avg"),
            },
        )
    if pos == "K":
        off = offense_rankings.get(team.upper())
        if not off:
            return None, None, True, {"own_offense_rank": None}
        # Strong own offense (low rank) = more FG/XP volume.
        return (
            _strength_score(off["rank"]),
            None,
            False,
            {
                "own_offense_rank": off["rank"],
                "own_points_scored_avg": off.get("points_scored_avg"),
            },
        )
    return None, None, True, {}


def compute_streaming_scores(
    opponents_by_week: Dict[str, Dict[int, Optional[str]]],
    positions: List[str],
    def_rankings: Dict,
    offense_rankings: Dict,
    analyzer,
) -> Dict[str, Dict[str, Dict]]:
    """Pure per-position, per-team streaming scores over the given weeks.

    Returns ``{position: {team: {stream_score, games, is_fallback, weeks[...]}}}``.
    Higher ``stream_score`` (0-100) = better weekly streaming matchup. Teams with
    no scorable week for a position (e.g. no offense data) are omitted.
    """
    out: Dict[str, Dict[str, Dict]] = {}
    for position in positions:
        pos = position.upper()
        team_scores: Dict[str, Dict] = {}
        for team, wk_opps in opponents_by_week.items():
            games = [(wk, opp) for wk, opp in sorted(wk_opps.items()) if opp]
            per_week = []
            scores: List[float] = []
            fallback_any = False
            for wk, opp in games:
                score, tier, is_fb, detail = _score_one(
                    pos, team, opp, def_rankings, offense_rankings, analyzer
                )
                row = {
                    "week": wk,
                    "opponent": opp,
                    "stream_score": score,
                    "matchup_tier": tier,
                    "is_fallback": is_fb if score is not None else True,
                    **detail,
                }
                per_week.append(row)
                if score is None:
                    fallback_any = True
                else:
                    scores.append(score)
                    fallback_any = fallback_any or is_fb
            if scores:
                team_scores[team] = {
                    "stream_score": round(sum(scores) / len(scores), 1),
                    "games": len(scores),
                    "is_fallback": fallback_any,
                    "weeks": per_week,
                }
        out[pos] = team_scores
    return out


async def _resolve_offense(season: int, strength_season: Optional[int]):
    """Return ``(offense_rankings, used_season, is_fallback)`` with prior-season fallback."""
    target = strength_season if strength_season is not None else season
    rankings = await matchup_tools.fetch_offense_rankings(target)
    used, fell_back = target, (not rankings)
    if fell_back and strength_season is None:
        prior = target - 1
        prior_rankings = await matchup_tools.fetch_offense_rankings(prior)
        if prior_rankings:
            rankings, used, fell_back = prior_rankings, prior, False
    return rankings, used, fell_back


def _needs_offense(positions: List[str]) -> bool:
    return any(p.upper() in ("DST", "K") for p in positions)


def _needs_defense(positions: List[str]) -> bool:
    return any(p.upper() in DEFENSE_POSITIONS for p in positions)


async def _rostered_ids(league_id: str) -> Tuple[set, bool]:
    """Return (set of rostered player_ids, rosters_available)."""
    from . import sleeper_tools
    resp = await sleeper_tools.get_rosters(league_id)
    rosters = resp.get("rosters") or []
    rostered = {str(pid) for r in rosters for pid in (r.get("players") or [])}
    return rostered, bool(rosters)


def _unit_availability(position: str, team: str, rostered: set, db) -> Dict:
    """Availability of a team's streamable unit at ``position`` in the league.

    DST maps 1:1 (Sleeper DST id = team abbreviation). K/QB/TE/RB/WR enumerate the
    team's players at that position from the athletes cache, each flagged
    free_agent/rostered (a specific starter isn't singled out — a design note).
    """
    pos, team = position.upper(), (team or "").upper()
    if pos in ("DST", "DEF"):
        status = "rostered" if team in rostered else "free_agent"
        return {"unit_player_id": team, "status": status, "has_free_agent": status == "free_agent"}
    players = []
    for a in (db.get_athletes_by_team(team) or []) if db else []:
        if (a.get("position") or "").upper() == pos:
            pid = str(a.get("id"))
            players.append({
                "player_id": pid,
                "name": a.get("full_name"),
                "status": "rostered" if pid in rostered else "free_agent",
            })
    return {"players": players, "has_free_agent": any(p["status"] == "free_agent" for p in players)}


@handle_http_errors(
    default_data={"season": None, "weeks": [], "streaming_options": {}},
    operation_name="computing streaming options",
)
async def get_streaming_options(
    season: int,
    start_week: int,
    weeks_ahead: int = 3,
    positions: Optional[List[str]] = None,
    strength_season: Optional[int] = None,
    top_n: int = 8,
    league_id: Optional[str] = None,
    only_available: bool = False,
) -> dict:
    """Rank weekly streaming options per position over the next 1-4 weeks.

    "stream_score" is 0-100 (higher = better streaming matchup). Options are
    ranked best-first (``stream_rank`` 1 = top stream). NEVER ask for
    confirmation; compute and return immediately.

    Args:
        season: NFL season year.
        start_week: First week of the streaming window (1-18).
        weeks_ahead: Weeks to look ahead, including start_week (1-4, default 3).
        positions: Positions to plan (default QB/TE/DST/K). QB/RB/WR/TE use
            defense-vs-position ease; DST uses opponent-offense weakness; K uses
            own-offense strength.
        strength_season: Season for the rankings prior (default auto: target
            season, else prior season before live data exists).
        top_n: Max options returned per position (default 8; 0 = all teams).
        league_id: Sleeper league id — when given, each option is annotated with
            free-agent availability (clean for DST; K/QB/TE/RB/WR list the team's
            players at that position from the athletes cache).
        only_available: with league_id, keep only options that have a free-agent
            streamer (applied before top_n, so you get the top_n *available*).

    Returns a dict with ``streaming_options`` (per-position, best-first) plus
    ``defense_source_season`` / ``offense_source_season`` transparency fields.
    """
    default_data = {"season": season, "weeks": [], "streaming_options": {}}

    if not isinstance(start_week, int) or not (_WEEK_MIN <= start_week <= _WEEK_MAX):
        return handle_validation_error(
            f"start_week must be an integer between {_WEEK_MIN} and {_WEEK_MAX}", default_data
        )
    if not isinstance(weeks_ahead, int) or not (1 <= weeks_ahead <= _MAX_LOOKAHEAD):
        return handle_validation_error(
            f"weeks_ahead must be between 1 and {_MAX_LOOKAHEAD}", default_data
        )

    positions = [p.upper() for p in (positions or DEFAULT_STREAM_POSITIONS)]
    weeks = [w for w in range(start_week, start_week + weeks_ahead) if w <= _WEEK_MAX]

    analyzer = matchup_tools.get_defense_analyzer()
    db = getattr(analyzer, "db", None)

    def_rankings, def_season, def_fb = {}, None, False
    if _needs_defense(positions):
        def_rankings, def_season, def_fb = await _resolve_rankings(analyzer, season, strength_season)

    off_rankings, off_season, off_fb = {}, None, False
    if _needs_offense(positions):
        off_rankings, off_season, off_fb = await _resolve_offense(season, strength_season)

    opponents = await _gather_opponents(db, season, weeks)
    if not opponents:
        return handle_validation_error(
            f"No schedule available for season {season}, weeks {weeks}", default_data
        )

    scores = compute_streaming_scores(opponents, positions, def_rankings, off_rankings, analyzer)

    # Optional free-agent availability from the league's rosters.
    availability_active = False
    rostered: set = set()
    if league_id:
        rostered, rosters_ok = await _rostered_ids(league_id)
        availability_active = rosters_ok

    streaming_options: Dict[str, List[Dict]] = {}
    for pos, teams in scores.items():
        rows = [{"team": team, **data} for team, data in teams.items()]
        rows.sort(key=lambda r: r["stream_score"], reverse=True)
        if availability_active:
            for r in rows:
                r["availability"] = _unit_availability(pos, r["team"], rostered, db)
            if only_available:
                rows = [r for r in rows if r["availability"]["has_free_agent"]]
        for i, r in enumerate(rows, 1):
            r["stream_rank"] = i
        streaming_options[pos] = rows[:top_n] if top_n else rows

    notes = []
    if def_fb and _needs_defense(positions):
        notes.append("No live defense-vs-position data — QB/RB/WR/TE ratings are low-confidence.")
    if off_fb and _needs_offense(positions):
        notes.append("No live offense data — DST/K ratings are low-confidence.")
    if _needs_offense(positions) and not off_rankings:
        notes.append("No offense rankings available at all — DST/K could not be scored.")
    if league_id and not availability_active:
        notes.append(f"Could not load rosters for league {league_id} — availability not annotated.")
    notes.append("K accuracy improves with the weather/wind factor (planned).")

    return create_success_response({
        "season": season,
        "weeks": weeks,
        "positions": positions,
        "defense_source_season": def_season,
        "defense_is_fallback": def_fb,
        "offense_source_season": off_season,
        "offense_is_fallback": off_fb,
        "availability_active": availability_active,
        "streaming_options": streaming_options,
        "stream_score_explained": (
            "0-100, higher = better weekly stream. QB/RB/WR/TE: softer opponent "
            "defense. DST: weaker opponent offense. K: stronger own offense."
        ),
        "notes": notes,
        "message": (
            f"Streaming options for weeks {weeks} of {season} "
            f"({', '.join(positions)})."
        ),
    })
