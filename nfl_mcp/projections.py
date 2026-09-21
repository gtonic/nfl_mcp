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

`scoring` sets the points scale, not just which market values are consulted:
both baselines are rebased to the league's per-reception value, so a half-PPR
league gets half-PPR points and the receiver-vs-runner ordering that follows
from them.
"""

from __future__ import annotations

import logging

from . import opportunity_tools
from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .matchup_tools import get_defense_analyzer
from .player_values import get_values_service, scoring_to_ppr
from .teams import normalize_team
from .vegas_tools import get_vegas_analyzer
from .weather_tools import weather_multiplier

logger = logging.getLogger(__name__)

VBD_POSITIONS = {"QB", "RB", "WR", "TE"}


# Share of a full-PPR baseline that *is* the per-reception bonus, by position —
# i.e. receptions per game divided by PPR points per game (a WR averaging 14.5
# PPR points catches roughly 4.5 balls). Removing `(1 - ppr) × share` rebases the
# bucket from full PPR to the league's actual reception value.
_RECEPTION_SHARE = {"WR": 0.31, "TE": 0.36, "RB": 0.23, "QB": 0.0, "K": 0.0,
                    "DST": 0.0, "DEF": 0.0}


def base_ppg(position: str, pos_rank: int | None, ppr: float = 1.0) -> float:
    """Baseline points/game from a player's positional rank.

    Buckets are full-PPR and then rebased to `ppr` (1.0 full, 0.5 half, 0.0
    standard). This is the fallback baseline — it is used before a player has
    enough games for the opportunity projection — so the rebasing is a
    position-average estimate rather than a per-player reception count.
    """
    p = (position or "").upper()
    r = pos_rank if (isinstance(pos_rank, int) and pos_rank > 0) else 999
    if p == "QB":
        full = 22.0 if r <= 3 else 20.0 if r <= 8 else 18.0 if r <= 12 else 16.0 if r <= 20 else 14.0
    elif p == "RB":
        full = 19.0 if r <= 3 else 16.0 if r <= 8 else 13.0 if r <= 15 else 11.0 if r <= 24 else 8.5 if r <= 36 else 6.0
    elif p == "WR":
        full = 17.0 if r <= 5 else 14.5 if r <= 12 else 12.0 if r <= 24 else 9.5 if r <= 36 else 7.5 if r <= 48 else 5.5
    elif p == "TE":
        full = 14.0 if r <= 3 else 11.0 if r <= 6 else 8.5 if r <= 12 else 6.5 if r <= 20 else 5.0
    elif p == "K":
        full = 8.0
    elif p in ("DST", "DEF"):
        full = 7.0
    else:
        full = 8.0
    return round(full * (1.0 - (1.0 - ppr) * _RECEPTION_SHARE.get(p, 0.0)), 2)


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

# Higher fantasy scoring variance = wider floor/ceiling band. floor/ceiling are
# `mean ± volatility·mean`, i.e. a ±1σ band under the Normal the win-probability
# optimizer assumes, so reality should land inside ~68% of the time.
#
# Measured by evals/backtest/calibration.py (2023-24, n~5.2k player-weeks): the
# hand-picked values covered only 36%, the stated floor was breached 36% of the
# time rather than 16%, and the win probability was badly over-confident as a
# result (matchups called 96% were won 79% of the time). These are the widths
# that hit 68.3% coverage per position.
_VOLATILITY = {"QB": 0.54, "RB": 0.65, "WR": 0.72, "TE": 0.74,
               # K/DST are not in the nflverse player-stats sample the
               # calibration runs on, so these carry the overall ~2x correction
               # rather than a per-position measurement.
               "K": 0.70, "DST": 0.90, "DEF": 0.90}


def _environment_mult(implied_total: float | None, is_fallback: bool) -> float:
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


DEFENSE_POSITIONS = {"DST", "DEF"}

# Defense scoring is dominated by how little the *opponent* is expected to
# score: sacks, turnovers and a shutout bonus all track a bad offensive day.
# Points are the projection itself rather than a multiplier, because a flat
# 7.0 baseline scaled by the defense's own team total — which is what the
# generic path did — has the causality backwards.
def defense_base(opponent_implied_total: float | None) -> float:
    """Expected fantasy points for a team defense, from the opponent's total."""
    if opponent_implied_total is None:
        return 7.0
    if opponent_implied_total <= 16:
        return 11.0
    if opponent_implied_total <= 19:
        return 9.5
    if opponent_implied_total <= 22:
        return 8.0
    if opponent_implied_total <= 25:
        return 6.5
    if opponent_implied_total <= 28:
        return 5.0
    return 3.5


