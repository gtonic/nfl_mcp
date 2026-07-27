"""Strength-of-schedule (SOS) tools.

Rank NFL teams by how easy or hard their upcoming — or fantasy-playoff-week
(15-17) — schedule is, per fantasy position, using defense-vs-position
rankings. Purely additive: this does NOT touch the projection engine. Useful
for rest-of-season planning, trade-deadline calls and playoff-week stash
decisions (which streamer / stash has the softest championship-run schedule).

Defense strength comes from ``matchup_tools`` (nflverse-derived: rank 1 =
toughest defense, 32 = easiest). Before a season has data (preseason) the prior
season is used as the strength prior; the response reports which season was used
and whether it fell back to placeholder data.
"""
from __future__ import annotations

import logging

from . import matchup_tools, sleeper_tools
from .errors import create_success_response, handle_http_errors, handle_validation_error

logger = logging.getLogger(__name__)

DEFAULT_SOS_POSITIONS = ["QB", "RB", "WR", "TE"]
PLAYOFF_WEEKS = (15, 16, 17)
_WEEK_MIN = 1
_WEEK_MAX = 18


def _ease_score(rank: float) -> float:
    """Map a defense rank (1 = toughest .. 32 = easiest) to a 0-100 ease score.

    Higher = easier schedule. Rank 1 -> 0.0, rank 32 -> 100.0.
    """
    r = max(1.0, min(32.0, float(rank)))
    return round((r - 1) / 31 * 100, 1)


def _all_fallback(rankings: dict[str, list[dict]] | None) -> bool:
    """True when every ranking entry is placeholder data (no live season yet)."""
    if not rankings:
        return True
    for teams in rankings.values():
        for team in teams:
            if not team.get("is_fallback"):
                return False
    return True


def compute_team_sos(
    opponents_by_week: dict[str, dict[int, str | None]],
    rankings: dict[str, list[dict]],
    positions: list[str],
    analyzer,
) -> dict[str, dict]:
    """Pure aggregation of per-team, per-position SOS over the given weeks.

    Args:
        opponents_by_week: ``{team: {week: opponent_abbr_or_None}}``.
        rankings: defense rankings from ``fetch_defense_rankings``.
        positions: positions to grade (e.g. ``["QB", "RB", "WR", "TE"]``).
        analyzer: a ``DefenseRankingsAnalyzer`` (used only for
            ``get_matchup_difficulty``), so this stays unit-testable with a stub.

    Returns ``{team: {games, bye_or_missing_weeks, positions{...}, overall_ease_score}}``.
    """
    result: dict[str, dict] = {}
    for team, wk_opps in opponents_by_week.items():
        games = [(wk, opp) for wk, opp in sorted(wk_opps.items()) if opp]
        missing = [wk for wk, opp in sorted(wk_opps.items()) if not opp]

        pos_summary: dict[str, dict] = {}
        for pos in positions:
            per_week = []
            ranks: list[float] = []
            fallback_any = False
            for wk, opp in games:
                m = analyzer.get_matchup_difficulty(pos, opp, rankings)
                rank = m.get("rank", 16)
                is_fb = bool(m.get("is_fallback", False))
                fallback_any = fallback_any or is_fb
                ranks.append(rank)
                per_week.append({
                    "week": wk,
                    "opponent": opp,
                    "rank": rank,
                    "matchup_tier": m.get("matchup_tier"),
                    "is_fallback": is_fb,
                })
            if ranks:
                avg_rank = round(sum(ranks) / len(ranks), 1)
                pos_summary[pos] = {
                    "avg_opponent_defense_rank": avg_rank,
                    "ease_score": _ease_score(avg_rank),
                    "is_fallback": fallback_any,
                    "weeks": per_week,
                }

        overall = None
        if pos_summary:
            overall = round(
                sum(p["ease_score"] for p in pos_summary.values()) / len(pos_summary), 1
            )
        result[team] = {
            "games": len(games),
            "bye_or_missing_weeks": missing,
            "positions": pos_summary,
            "overall_ease_score": overall,
        }
    return result


async def _resolve_rankings(analyzer, season: int, strength_season: int | None):
    """Return ``(rankings, used_season, is_fallback)``.

    Uses ``strength_season`` if given; otherwise tries ``season`` and, when that
    has no live data yet (preseason), falls back to the prior season so SOS is
    still meaningful before Week 1.
    """
    target = strength_season if strength_season is not None else season
    rankings = await analyzer.fetch_defense_rankings(target)
    used, fell_back = target, _all_fallback(rankings)

    if fell_back and strength_season is None:
        prior = target - 1
        prior_rankings = await analyzer.fetch_defense_rankings(prior)
        if not _all_fallback(prior_rankings):
            rankings, used, fell_back = prior_rankings, prior, False
    return rankings, used, fell_back


async def _gather_opponents(db, season: int, weeks: list[int]) -> dict[str, dict[int, str | None]]:
    """Build ``{team: {week: opponent}}`` for the weeks, cache-first then fetch."""
    opponents: dict[str, dict[int, str | None]] = {}
    teams = list(matchup_tools.ESPN_TEAM_MAP.values())
    for wk in weeks:
        week_map: dict[str, str] = {}
        if db:
            for team in teams:
                try:
                    opp = db.get_opponent(season, wk, team)
                except Exception:
                    opp = None
                if opp:
                    week_map[team] = opp
        if not week_map:
            # Cache miss for this week: fetch from ESPN (force past the enrich gate).
            fetched = await sleeper_tools._fetch_week_schedule(season, wk, force=True)
            if fetched and db:
                try:
                    db.upsert_schedule_games(fetched)  # warm the cache
                except Exception as e:
                    logger.debug(f"schedule cache warm failed (week {wk}): {e}")
            for g in fetched or []:
                if g.get("team") and g.get("opponent"):
                    week_map[g["team"]] = g["opponent"]
        for team, opp in week_map.items():
            opponents.setdefault(team, {})[wk] = opp
    return opponents


