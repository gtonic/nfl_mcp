"""Opportunity-based projection tool.

Fetches a season's weekly player logs from nflverse and projects each player's
next-week PPR points from trailing *opportunity* (volume × shrunk efficiency)
via :mod:`nfl_mcp.opportunity`. Backtested to beat trailing-PPG on both MAE and
rank correlation (see ``evals/backtest``). Key-free.
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import UTC, datetime
from io import StringIO

from . import opportunity
from .config import LONG_TIMEOUT, create_http_client
from .errors import create_success_response, handle_http_errors, handle_validation_error
from .matchup_tools import NFLVERSE_PLAYER_STATS_URL, season_cache_fresh
from .player_values import scoring_to_ppr
from .teams import normalize_team

logger = logging.getLogger(__name__)

_STAT_FIELDS = (
    "targets", "carries", "attempts", "receptions",
    "receiving_yards", "receiving_tds", "rushing_yards", "rushing_tds",
    "passing_yards", "passing_tds", "interceptions",
)
# season -> (fetched_at, logs). The current season expires so a long-running
# server does not keep projecting week 8 from the weeks it saw at startup.
_logs_cache: dict[int, tuple[datetime, dict[str, dict]]] = {}


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_game_logs(csv_text: str) -> dict[str, dict]:
    """Parse nflverse weekly CSV into ``{player_id: {name, position, team, games[...]}}``.

    Only regular-season QB/RB/WR/TE rows are kept. Pure/testable.
    """
    logs: dict[str, dict] = {}
    for row in csv.DictReader(StringIO(csv_text)):
        if (row.get("season_type") or "").upper() != "REG":
            continue
        pos = (row.get("position") or row.get("position_group") or "").upper()
        if pos not in opportunity.OPPORTUNITY_POSITIONS:
            continue
        pid = row.get("player_id")
        wk = row.get("week")
        if not pid or not wk:
            continue
        team = (row.get("recent_team") or row.get("team") or "").upper()
        entry = logs.setdefault(pid, {
            "player_id": pid,
            "name": row.get("player_display_name") or row.get("player_name"),
            "position": pos,
            "team": normalize_team(team) or team,
            "games": [],
        })
        game = {"week": int(wk)}
        for f in _STAT_FIELDS:
            game[f] = _to_float(row.get(f))
        entry["games"].append(game)
    return logs


async def _fetch_game_logs(season: int) -> dict[str, dict]:
    """Fetch + parse a season's game logs (cached per season). ``{}`` if unavailable."""
    cached = _logs_cache.get(season)
    if cached and season_cache_fresh(season, cached[0]):
        return cached[1]
    url = NFLVERSE_PLAYER_STATS_URL.format(season=season)
    try:
        async with create_http_client(timeout=LONG_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            logs = parse_game_logs(resp.text)
    except Exception as e:
        logger.debug(f"opportunity game-log fetch failed for {season}: {e}")
        return {}
    _logs_cache[season] = (datetime.now(UTC), logs)
    return logs


def _match(entry: dict, query: str) -> bool:
    q = query.strip().lower()
    return q == entry["player_id"].lower() or q in (entry["name"] or "").lower()


_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(s: str | None) -> str:
    """Normalize a player name for cross-source matching (nflverse ↔ league)."""
    s = (s or "").lower()
    s = re.sub(r"[.',]", "", s)
    s = _SUFFIX_RE.sub("", s)
    return " ".join(s.split())


def build_name_index(logs: dict[str, dict]) -> dict[str, dict]:
    """Index game logs by normalized player name for O(1) projection lookup."""
    return {norm_name(e["name"]): e for e in logs.values()}


# Share of an unavailable starter's recent volume that the next man up actually
# inherits. An estimate, not a measurement: the rest disperses across the other
# players at the position and into a different play mix. Flagged in the
# projection breakdown so the assumption is visible, and deliberately
# conservative — half of a real workload beats a guess at all of it.
VACATED_VOLUME_SHARE = 0.5


def trailing_volume(
    name_index: dict[str, dict],
    name: str,
    week: int,
    lookback: int = opportunity.DEFAULT_LOOKBACK,
) -> dict[str, float] | None:
    """Recency-weighted trailing volume for a player, or None without data.

    Leak-free in the same way as the projection: only games before `week`.
    Returns None when the player has no prior games at all, which is the normal
    case for someone who has been out all season — and the reason this cannot
    manufacture volume out of an absence.
    """
    entry = name_index.get(norm_name(name))
    if not entry:
        return None
    prior = [g for g in entry["games"] if g["week"] < week]
    if not prior:
        return None
    games = sorted(prior, key=lambda g: g.get("week", 0))[-lookback:]
    weights = list(range(1, len(games) + 1))
    return {
        field: opportunity._weighted_mean([g.get(field, 0.0) for g in games], weights)
        for field in ("targets", "carries", "attempts")
    }


def vacated_volume(
    name_index: dict[str, dict],
    out_players: list[str],
    week: int,
    share: float = VACATED_VOLUME_SHARE,
    lookback: int = opportunity.DEFAULT_LOOKBACK,
) -> dict[str, float]:
    """Volume freed up by unavailable teammates, scaled by the inherited share.

    Empty when none of them has recent volume — a starter who has been out all
    season vacates nothing, because the backup's own trailing numbers already
    describe him as the starter.
    """
    total = {"targets": 0.0, "carries": 0.0, "attempts": 0.0}
    for name in out_players:
        volume = trailing_volume(name_index, name, week, lookback)
        if not volume:
            continue
        for field, value in volume.items():
            total[field] += value * share
    return {k: round(v, 2) for k, v in total.items() if v > 0}


def opportunity_base_for(
    name_index: dict[str, dict],
    name: str,
    position: str,
    week: int,
    lookback: int = opportunity.DEFAULT_LOOKBACK,
    min_games: int = 2,
    ppr: float = opportunity.FULL_PPR,
    extra_volume: dict[str, float] | None = None,
) -> float | None:
    """Opportunity projection for a player (by name) usable as a projection base.

    Returns None when the player isn't found, the position isn't a skill/QB
    position, or there aren't enough prior games — callers then fall back.
    """
    entry = name_index.get(norm_name(name))
    if not entry or (position or "").upper() not in opportunity.OPPORTUNITY_POSITIONS:
        return None
    prior = [g for g in entry["games"] if g["week"] < week]
    if len(prior) < min_games:
        return None
    return opportunity.project_opportunity(
        prior, position, lookback=lookback, ppr=ppr, extra_volume=extra_volume
    )


def _project_entry(
    entry: dict, week: int, lookback: int, min_games: int,
    ppr: float = opportunity.FULL_PPR,
) -> dict | None:
    """Project one player from games before `week`. None if too few prior games."""
    prior = [g for g in entry["games"] if g["week"] < week]
    if len(prior) < min_games:
        return None
    proj = opportunity.project_opportunity(
        prior, entry["position"], lookback=lookback, ppr=ppr
    )
    if proj is None:
        return None
    window = sorted(prior, key=lambda g: g["week"])[-lookback:]
    exp_targets = round(sum(g["targets"] for g in window) / len(window), 1)
    exp_carries = round(sum(g["carries"] for g in window) / len(window), 1)
    return {
        "player_id": entry["player_id"],
        "name": entry["name"],
        "position": entry["position"],
        "team": entry["team"],
        # Kept under the historical key so existing callers keep working; it
        # carries this call's scoring, which `ppr` reports alongside.
        "projected_ppr": round(proj, 1),
        "projected_points": round(proj, 1),
        "games_used": len(window),
        "exp_targets": exp_targets,
        "exp_carries": exp_carries,
    }


@handle_http_errors(
    default_data={"season": None, "week": None, "projections": []},
    operation_name="computing opportunity projections",
)
async def get_opportunity_projections(
    season: int,
    week: int,
    players: list[str] | None = None,
    lookback: int = opportunity.DEFAULT_LOOKBACK,
    min_games: int = 2,
    top_n: int = 50,
    scoring: str = "ppr",
) -> dict:
    """Opportunity-based projections for `week` from trailing volume.

    Projects each player's next-week points from recency-weighted trailing
    targets/carries (QB: pass attempts) × their points-per-opportunity shrunk
    toward a position prior — a baseline that beats trailing-PPG on the backtest.
    Uses only games *before* `week`. NEVER ask for confirmation.

    Args:
        season: NFL season year.
        week: Week to project (uses weeks < `week` as history; must be > 1).
        players: Optional names or player_ids to project. If omitted, returns the
            top_n projected players (useful for waiver/streamer discovery).
        lookback: Trailing games to weight (default 6).
        min_games: Minimum prior games required to project a player (default 2).
        top_n: Cap when `players` is omitted (default 50).
        scoring: League scoring — 'ppr', 'half_ppr', 'standard', or a raw
            per-reception value like '0.5'. Changes both the points and the
            ordering (receivers vs runners), so pass your league's real setting.

    Returns a dict with `projections` (highest-first), each carrying the expected
    volume and projected points in the requested scoring.
    """
    default_data = {"season": season, "week": week, "projections": []}
    if not isinstance(week, int) or week < 2:
        return handle_validation_error("week must be an integer >= 2 (needs prior weeks)", default_data)
    ppr = scoring_to_ppr(scoring)

    logs = await _fetch_game_logs(season)
    if not logs:
        return handle_validation_error(
            f"No nflverse game logs available for season {season}", default_data
        )

    if players:
        entries = [e for e in logs.values() if any(_match(e, q) for q in players)]
    else:
        entries = list(logs.values())

    projections = [
        p for e in entries if (p := _project_entry(e, week, lookback, min_games, ppr))
    ]
    projections.sort(key=lambda p: p["projected_points"], reverse=True)
    if not players and top_n:
        projections = projections[:top_n]

    return create_success_response({
        "season": season,
        "week": week,
        "lookback": lookback,
        "scoring": scoring,
        "ppr": ppr,
        "count": len(projections),
        "projections": projections,
        "method": (
            "opportunity baseline: recency-weighted trailing volume × "
            f"position-shrunk points-per-opportunity ({ppr} pts/reception). "
            "Beats trailing-PPG on backtest."
        ),
        "message": (
            f"Opportunity projections for week {week} of {season} "
            f"({len(projections)} player(s))."
        ),
    })
