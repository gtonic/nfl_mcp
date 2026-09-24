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

from . import matchup_tools
from .errors import create_success_response, handle_http_errors, handle_validation_error
from .player_pool import playing_options
from .sos_tools import _ease_score, _gather_opponents, _resolve_rankings
from .teams import normalize_team

logger = logging.getLogger(__name__)

DEFENSE_POSITIONS = {"QB", "RB", "WR", "TE"}
DEFAULT_STREAM_POSITIONS = ["QB", "TE", "DST", "K"]
# Sleeper calls a team defense "DEF"; the planner's own label is "DST".
DEFENSE_UNIT_POSITIONS = frozenset({"DST", "DEF"})
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
    def_rankings: dict,
    offense_rankings: dict,
    analyzer,
) -> tuple[float | None, str | None, bool, dict]:
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
    if pos in DEFENSE_UNIT_POSITIONS:
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
                "opponent_points_per_game": off.get("real_points_avg"),
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
                "own_points_per_game": off.get("real_points_avg"),
            },
        )
    return None, None, True, {}


def compute_streaming_scores(
    opponents_by_week: dict[str, dict[int, str | None]],
    positions: list[str],
    def_rankings: dict,
    offense_rankings: dict,
    analyzer,
) -> dict[str, dict[str, dict]]:
    """Pure per-position, per-team streaming scores over the given weeks.

    Returns ``{position: {team: {stream_score, games, byes, is_fallback, weeks[...]}}}``.
    Higher ``stream_score`` (0-100) = better weekly streaming matchup. Teams with
    no scorable week for a position (e.g. no offense data) are omitted.

    A bye inside the window scores 0 and counts toward the average: a unit
    that sits out one of three weeks gives you two weeks of starts, and
    averaging the bye away ranked it level with one that plays all three.
    A week counts as a bye only when the schedule has games that week for
    other teams — a week with no schedule at all is unknown, not a bye.
    """
    scheduled_weeks = {wk for opps in opponents_by_week.values()
                       for wk, opp in opps.items() if opp}
    out: dict[str, dict[str, dict]] = {}
    for position in positions:
        pos = position.upper()
        team_scores: dict[str, dict] = {}
        for team, wk_opps in opponents_by_week.items():
            games = [(wk, opp) for wk, opp in sorted(wk_opps.items()) if opp]
            per_week = []
            scores: list[float] = []
            fallback_any = False
            byes = sorted(wk for wk in scheduled_weeks if not wk_opps.get(wk))
            for wk in byes:
                per_week.append({"week": wk, "opponent": "BYE", "on_bye": True,
                                 "stream_score": 0.0, "matchup_tier": "bye",
                                 "is_fallback": False})
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
                per_week.sort(key=lambda r: r["week"])
                team_scores[team] = {
                    "stream_score": round(sum(scores) / (len(scores) + len(byes)), 1),
                    "games": len(scores),
                    "byes": byes,
                    "is_fallback": fallback_any,
                    "weeks": per_week,
                }
        out[pos] = team_scores
    return out


def unit_points(position: str, week_row: dict, model) -> float | None:
    """Expected fantasy points for a DST/K streaming week in the league's scoring.

    Priced like the projection engine: the default-scoring DST/K baseline from
    the relevant NFL points per game (opponent's for a defense, own for a
    kicker), rescaled by the league's points-allowed tiers / FG distance
    values. A bye week is 0. None when the week has no scoring average to price.

    It used to read `*_points_scored_avg`, which is the offense's summed
    *fantasy* points (~70-120 a game) rather than its score, so every defense
    fell in the 28+ tier (3.5 pts) and every kicker in the top one.
    """
    from .projections import defense_base, kicker_base
    if week_row.get("on_bye"):
        return 0.0
    pos = position.upper()
    if pos in DEFENSE_UNIT_POSITIONS:
        total = week_row.get("opponent_points_per_game")
        if total is None:
            return None
        return round(defense_base(total) * model.defense_scale(total), 1)
    if pos == "K":
        total = week_row.get("own_points_per_game")
        if total is None:
            return None
        return round(kicker_base(total) * model.kicker_scale(), 1)
    return None