def _ranked_views(sos: dict[str, dict], positions: list[str]) -> dict:
    """Turn the per-team SOS map into easiest-first ranked lists."""
    by_position: dict[str, list[dict]] = {}
    for pos in positions:
        rows = []
        for team, data in sos.items():
            p = data["positions"].get(pos)
            if p is None:
                continue
            rows.append({
                "team": team,
                "avg_opponent_defense_rank": p["avg_opponent_defense_rank"],
                "ease_score": p["ease_score"],
                "is_fallback": p["is_fallback"],
                "weeks": p["weeks"],
            })
        rows.sort(key=lambda r: r["ease_score"], reverse=True)  # easiest first
        for i, r in enumerate(rows, 1):
            r["sos_rank"] = i  # 1 = easiest schedule
        by_position[pos] = rows

    overall = [
        {"team": team, "overall_ease_score": data["overall_ease_score"], "games": data["games"]}
        for team, data in sos.items()
        if data["overall_ease_score"] is not None
    ]
    overall.sort(key=lambda r: r["overall_ease_score"], reverse=True)
    for i, r in enumerate(overall, 1):
        r["sos_rank"] = i
    return {"by_position": by_position, "overall": overall}


@handle_http_errors(
    default_data={"season": None, "weeks": [], "by_position": {}, "overall": []},
    operation_name="computing strength of schedule",
)
async def get_strength_of_schedule(
    season: int,
    start_week: int,
    end_week: int,
    positions: list[str] | None = None,
    strength_season: int | None = None,
) -> dict:
    """Rank NFL teams by schedule difficulty over a week range, per position.

    "Ease score" is 0-100 (higher = easier schedule; a team facing the weakest
    defenses scores high). Teams are ranked easiest-first (``sos_rank`` 1 =
    softest schedule). Defense strength defaults to the target season, falling
    back to the prior season before the season has data.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        season: NFL season year for the schedule.
        start_week: First regular-season week to include (1-18).
        end_week: Last regular-season week to include (>= start_week, <= 18).
        positions: Positions to grade (default QB/RB/WR/TE).
        strength_season: Season whose defense rankings to use as the strength
            prior. Defaults to auto (target season, else prior season).

    Returns a dict with by_position and overall easiest-first rankings, plus
    ``strength_source_season`` / ``strength_is_fallback`` transparency fields.
    """
    default_data = {"season": season, "weeks": [], "by_position": {}, "overall": []}

    if not isinstance(start_week, int) or not isinstance(end_week, int):
        return handle_validation_error("start_week and end_week must be integers", default_data)
    if not (_WEEK_MIN <= start_week <= _WEEK_MAX) or not (_WEEK_MIN <= end_week <= _WEEK_MAX):
        return handle_validation_error(
            f"Weeks must be between {_WEEK_MIN} and {_WEEK_MAX}", default_data
        )
    if start_week > end_week:
        return handle_validation_error("start_week must be <= end_week", default_data)

    positions = [p.upper() for p in (positions or DEFAULT_SOS_POSITIONS)]
    weeks = list(range(start_week, end_week + 1))

    analyzer = matchup_tools.get_defense_analyzer()
    db = getattr(analyzer, "db", None)

    rankings, used_season, fell_back = await _resolve_rankings(analyzer, season, strength_season)
    opponents = await _gather_opponents(db, season, weeks)

    if not opponents:
        return handle_validation_error(
            f"No schedule available for season {season}, weeks {start_week}-{end_week}",
            default_data,
        )

    sos = compute_team_sos(opponents, rankings, positions, analyzer)
    views = _ranked_views(sos, positions)

    message = (
        f"Strength of schedule for weeks {start_week}-{end_week} of {season}, "
        f"graded on {used_season} defense rankings."
    )
    if fell_back:
        message += (
            " ⚠️ No live defense data available — ratings are placeholder/neutral; "
            "treat the SOS as low-confidence."
        )

    return create_success_response({
        "season": season,
        "weeks": weeks,
        "positions": positions,
        "strength_source_season": used_season,
        "strength_is_fallback": fell_back,
        "by_position": views["by_position"],
        "overall": views["overall"],
        "ease_score_explained": (
            "0-100, higher = easier schedule. Derived from average opponent "
            "defense rank (1 = toughest defense, 32 = easiest)."
        ),
        "message": message,
    })


@handle_http_errors(
    default_data={"season": None, "weeks": [], "by_position": {}, "overall": []},
    operation_name="computing playoff strength of schedule",
)
async def get_playoff_sos(
    season: int,
    positions: list[str] | None = None,
    strength_season: int | None = None,
) -> dict:
    """Strength of schedule for the fantasy playoff weeks (15-17).

    Convenience wrapper around :func:`get_strength_of_schedule` for championship-run
    planning (trade-deadline and stash decisions). NEVER ask for confirmation.
    """
    return await get_strength_of_schedule(
        season,
        PLAYOFF_WEEKS[0],
        PLAYOFF_WEEKS[-1],
        positions=positions,
        strength_season=strength_season,
    )