def kicker_base(implied_total: float | None) -> float:
    """Expected fantasy points for a kicker, from his own team's total.

    Rises with scoring but flattens at the top: a team expected to score 30 is
    trading field goals for touchdowns, which pays the kicker one point
    instead of three.
    """
    if implied_total is None:
        return 8.0
    if implied_total >= 28:
        return 9.0
    if implied_total >= 24:
        return 9.5
    if implied_total >= 21:
        return 8.5
    if implied_total >= 18:
        return 7.5
    return 6.5


def _usage_mult(snap_pct: float | None, usage_trend: str | None) -> float:
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


# Statuses that mean the player will not take the field. Sleeper's short codes
# are in here alongside the long forms, because the short ones are what the
# player feed actually sends: `Sus` (suspension), `NA` (not active — almost
# always an unrostered player), `DNR` (did not report), `COV` (COVID list).
# Verified against the live cache: 110 players carried one of those four and
# every one of them was projected at full points and never auto-benched.
UNAVAILABLE_STATUSES = frozenset({
    "out", "ir", "injured reserve", "suspended", "sus", "pup", "nfi",
    "na", "dnr", "cov", "doubtful_out",
})
DOUBTFUL_STATUSES = frozenset({"doubtful"})
QUESTIONABLE_STATUSES = frozenset({"questionable", "q", "dnp", "lp"})


def _injury_mult(status: str | None) -> float:
    """Availability multiplier for a status string.

    An unrecognised *non-empty* status is treated as questionable rather than
    healthy. Sleeper only populates `injury_status` when something is wrong, so
    "a designation exists that we do not know" is evidence against the player,
    not evidence for him — and defaulting to 1.0 is what let `Sus` and `NA`
    project at full points. New upstream codes now degrade safely and noisily
    instead of silently.
    """
    s = (status or "").strip().lower()
    if not s or s in ("active", "healthy", "probable", "fp"):
        return 1.0
    if s in UNAVAILABLE_STATUSES:
        return 0.0
    if s in DOUBTFUL_STATUSES:
        return 0.35
    if s in QUESTIONABLE_STATUSES:
        return 0.9
    logger.warning(
        f"unrecognised injury status {status!r} — treating as questionable (0.9). "
        "Add it to the status tables in projections.py / injury_service.py."
    )
    return 0.9