async def _resolve_offense(season: int, strength_season: int | None):
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


async def unit_matchup(position: str, team: str, opponent: str, season: int | None,
                       model) -> dict | None:
    """Matchup read for one kicker or team defense, for start/sit.

    DEF keys on the *opponent's* offense (rank 32 = weakest = smash), K on his
    own (rank 1 = strongest = smash), tiered on the same 5/12/20/27 cut-offs as
    defense-vs-position. The tier is withheld (neutral) until the offense has
    `matchup_tools.MIN_GAMES_FOR_TIERS` games, as the defense tiers are.
    `projected_points` is the key-free schedule projection the streaming
    planner uses — the start/sit fallback when there are no Vegas totals.
    None without offense rankings or a season.
    """
    pos = (position or "").upper()
    if not season or not (pos in DEFENSE_UNIT_POSITIONS or pos == "K"):
        return None
    rankings, used, fell_back = await _resolve_offense(season, None)
    # Rankings are keyed by canonical codes; a caller's "JAC"/"WSH" missed.
    raw = ((opponent if pos in DEFENSE_UNIT_POSITIONS else team) or "").strip().upper()
    key = normalize_team(raw) or raw
    off = rankings.get(key) if key else None
    if not off:
        return None
    rank = int(off["rank"])
    games = int(off.get("games") or 0)
    # Past seasons are complete; only a live season can be too thin to tier.
    thin = used == season and games < matchup_tools.MIN_GAMES_FOR_TIERS
    tier_rank = rank if pos in DEFENSE_UNIT_POSITIONS else 33 - rank
    tier = "neutral" if thin else matchup_tools._get_matchup_tier(tier_rank)
    detail = ({"opponent_points_per_game": off.get("real_points_avg"), "opponent": opponent}
              if pos in DEFENSE_UNIT_POSITIONS else
              {"own_points_per_game": off.get("real_points_avg")})
    return {
        "offense_rank": rank,
        "offense_side": "opponent" if pos in DEFENSE_UNIT_POSITIONS else "own",
        "points_per_game": off.get("real_points_avg"),
        "games": games,
        "matchup_tier": tier,
        "tier_withheld": thin,
        "source_season": used,
        "is_fallback": fell_back or used != season,
        "projected_points": unit_points(pos, detail, model),
    }


def _blend_unit_week(position: str, team: str, week_row: dict, sleeper_weeks: dict,
                     model) -> None:
    """Blend one streaming week's K/DEF points with Sleeper's, in place.

    Keeps the schedule projection as ``model_projection``; a bye, a week with
    no schedule number or no Sleeper row for the team stays model-only.
    """
    from . import sleeper_projections as sp
    ours = week_row.get("projected_points")
    week_row["model_projection"] = ours
    week_row["projection_source"] = "bye" if week_row.get("on_bye") else "model_only"
    index = sleeper_weeks.get(week_row.get("week")) or {}
    if ours is None or week_row.get("on_bye") or not index.get("by_id"):
        return
    code = normalize_team(team) or team
    row = (index.get("by_def") or {}).get(code) if position in DEFENSE_UNIT_POSITIONS \
        else (index.get("by_k") or {}).get(code)
    if not row:
        return
    theirs = sp.price_stats(row["stats"], model)
    week_row.update({"sleeper_projection": theirs, "projection_source": "sleeper_blend",
                     "projected_points": sp.blend(float(ours), theirs)})


def _needs_offense(positions: list[str]) -> bool:
    return any(p.upper() in ("DST", "DEF", "K") for p in positions)


def _needs_defense(positions: list[str]) -> bool:
    return any(p.upper() in DEFENSE_POSITIONS for p in positions)


async def _rostered_ids(league_id: str) -> tuple[set, bool, str | None]:
    """``(rostered player_ids, rosters usable for availability, why not)``.

    Reserve and taxi players are owned too. A snapshot too old to say who is
    still a free agent is not used for availability at all.
    """
    from . import sleeper_tools
    state = await sleeper_tools.load_rosters(league_id, "availability")
    if state["blocking_error"]:
        return set(), False, state["blocking_error"]
    rostered = {str(pid) for r in state["rosters"]
                for key in ("players", "reserve", "taxi") for pid in (r.get(key) or [])}
    return rostered, bool(state["rosters"]), state["warning"]


