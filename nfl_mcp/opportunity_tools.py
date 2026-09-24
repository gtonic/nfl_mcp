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
from .scoring import ScoringModel, league_scoring, resolve_scoring
from .teams import normalize_team

logger = logging.getLogger(__name__)

_STAT_FIELDS = (
    "targets", "carries", "attempts", "receptions",
    "receiving_yards", "receiving_tds", "rushing_yards", "rushing_tds",
    "passing_yards", "passing_tds", "interceptions",
    # Priced only when the league scores them (see `scoring.ScoringModel`).
    "completions", "sacks_suffered", "sack_fumbles", "sack_fumbles_lost",
    "passing_first_downs", "passing_2pt_conversions",
    "rushing_fumbles", "rushing_fumbles_lost", "rushing_first_downs",
    "rushing_2pt_conversions",
    "receiving_fumbles", "receiving_fumbles_lost", "receiving_first_downs",
    "receiving_2pt_conversions",
)
# nflverse renamed some columns in the `stats_player` release; read the new
# name first. `interceptions` used to be read as-is and was always 0 in the
# current files, so no QB was ever charged for an interception.
_COLUMN_ALIASES = {"interceptions": ("passing_interceptions", "interceptions"),
                   "sacks_suffered": ("sacks_suffered", "sacks")}
# nflverse's own per-game usage shares (0-1 fractions), kept for the usage
# trends tool. None when the row leaves them blank — a zero share and an
# unreported one are different things. Not read by the projection.
_USAGE_FIELDS = ("target_share", "air_yards_share", "wopr", "racr", "receiving_air_yards")
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
        # The team he played *for* that week: a traded player's share is of
        # the team he was on then, not the one he is on now.
        opp = (row.get("opponent_team") or "").upper()
        game = {"week": int(wk), "team": normalize_team(team) or team,
                "opponent": (normalize_team(opp) or opp) or None}
        for f in _USAGE_FIELDS:
            v = row.get(f)
            game[f] = _to_float(v) if v not in (None, "", "NA") else None
        for f in _STAT_FIELDS:
            cols = _COLUMN_ALIASES.get(f, (f,))
            game[f] = _to_float(next((row[c] for c in cols if row.get(c) not in (None, "")), None))
        # The player's current team is his latest week's: a traded player
        # kept the team of whichever row came first.
        if game["team"] and game["week"] >= max((g["week"] for g in entry["games"]), default=0):
            entry["team"] = game["team"]
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


def usage_sample(
    name_index: dict[str, dict],
    name: str,
    week: int,
    lookback: int = opportunity.DEFAULT_LOOKBACK,
) -> dict | None:
    """How much real usage history backs a player's projection, and how steady.

    ``games`` is the number of prior games in the lookback window; ``volume_cv``
    the coefficient of variation of his weekly opportunities (targets + carries
    + pass attempts) over them — a role that swings from 2 to 9 targets is a
    less certain projection than one that sits at 6 every week. None without
    prior games.
    """
    entry = name_index.get(norm_name(name))
    if not entry:
        return None
    prior = [g for g in entry["games"] if g["week"] < week]
    if not prior:
        return None
    games = sorted(prior, key=lambda g: g.get("week", 0))[-lookback:]
    volumes = [
        float(g.get("targets", 0.0) or 0.0) + float(g.get("carries", 0.0) or 0.0)
        + float(g.get("attempts", 0.0) or 0.0)
        for g in games
    ]
    mean = sum(volumes) / len(volumes)
    if len(volumes) < 2 or mean <= 0:
        cv = None
    else:
        var = sum((v - mean) ** 2 for v in volumes) / len(volumes)
        cv = round(var ** 0.5 / mean, 3)
    return {"games": len(games), "volume_cv": cv}


def vacated_volume(
    name_index: dict[str, dict],
    out_players: list[str],
    week: int,
    share: float | dict[str, float] = VACATED_VOLUME_SHARE,
    lookback: int = opportunity.DEFAULT_LOOKBACK,
) -> dict[str, float]:
    """Volume freed up by unavailable teammates, scaled by the inherited share.

    `share` is one fraction for every out player, or ``{name: fraction}`` —
    this player's slice of each starter's volume (see
    ``projections._inherited_shares``), so the teammates below a starter split
    his volume instead of each inheriting all of it.

    Empty when none of them has recent volume — a starter who has been out all
    season vacates nothing, because the backup's own trailing numbers already
    describe him as the starter.
    """
    total = {"targets": 0.0, "carries": 0.0, "attempts": 0.0}
    for name in out_players:
        fraction = share.get(name, 0.0) if isinstance(share, dict) else share
        volume = trailing_volume(name_index, name, week, lookback)
        if not volume or fraction <= 0:
            continue
        for field, value in volume.items():
            total[field] += value * fraction
    return {k: round(v, 2) for k, v in total.items() if v > 0}


def opportunity_base_for(
    name_index: dict[str, dict],
    name: str,
    position: str,
    week: int,
    lookback: int | None = None,
    min_games: int = 2,
    ppr: float = opportunity.FULL_PPR,
    extra_volume: dict[str, float] | None = None,
    scoring: ScoringModel | None = None,
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
        prior, position, lookback=lookback, ppr=ppr, extra_volume=extra_volume,
        scoring=scoring,
    )


def _project_entry(
    entry: dict, week: int, lookback: int, min_games: int,
    ppr: float = opportunity.FULL_PPR,
    scoring: ScoringModel | None = None,
) -> dict | None:
    """Project one player from games before `week`. None if too few prior games."""
    prior = [g for g in entry["games"] if g["week"] < week]
    if len(prior) < min_games:
        return None
    proj = opportunity.project_opportunity(
        prior, entry["position"], lookback=lookback, ppr=ppr, scoring=scoring
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


async def _league_model(league_id: str) -> ScoringModel | None:
    """The league's full scoring model, or None when it cannot be loaded."""
    try:
        from . import sleeper_tools
        league = ((await sleeper_tools.get_league(league_id)) or {}).get("league") or {}
    except Exception as e:  # a league lookup must not sink the projection
        logger.debug(f"league lookup for scoring failed: {e}")
        return None
    return league_scoring(league).model if league else None


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
    league_id: str | None = None,
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
        league_id: Sleeper league id. When given, the league's full
            scoring_settings are used (pass TD / INT values, fumbles, TE
            premium, first downs, yardage bonuses) instead of the preset;
            reported as `scoring_used`.

    Returns a dict with `projections` (highest-first), each carrying the expected
    volume and projected points in the requested scoring.
    """
    default_data = {"season": season, "week": week, "projections": []}
    if not isinstance(week, int) or week < 2:
        return handle_validation_error("week must be an integer >= 2 (needs prior weeks)", default_data)
    model = resolve_scoring(scoring)
    if league_id:
        model = await _league_model(league_id) or model
    ppr = model.rec if league_id else scoring_to_ppr(scoring)

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
        p for e in entries
        if (p := _project_entry(e, week, lookback, min_games, ppr, model))
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
        "scoring_used": model.summary(),
        "count": len(projections),
        "projections": projections,
        "method": (
            "opportunity baseline: recency-weighted trailing volume × "
            f"position-shrunk points-per-opportunity ({ppr} pts/reception, "
            "every other stat at the league's scoring). "
            "Beats trailing-PPG on backtest."
        ),
        "message": (
            f"Opportunity projections for week {week} of {season} "
            f"({len(projections)} player(s))."
        ),
    })