class ProjectionEngine:
    """Projects fantasy points by combining value, matchup, environment, usage."""

    def __init__(self, db=None):
        self.values = get_values_service(db)
        self.defense = get_defense_analyzer()
        self.vegas = get_vegas_analyzer()

    def _project_one(
        self, player: dict, values_index: dict, rankings: dict, lines: dict,
        opp_index: dict | None = None, week: int | None = None, ppr: float = 1.0,
    ) -> dict:
        name = player.get("name") or player.get("player_name")
        position = (player.get("position") or "").upper()
        team = (player.get("team") or "").upper()
        opponent = (player.get("opponent") or "").upper()
        player_id = player.get("player_id")
        usage = player.get("usage") or {}
        injury = player.get("injury") or {}

        # 1) Baseline. Prefer the opportunity-based projection (backtested to beat
        #    rank-bucket PPG) when we have this player's trailing nflverse volume;
        #    otherwise fall back to the positional-rank baseline.
        market = self.values.lookup(values_index, player_id=player_id, name=name, position=position)
        pos_rank = (market or {}).get("position_rank")
        base = base_ppg(position, pos_rank, ppr)
        base_source = "rank_bucket"
        if opp_index and week and name:
            opp_base = opportunity_tools.opportunity_base_for(
                opp_index, name, position, week, ppr=ppr
            )
            if opp_base is not None:
                base = round(opp_base, 1)
                base_source = "opportunity"

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
        opponent_implied_total = None
        env_is_fallback = True
        if team:
            try:
                game = self.vegas.get_game_lines(team, lines)
                # Compare canonical to canonical. `get_game_lines` normalizes
                # its lookup but returns the canonical spelling, so a caller
                # passing Sleeper's `WAS`/`JAC`/`LA` would fail this test on a
                # HOME game and read the *opponent's* implied total instead.
                canonical = normalize_team(team) or team
                is_home = game.get("home_team") == canonical
                implied_total = game.get("home_implied_total") if is_home else game.get("away_implied_total")
                opponent_implied_total = (
                    game.get("away_implied_total") if is_home else game.get("home_implied_total")
                )
                env_is_fallback = bool(game.get("is_fallback"))
            except Exception as e:
                # Never silent: a payload-shape change here would send every
                # player to the neutral fallback with no trace in the logs.
                logger.warning(f"Vegas lookup failed for {team}: {e}")
        env_mult = _environment_mult(implied_total, env_is_fallback)

        # Defenses and kickers are priced off the game total directly rather
        # than off a positional-rank baseline: neither has a market value to
        # rank against, so both used to return a constant (7.0 / 8.0) for every
        # team in every matchup. A defense keys on the *opponent's* total — the
        # generic path scaled it by its own, which is backwards.
        if position in DEFENSE_POSITIONS:
            usable = None if env_is_fallback else opponent_implied_total
            base = defense_base(usable)
            base_source = "opponent_total"
            matchup_mult = 1.0
            env_mult = 1.0
        elif position == "K":
            usable = None if env_is_fallback else implied_total
            base = kicker_base(usable)
            base_source = "team_total"
            matchup_mult = 1.0
            env_mult = 1.0

        # 4) Usage & 5) injury. The opportunity base already embeds volume/usage
        #    trend, so skip the usage multiplier there to avoid double-counting.
        usage_mult = (
            1.0 if base_source == "opportunity"
            else _usage_mult(usage.get("snap_percentage"), usage.get("usage_trend"))
        )
        inj_mult = _injury_mult(injury.get("status"))

        # 6) Weather (opt-in): only applied when the caller supplies wind/roof
        #    (e.g. from get_weather_forecast). The backtest shows the effect is
        #    small and rare — real in windy games, ~noise otherwise — so it is
        #    deliberately not always-on. Neutral (1.0) when no weather given.
        weather = player.get("weather") or {}
        weather_mult = weather_multiplier(
            position, weather.get("wind_mph") or 0.0, weather.get("precip_in") or 0.0,
            weather.get("temp_f"), bool(weather.get("is_dome")),
        ) if weather else 1.0

        projected = round(base * matchup_mult * env_mult * usage_mult * weather_mult * inj_mult, 1)
        vol = _VOLATILITY.get(position, 0.35)
        if inj_mult == 0.0:
            floor = ceiling = 0.0
        else:
            floor = round(projected * (1 - vol), 1)
            ceiling = round(projected * (1 + vol), 1)

        # Confidence = how many real signals we had
        conf = 50
        if market or base_source == "opportunity":
            conf += 20
        if not env_is_fallback:
            conf += 15
        # Only credit real usage signal — project_player always passes a
        # {snap_percentage: None, usage_trend: None} dict (truthy), which used to
        # inflate confidence to 85/high with no actual usage data.
        has_real_usage = (
            base_source == "opportunity"
            or usage.get("snap_percentage") is not None
            or usage.get("usage_trend") is not None
        )
        if has_real_usage:
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
            "opponent_implied_total": opponent_implied_total,
            "vegas_active": not env_is_fallback,
            "breakdown": {
                "base_ppg": base,
                "base_source": base_source,
                "position_rank": pos_rank,
                "matchup_mult": round(matchup_mult, 3),
                "environment_mult": round(env_mult, 3),
                "usage_mult": round(usage_mult, 3),
                "weather_mult": round(weather_mult, 3),
                "injury_mult": round(inj_mult, 3),
            },
            "value_source": (
                "opportunity" if base_source == "opportunity"
                else "fantasycalc" if market else "baseline"
            ),
        }

    async def project_many(
        self, players: list[dict], scoring: str = "ppr", superflex: bool = False,
        num_teams: int = 12, season: int | None = None, week: int | None = None,
    ) -> dict:
        ppr = scoring_to_ppr(scoring)
        values_index = await self.values.get_values(
            ppr, 2 if superflex else 1, num_teams, False
        )
        try:
            rankings = await self.defense.fetch_defense_rankings()
        except Exception:
            rankings = {}
        try:
            lines = await self.vegas.fetch_current_lines()
        except Exception:
            lines = {}

        # Opportunity baseline: fetch this season's trailing volume once and index
        # by name. Requires season + week (>1); otherwise the rank-bucket baseline
        # is used (backward compatible).
        opp_index: dict = {}
        if season and week and week > 1:
            try:
                logs = await opportunity_tools._fetch_game_logs(season)
                opp_index = opportunity_tools.build_name_index(logs)
            except Exception:
                opp_index = {}

        projections = [
            self._project_one(p, values_index, rankings, lines, opp_index, week, ppr)
            for p in players
        ]
        return {
            "projections": projections,
            "values_source": values_index.get("source"),
            "vegas_active": bool(lines),
            "opportunity_active": bool(opp_index),
            # Stated rather than assumed: the same roster is worth visibly
            # different points in full vs half PPR, and the FLEX order changes.
            "scoring": scoring,
            "ppr": ppr,
        }