def _unit_availability(position: str, team: str, rostered: set, db) -> dict:
    """Availability of a team's streamable unit at ``position`` in the league.

    DST maps 1:1 (Sleeper DST id = team abbreviation). K/QB/TE/RB/WR enumerate the
    team's players at that position from the athletes cache, each flagged
    free_agent/rostered (a specific starter isn't singled out — a design note).
    """
    pos, team = position.upper(), (team or "").upper()
    if pos in DEFENSE_UNIT_POSITIONS:
        status = "rostered" if team in rostered else "free_agent"
        return {"unit_player_id": team, "status": status, "has_free_agent": status == "free_agent"}
    players = []
    # The team's playing options only: a free practice-squad kicker behind a
    # rostered starter is not a streamer (see player_pool).
    team_rows = (db.get_athletes_by_team(team) or []) if db else []
    for a in playing_options(team_rows):
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
    positions: list[str] | None = None,
    strength_season: int | None = None,
    top_n: int = 8,
    league_id: str | None = None,
    only_available: bool = False,
    scoring: str | None = None,
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
        scoring: League scoring label. DST/K options carry `projected_points`
            priced in the league's full scoring_settings when league_id is
            given (points-allowed tiers, FG distance values), else in this
            preset (Sleeper defaults). Reported as `scoring_used`.

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

    positions_given = bool(positions)
    positions = [p.upper() for p in (positions or DEFAULT_STREAM_POSITIONS)]
    skipped_positions: list[str] = []
    if league_id and not positions_given:
        # A league with no K (or no DEF) slot has no use for kicker streamers.
        from . import sleeper_tools
        from .lineup_slots import league_starts
        try:
            league = ((await sleeper_tools.get_league(league_id)) or {}).get("league") or {}
        except Exception:
            league = {}
        skipped_positions = [p for p in positions
                             if not league_starts(league.get("roster_positions"), p)]
        positions = [p for p in positions if p not in skipped_positions]
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
    roster_note = None
    if league_id:
        rostered, rosters_ok, roster_note = await _rostered_ids(league_id)
        availability_active = rosters_ok

    from .projections import _scoring_for
    from .scoring import resolve_scoring
    model = resolve_scoring(await _scoring_for(scoring, league_id))

    # Sleeper's K/DEF projections for the window, blended in as the weekly
    # engine does (see `sleeper_projections.blend`): the same number start/sit
    # shows. Weeks Sleeper has not published stay model-only.
    sleeper_weeks: dict[int, dict] = {}
    if any(p in DEFENSE_UNIT_POSITIONS or p == "K" for p in positions):
        from . import sleeper_projections as sp
        for w in weeks:
            try:
                sleeper_weeks[w] = await sp.fetch_week_projections(season, w)
            except Exception as e:  # the schedule projection must still answer
                logger.debug(f"Sleeper K/DEF projections unavailable for wk{w}: {e}")

    streaming_options: dict[str, list[dict]] = {}
    for pos, teams in scores.items():
        rows = [{"team": team, **data} for team, data in teams.items()]
        if pos in DEFENSE_UNIT_POSITIONS or pos == "K":
            for r in rows:
                pts = []
                for w in r["weeks"]:
                    w["projected_points"] = unit_points(pos, w, model)
                    _blend_unit_week(pos, r["team"], w, sleeper_weeks, model)
                    if w["projected_points"] is not None:
                        pts.append(w["projected_points"])
                r["projected_points"] = round(sum(pts) / len(pts), 1) if pts else None
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
        notes.append(f"Could not load rosters for league {league_id} — availability not annotated"
                     + (f" ({roster_note})" if roster_note else "") + ".")
    elif roster_note:
        notes.append(roster_note)
    if skipped_positions:
        notes.append(f"Skipped {', '.join(skipped_positions)}: the league has no starting slot for it.")
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
        "skipped_positions": skipped_positions,
        "scoring_used": model.summary(),
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
