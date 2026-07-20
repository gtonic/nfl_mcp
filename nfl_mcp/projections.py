"""
Transparent weekly fantasy point projections.

Instead of scraping a fragile third-party page, this builds a projection from
signals the server already has:

    projected = base_ppg(position_rank)   # talent/role baseline (FantasyCalc rank)
              × matchup_multiplier         # defense vs position (matchup_tools)
              × environment_multiplier      # Vegas implied team total (vegas_tools)
              × usage_multiplier            # snap% / usage trend (enrichment)
              × injury_multiplier           # availability

Every factor is reported in a `breakdown` so the number is explainable, and a
`confidence` reflects how many real signals were available. No API key needed
(FantasyCalc + ESPN); Vegas is optional (ODDS_API_KEY improves it).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .player_values import get_values_service, scoring_to_ppr
from .matchup_tools import get_defense_analyzer
from .vegas_tools import get_vegas_analyzer
from .errors import create_success_response, create_error_response, ErrorType, handle_http_errors

logger = logging.getLogger(__name__)

VBD_POSITIONS = {"QB", "RB", "WR", "TE"}


def base_ppg(position: str, pos_rank: Optional[int]) -> float:
    """Baseline PPR points/game from a player's positional rank."""
    p = (position or "").upper()
    r = pos_rank if (isinstance(pos_rank, int) and pos_rank > 0) else 999
    if p == "QB":
        return 22.0 if r <= 3 else 20.0 if r <= 8 else 18.0 if r <= 12 else 16.0 if r <= 20 else 14.0
    if p == "RB":
        return 19.0 if r <= 3 else 16.0 if r <= 8 else 13.0 if r <= 15 else 11.0 if r <= 24 else 8.5 if r <= 36 else 6.0
    if p == "WR":
        return 17.0 if r <= 5 else 14.5 if r <= 12 else 12.0 if r <= 24 else 9.5 if r <= 36 else 7.5 if r <= 48 else 5.5
    if p == "TE":
        return 14.0 if r <= 3 else 11.0 if r <= 6 else 8.5 if r <= 12 else 6.5 if r <= 20 else 5.0
    if p == "K":
        return 8.0
    if p in ("DST", "DEF"):
        return 7.0
    return 8.0


# Tier -> baseline point-swing from an average matchup (before position scaling).
_MATCHUP_TIER_DEV = {"smash": 0.10, "favorable": 0.05, "neutral": 0.0,
                     "tough": -0.05, "elite": -0.10, "unknown": 0.0}
# How much each position's projection actually moves with matchup. Data-driven
# from evals/backtest (2023-24, n~5.2k): RB matchup matters most (full weight),
# TE half, QB a little, WR essentially not at all (talent dominates). A flat
# ±10% over-adjusted overall — see evals/README.md.
_MATCHUP_POS_STRENGTH = {"RB": 1.0, "TE": 0.5, "QB": 0.25, "WR": 0.0}
_DEFAULT_POS_STRENGTH = 0.3


def matchup_multiplier(position: str, tier: str) -> float:
    """Position-aware matchup multiplier (see evals/backtest for the tuning)."""
    dev = _MATCHUP_TIER_DEV.get(tier, 0.0)
    strength = _MATCHUP_POS_STRENGTH.get((position or "").upper(), _DEFAULT_POS_STRENGTH)
    return round(1.0 + strength * dev, 4)

# Higher fantasy scoring variance = wider floor/ceiling band.
_VOLATILITY = {"QB": 0.22, "RB": 0.30, "WR": 0.38, "TE": 0.40, "K": 0.35, "DST": 0.45, "DEF": 0.45}


def _environment_mult(implied_total: Optional[float], is_fallback: bool) -> float:
    if is_fallback or implied_total is None:
        return 1.0
    if implied_total >= 28:
        return 1.08
    if implied_total >= 25:
        return 1.04
    if implied_total >= 21:
        return 1.0
    if implied_total >= 18:
        return 0.96
    return 0.92


def _usage_mult(snap_pct: Optional[float], usage_trend: Optional[str]) -> float:
    mult = 1.0
    if snap_pct:
        if snap_pct >= 80:
            mult *= 1.05
        elif snap_pct < 40:
            mult *= 0.92
    t = (usage_trend or "").lower()
    if t in ("up", "upward"):
        mult *= 1.05
    elif t in ("down", "downward"):
        mult *= 0.95
    return mult


def _injury_mult(status: Optional[str]) -> float:
    s = (status or "").lower()
    if s in ("out", "ir", "suspended", "pup", "injured reserve"):
        return 0.0
    if s == "doubtful":
        return 0.35
    if s == "questionable":
        return 0.9
    return 1.0