_engine: ProjectionEngine | None = None


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
    players: list[dict],
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    season: int | None = None,
    week: int | None = None,
    db=None,
) -> dict:
    """Project weekly fantasy points for multiple players (transparent, no scraping).

    Args:
        players: list of dicts with name, position, team, opponent, and optional
            usage {snap_percentage, usage_trend} and injury {status}.
        scoring/superflex/num_teams: league format for the value baseline.
        season, week: pass both to use the opportunity-based baseline (trailing
            nflverse volume, backtested to beat rank-bucket PPG); week must be >1.
            Omit either to use the positional-rank baseline.

    Returns: {projections:[{projected_points, floor, ceiling, confidence, breakdown, ...}]}
    """
    if not players:
        return create_error_response("No players provided", ErrorType.VALIDATION, {"projections": []})
    engine = get_projection_engine(db)
    result = await engine.project_many(
        players, scoring=scoring, superflex=superflex, num_teams=num_teams,
        season=season, week=week,
    )
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
    snap_percentage: float | None = None,
    usage_trend: str | None = None,
    injury_status: str | None = None,
    scoring: str = "ppr",
    superflex: bool = False,
    season: int | None = None,
    week: int | None = None,
    wind_mph: float | None = None,
    is_dome: bool = False,
    db=None,
) -> dict:
    """Project weekly fantasy points for a single player.

    Pass season + week (week > 1) to use the opportunity-based baseline (trailing
    nflverse volume, backtested to beat rank-bucket PPG); omit either to use the
    positional-rank baseline. Optionally pass wind_mph / is_dome (e.g. from
    get_weather_forecast) to apply the weather factor — small but real in windy
    games; neutral otherwise.

    Returns: {projection: {projected_points, floor, ceiling, confidence, breakdown, ...}}
    """
    player = {
        "name": player_name, "position": position, "team": team, "opponent": opponent,
        "usage": {"snap_percentage": snap_percentage, "usage_trend": usage_trend},
        "injury": {"status": injury_status},
    }
    if wind_mph is not None or is_dome:
        player["weather"] = {"wind_mph": wind_mph, "is_dome": is_dome}
    engine = get_projection_engine(db)
    result = await engine.project_many([player], scoring=scoring, superflex=superflex,
                                       season=season, week=week)
    proj = result["projections"][0] if result["projections"] else None
    return create_success_response({
        "projection": proj,
        "values_source": result.get("values_source"),
        "message": (f"{player_name}: {proj['projected_points']} pts "
                    f"(floor {proj['floor']}, ceiling {proj['ceiling']}, {proj['confidence_level']} conf)"
                    if proj else "No projection"),
    })