class ProjectionEngine:
    """Projects fantasy points by combining value, matchup, environment, usage."""

    def __init__(self, db=None):
        self.values = get_values_service(db)
        self.defense = get_defense_analyzer()
        self.vegas = get_vegas_analyzer()

    def _project_one(self, player: Dict, values_index: Dict, rankings: Dict, lines: Dict) -> Dict:
        name = player.get("name") or player.get("player_name")
        position = (player.get("position") or "").upper()
        team = (player.get("team") or "").upper()
        opponent = (player.get("opponent") or "").upper()
        player_id = player.get("player_id")
        usage = player.get("usage") or {}
        injury = player.get("injury") or {}

        # 1) Baseline from positional rank (real market data when available)
        market = self.values.lookup(values_index, player_id=player_id, name=name, position=position)
        pos_rank = (market or {}).get("position_rank")
        base = base_ppg(position, pos_rank)

        # 2) Matchup vs opponent defense
        matchup_tier = "unknown"
        if position in VBD_POSITIONS and opponent:
            try:
                m = self.defense.get_matchup_difficulty(position, opponent, rankings)
                matchup_tier = m.get("matchup_tier", "unknown")
            except Exception:
                matchup_tier = "unknown"
        matchup_mult = matchup_multiplier(position, matchup_tier)

        # 3) Game environment (Vegas implied team total)
        implied_total = None
        env_is_fallback = True
        if team:
            try:
                game = self.vegas.get_game_lines(team, lines)
                is_home = game.get("home_team") == team
                implied_total = game.get("home_implied_total") if is_home else game.get("away_implied_total")
                env_is_fallback = bool(game.get("is_fallback"))
            except Exception:
                pass
        env_mult = _environment_mult(implied_total, env_is_fallback)

        # 4) Usage & 5) injury
        usage_mult = _usage_mult(usage.get("snap_percentage"), usage.get("usage_trend"))
        inj_mult = _injury_mult(injury.get("status"))

        projected = round(base * matchup_mult * env_mult * usage_mult * inj_mult, 1)
        vol = _VOLATILITY.get(position, 0.35)
        if inj_mult == 0.0:
            floor = ceiling = 0.0
        else:
            floor = round(projected * (1 - vol), 1)
            ceiling = round(projected * (1 + vol), 1)

        # Confidence = how many real signals we had
        conf = 50
        if market:
            conf += 20
        if not env_is_fallback:
            conf += 15
        if usage:
            conf += 15
        conf = min(conf, 100)
        conf_level = "high" if conf >= 80 else "medium" if conf >= 60 else "low"

        return {
            "player": name,
            "position": position,
            "team": team,
            "opponent": opponent,
            "projected_points": projected,
            "floor": floor,
            "ceiling": ceiling,
            "confidence": conf,
            "confidence_level": conf_level,
            "matchup_tier": matchup_tier,
            "implied_total": implied_total,
            "breakdown": {
                "base_ppg": base,
                "position_rank": pos_rank,
                "matchup_mult": round(matchup_mult, 3),
                "environment_mult": round(env_mult, 3),
                "usage_mult": round(usage_mult, 3),
                "injury_mult": round(inj_mult, 3),
            },
            "value_source": "fantasycalc" if market else "baseline",
        }

    async def project_many(
        self, players: List[Dict], scoring: str = "ppr", superflex: bool = False, num_teams: int = 12
    ) -> Dict:
        values_index = await self.values.get_values(
            scoring_to_ppr(scoring), 2 if superflex else 1, num_teams, False
        )
        try:
            rankings = await self.defense.fetch_defense_rankings()
        except Exception:
            rankings = {}
        try:
            lines = await self.vegas.fetch_current_lines()
        except Exception:
            lines = {}
        projections = [self._project_one(p, values_index, rankings, lines) for p in players]
        return {
            "projections": projections,
            "values_source": values_index.get("source"),
            "vegas_active": bool(lines),
        }


_engine: Optional[ProjectionEngine] = None


def get_projection_engine(db=None) -> ProjectionEngine:
    global _engine
    if _engine is None:
        _engine = ProjectionEngine(db=db)
    return _engine


# ==========================================================================
# MCP Tool Functions
# ==========================================================================

@handle_http_errors(default_data={"projections": []}, operation_name="projecting players")
async def project_players(
    players: List[Dict],
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    db=None,
) -> Dict:
    """Project weekly fantasy points for multiple players (transparent, no scraping).

    Args:
        players: list of dicts with name, position, team, opponent, and optional
            usage {snap_percentage, usage_trend} and injury {status}.
        scoring/superflex/num_teams: league format for the value baseline.

    Returns: {projections:[{projected_points, floor, ceiling, confidence, breakdown, ...}]}
    """
    if not players:
        return create_error_response("No players provided", ErrorType.VALIDATION, {"projections": []})
    engine = get_projection_engine(db)
    result = await engine.project_many(players, scoring=scoring, superflex=superflex, num_teams=num_teams)
    return create_success_response({
        **result,
        "total": len(result["projections"]),
        "message": f"Projected {len(result['projections'])} players ({result.get('values_source')})",
    })


@handle_http_errors(default_data={"projection": None}, operation_name="projecting player")
async def project_player(
    player_name: str,
    position: str,
    team: str,
    opponent: str,
    snap_percentage: Optional[float] = None,
    usage_trend: Optional[str] = None,
    injury_status: Optional[str] = None,
    scoring: str = "ppr",
    superflex: bool = False,
    db=None,
) -> Dict:
    """Project weekly fantasy points for a single player.

    Returns: {projection: {projected_points, floor, ceiling, confidence, breakdown, ...}}
    """
    player = {
        "name": player_name, "position": position, "team": team, "opponent": opponent,
        "usage": {"snap_percentage": snap_percentage, "usage_trend": usage_trend},
        "injury": {"status": injury_status},
    }
    engine = get_projection_engine(db)
    result = await engine.project_many([player], scoring=scoring, superflex=superflex)
    proj = result["projections"][0] if result["projections"] else None
    return create_success_response({
        "projection": proj,
        "values_source": result.get("values_source"),
        "message": (f"{player_name}: {proj['projected_points']} pts "
                    f"(floor {proj['floor']}, ceiling {proj['ceiling']}, {proj['confidence_level']} conf)"
                    if proj else "No projection"),
    })
