"""
Transparent weekly fantasy point projections — Sleeper-first.

The weekly number is a blend (``sleeper_projections.blend``):

    projected = 0.25 × model_projection + 0.75 × Sleeper's projection

where Sleeper's projected stat line is priced in the league's scoring, and
our own model is built from signals the server already has:

    model_projection = base                    # trailing opportunity regressed
                                               # toward the rank bucket
                     × matchup_multiplier      # defense vs position (matchup_tools)
                     × environment_multiplier  # Vegas implied team total
                     × usage_multiplier        # snap% / usage trend (rank bucket only)
                     × injury_multiplier       # availability

Without a Sleeper projection for the player (an outage, the off-season, a
player it does not list) the model alone is used, labelled
``projection_source: "model_only"`` with a warning. Byes and Out are zero
either way. Every factor is reported in a `breakdown` so the number is
explainable, and a `confidence` reflects how many real signals were
available. No API key needed (Sleeper + FantasyCalc + ESPN); Vegas is
optional (ODDS_API_KEY improves it).

`scoring` sets the points scale, not just which market values are consulted:
both baselines are rebased to the league's per-reception value, so a half-PPR
league gets half-PPR points and the receiver-vs-runner ordering that follows
from them. Given the league's full settings (a ``scoring_settings`` dict, a
``scoring.LeagueScoring`` or ``league_id`` on the tools) every other stat is
priced too: the opportunity base on the player's own stat lines, the rank
buckets on a typical line for the position, K and DEF through the league's
distance and points-allowed tiers. Reported as `scoring_used`.
"""

from __future__ import annotations

import asyncio
import logging

from . import opportunity_tools
from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .matchup_tools import get_defense_analyzer, matchup_ratio
from .player_values import get_values_service
from .ros import PRIOR_GAMES, regressed_rate
from .scoring import ScoringModel, league_scoring, resolve_scoring
from .teams import normalize_team
from .vegas_tools import get_vegas_analyzer
from .weather_tools import weather_multiplier
from .week_context import BYE, bye_check, resolve_season_week, week_schedule

logger = logging.getLogger(__name__)

VBD_POSITIONS = {"QB", "RB", "WR", "TE"}


# Share of a full-PPR baseline that *is* the per-reception bonus, by position —
# i.e. receptions per game divided by PPR points per game (a WR averaging 14.5
# PPR points catches roughly 5 balls). Removing `(1 - ppr) × share` rebases the
# bucket from full PPR to the league's actual reception value. Measured on
# 2023-24 nflverse weekly lines by `evals/backtest/bucket_calibration.py`, which
# also checks the rebased half-PPR buckets against half-PPR actuals.
_RECEPTION_SHARE = {"WR": 0.35, "TE": 0.41, "RB": 0.21, "QB": 0.0, "K": 0.0,
                    "DST": 0.0, "DEF": 0.0}

# Rank buckets: ``(last positional rank in the tier, full-PPR points per game
# played)``; the final ``None`` tier covers every deeper and unranked player.
# Calibrated by `evals/backtest/bucket_calibration.py` (2023-24; a player's
# previous-season rank by points per game stands in for his market rank; mean
# over the games he played, at least four). The WR and TE tiers used to read
# low — WR37-48 priced at 7.5 scored 10.0, TE13-20 at 6.5 scored 8.2 — which is
# what the ROS-only `PRIOR_SCALE` had compensated for. The top tiers (at most
# ten players over two seasons) keep their market-informed values: last
# season's rank is a weaker ranking than the market's and regresses them. QB
# 4-12 came down a point once truth was priced in the projection's own scoring
# (2023-25, INT -1 rather than nflverse's -2): QB4-8 scored 18.0 per game over
# the rest of the season, QB9-12 16.9. QB21+ stays: last season's QB21+ by
# points per game are mostly backups, the market's QB21-32 are starters, so
# the proxy does not carry over there.
_RANK_BUCKETS: dict[str, tuple[tuple[int | None, float], ...]] = {
    "QB": ((3, 22.0), (8, 19.0), (12, 17.0), (20, 16.0), (None, 14.0)),
    "RB": ((3, 19.0), (8, 16.0), (15, 14.0), (24, 12.5), (36, 10.0), (60, 7.5), (None, 5.5)),
    "WR": ((5, 18.0), (12, 17.0), (24, 13.0), (36, 12.0), (48, 10.0), (72, 7.0), (None, 5.5)),
    "TE": ((3, 14.0), (6, 12.0), (12, 10.5), (20, 8.5), (32, 7.0), (None, 4.5)),
}
_FLAT_BASE = {"K": 8.0, "DST": 7.0, "DEF": 7.0}


def rank_bucket(position: str, pos_rank: int | None) -> float:
    """Full-PPR points per game for a positional rank (see `_RANK_BUCKETS`)."""
    p = (position or "").upper()
    tiers = _RANK_BUCKETS.get(p)
    if tiers is None:
        return _FLAT_BASE.get(p, 8.0)
    r = pos_rank if (isinstance(pos_rank, int) and pos_rank > 0) else None
    for last, ppg in tiers:
        if last is None or (r is not None and r <= last):
            return ppg
    return tiers[-1][1]


def base_ppg(position: str, pos_rank: int | None, ppr: float = 1.0,
             scoring: ScoringModel | None = None) -> float:
    """Baseline points/game from a player's positional rank.

    Buckets are full-PPR and then rebased to `ppr` (1.0 full, 0.5 half, 0.0
    standard). This is the fallback baseline — it is used before a player has
    enough games for the opportunity projection — so the rebasing is a
    position-average estimate rather than a per-player reception count.

    With a full `scoring` model the rest of the league's settings are added the
    same way: what they are worth on a typical week at the position, as a share
    of the bucket (a 0.5 TE premium adds ~20% to a TE; a 6-point passing TD
    ~16% to a QB). K and DST are not rebased here — see `defense_base` /
    `kicker_base` and the scales applied to them in `_project_one`.
    """
    p = (position or "").upper()
    full = rank_bucket(p, pos_rank)
    if scoring is not None:
        ppr = scoring.rec
    adjust = scoring.bucket_adjust(p) if scoring is not None else 0.0
    return round(full * (1.0 - (1.0 - ppr) * _RECEPTION_SHARE.get(p, 0.0) + adjust), 2)


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

# Strength of the continuous matchup factor per position: the multiplier is
# `1 + strength × (ratio − 1)`, with the ratio's deviation capped at ±30%.
# From evals/backtest on the opportunity base (2023 and 2024 each out of
# sample): RB moves most with the defense, QB and TE half, WR a little.
_MATCHUP_RATIO_STRENGTH = {"RB": 0.75, "QB": 0.5, "TE": 0.5, "WR": 0.25}
_MATCHUP_RATIO_CAP = 0.30


def matchup_factor(position: str, ratio: float) -> float:
    """Multiplier from a `matchup_tools.matchup_ratio` (1.0 = average defense)."""
    strength = _MATCHUP_RATIO_STRENGTH.get((position or "").upper(), 0.0)
    dev = max(-_MATCHUP_RATIO_CAP, min(_MATCHUP_RATIO_CAP, ratio - 1.0))
    return round(1.0 + strength * dev, 4)


def _ranking_entry(rankings: dict | None, position: str, opponent: str) -> dict | None:
    team = normalize_team(opponent) or (opponent or "").upper()
    for row in (rankings or {}).get(position) or []:
        if row.get("team") == team:
            return row
    return None


# Higher fantasy scoring variance = wider floor/ceiling band. floor/ceiling are
# `mean ± volatility·mean`, i.e. a ±1σ band under the Normal the win-probability
# optimizer assumes, so reality should land inside ~68% of the time.
#
# The hand-picked values once covered only 36% (evals/backtest/calibration.py).
# These are the widths that hit 68.3% coverage around the *blended* weekly
# projection per position (evals/backtest/sleeper_blend.py, 2023-25 weeks 3+,
# QB/RB/WR/TE n~8k; K 2025 n=415; DEF priced from nflverse team stats,
# n=1.3k). Around the model-only number the same widths cover 67-69%.
_VOLATILITY = {"QB": 0.47, "RB": 0.62, "WR": 0.68, "TE": 0.71,
               "K": 0.58, "DST": 0.78, "DEF": 0.78}


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

    Rises with scoring and flattens from 21 up: a team expected to score 30 is
    trading field goals for touchdowns. The tiers are the mean kicker score
    per implied-total band (2025, Sleeper default scoring, n=543): 6.2 / 7.9 /
    8.7 / 8.6 / 8.8 below 18 / 18-21 / 21-24 / 24-28 / 28+. The old 9.5 peak
    at 24-28 was not in the data.
    """
    if implied_total is None:
        return 8.0
    if implied_total >= 28:
        return 8.8
    if implied_total >= 21:
        return 8.7
    if implied_total >= 18:
        return 7.9
    return 6.2


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
    "out", "ir", "injured reserve", "injured_reserve", "suspended", "sus", "pup",
    "nfi", "na", "dnr", "cov", "doubtful_out", "inactive", "reserve",
})
# Prefixes of the long list designations ("Reserve/PUP", "PUP-R",
# "Reserve-Suspended", "Inactive (injury)"): all of them mean no game this week.
_UNAVAILABLE_PREFIXES = ("reserve", "pup", "suspend", "injured reserve", "inactive", "nfi")
DOUBTFUL_STATUSES = frozenset({"doubtful"})
QUESTIONABLE_STATUSES = frozenset({"questionable", "q", "dnp", "lp"})
# A designation that says "we do not know" rather than "something is wrong".
# Priced as mild uncertainty: not a questionable tag, and not healthy either.
UNCERTAIN_STATUSES = frozenset({"unknown"})
UNCERTAIN_MULT = 0.95


def availability(status: str | None) -> str:
    """Classify a status: healthy, out, doubtful, questionable, uncertain or
    unrecognised.

    One vocabulary for both the projection multiplier and the start/sit health
    score, so the two cannot disagree about whether a player plays — they did:
    `Inactive` and `Reserve` projected at 0.9 and scored a perfect health 100.
    """
    s = (status or "").strip().lower()
    if not s or s in ("active", "healthy", "probable", "fp"):
        return "healthy"
    if s in UNAVAILABLE_STATUSES or s.startswith(_UNAVAILABLE_PREFIXES):
        return "out"
    if s in DOUBTFUL_STATUSES:
        return "doubtful"
    if s in QUESTIONABLE_STATUSES:
        return "questionable"
    if s in UNCERTAIN_STATUSES:
        return "uncertain"
    return "unrecognised"


# How a questionable tag plays out depends on the week's practice: a player
# limited or better on the latest report usually suits up, one who has not
# practised usually does not. Applied only to a *reported* practice line on top
# of a questionable designation; with no report the flat 0.9 stands. Heuristic
# weights (not backtested — the calibration set has no practice data), kept
# mild on the upside and firm on the downside.
QUESTIONABLE_BY_PRACTICE = {"FP": 0.97, "REST": 0.97, "LP": 0.9, "DNP": 0.65}


def practice_adjusted_mult(status: str | None, practice_status: str | None) -> float:
    """``_injury_mult`` refined by the latest reported practice.

    Only a questionable designation moves: Out stays 0, a healthy player with
    a DNP (often rest before the designations are set) stays 1.0.
    """
    base = _injury_mult(status)
    practice = (practice_status or "").strip().upper()
    if practice and practice not in QUESTIONABLE_BY_PRACTICE:
        # A caller's spelling ("limited", "Did Not Participate In Practice").
        from .practice_reports import normalize_practice
        practice = normalize_practice(practice) or ""
    if practice and availability(status) == "questionable" and practice in QUESTIONABLE_BY_PRACTICE:
        return QUESTIONABLE_BY_PRACTICE[practice]
    return base


def _injury_mult(status: str | None) -> float:
    """Availability multiplier for a status string.

    An unrecognised *non-empty* status is treated as questionable rather than
    healthy. Sleeper only populates `injury_status` when something is wrong, so
    "a designation exists that we do not know" is evidence against the player,
    not evidence for him — and defaulting to 1.0 is what let `Sus` and `NA`
    project at full points. New upstream codes now degrade safely and noisily
    instead of silently.
    """
    kind = availability(status)
    if kind == "healthy":
        return 1.0
    if kind == "out":
        return 0.0
    if kind == "doubtful":
        return 0.35
    if kind == "questionable":
        return 0.9
    if kind == "uncertain":
        return UNCERTAIN_MULT
    logger.warning(
        f"unrecognised injury status {status!r} — treating as questionable (0.9). "
        "Add it to the status tables in projections.py / injury_service.py."
    )
    return 0.9


def _depth_map(values_index: dict) -> dict[tuple[str, str], list[dict]]:
    """``{(team, position): [entries, best first]}`` from the market values.

    Market rank is the only league-wide depth signal available here — the ESPN
    depth chart is a separate fetch per team, and an unavailable starter is
    almost always the higher-valued player anyway.
    """
    depth: dict[tuple[str, str], list[dict]] = {}
    for entry in (values_index or {}).get("list", []) or []:
        team = normalize_team(entry.get("team")) or (entry.get("team") or "").upper()
        position = (entry.get("position") or "").upper()
        if not team or not position:
            continue
        depth.setdefault((team, position), []).append(entry)
    for entries in depth.values():
        entries.sort(key=lambda e: e.get("position_rank") or 999)
    return depth


def _starters_ahead(
    depth: dict[tuple[str, str], list[dict]], team: str, position: str,
    pos_rank: int | None, status_of,
) -> list[str]:
    """Names of higher-valued teammates at this position who cannot play."""
    if pos_rank is None:
        return []
    out = []
    for entry in depth.get((team, position), []):
        rank = entry.get("position_rank") or 999
        if rank >= pos_rank:
            break
        name = entry.get("name")
        if name and _injury_mult(status_of(name, team)) == 0.0:
            out.append(name)
    return out


# How an out starter's inherited volume is split among the available
# teammates below him at the position, next man up first. Normalized, so the
# group inherits `VACATED_VOLUME_SHARE` of it between them: every lower-ranked
# teammate used to take the full share, and a WR1 absence handed WR2-WR5 four
# targets each — sixteen targets that did not exist.
_HEIR_WEIGHTS = (1.0, 0.5, 0.25)


def _inherited_shares(
    depth: dict[tuple[str, str], list[dict]], team: str, position: str,
    pos_rank: int | None, out_players: list[str], status_of,
    share: float = opportunity_tools.VACATED_VOLUME_SHARE,
) -> dict[str, float]:
    """``{out starter: fraction of his volume this player inherits}``.

    The player (at `pos_rank`) is placed among the available teammates ranked
    below each out starter; his slice is his `_HEIR_WEIGHTS` weight over the
    group's total, so the slices of all heirs add up to `share`. Nothing past
    the last weighted heir, or when he is not in the depth list.
    """
    entries = depth.get((team, position), [])
    shares: dict[str, float] = {}
    for starter in out_players:
        top = next((e for e in entries if e.get("name") == starter), None)
        if top is None:
            continue
        top_rank = top.get("position_rank") or 999
        heirs = [e for e in entries
                 if (e.get("position_rank") or 999) > top_rank and e.get("name")
                 and _injury_mult(status_of(e["name"], team)) != 0.0][:len(_HEIR_WEIGHTS)]
        slot = next((i for i, e in enumerate(heirs) if e.get("position_rank") == pos_rank), None)
        if slot is None:
            continue
        weights = _HEIR_WEIGHTS[:len(heirs)]
        shares[starter] = share * weights[slot] / sum(weights)
    return shares


def projection_confidence(
    *,
    has_market: bool,
    base_source: str,
    usage_games: int = 0,
    volume_cv: float | None = None,
    has_real_usage: bool = False,
    vegas_real: bool = False,
    injury_mult: float = 1.0,
    inherits_volume: bool = False,
) -> int:
    """How much to trust a projection, 0-100.

    It used to count signals present and cap at 100, crediting an opportunity
    base twice (+20 as a "value" signal, +15 as "real usage"), so with Vegas on
    every skill player with two games of history scored 100 and the number
    ranked nothing. Now it measures what actually makes a weekly projection
    reliable: how many weeks of real usage back it, how steady that role is,
    whether the player's availability is in doubt, and whether the inputs are
    fallbacks (rank bucket, constant game environment).
    """
    conf = 45
    if has_market:
        conf += 10
    if base_source == "opportunity":
        # Two games (the minimum) is thin; a full six-game window is not.
        conf += 5 + 3 * min(max(usage_games, 0), 6)
    elif has_real_usage:
        conf += 8
    if vegas_real:
        conf += 10
    if volume_cv is not None:
        if volume_cv > 0.5:
            conf -= 10  # the role swings week to week
        elif volume_cv > 0.3:
            conf -= 5
    if inherits_volume:
        conf -= 8   # priced on an assumed share of a teammate's workload
    if 0.0 < injury_mult < 1.0:
        # Questionable (0.9) or doubtful (0.35): whether he plays is unknown.
        # An Out player projects to a certain zero, which is not uncertain.
        conf -= 10 if injury_mult >= 0.5 else 20
    return max(0, min(100, conf))


def _bye_projection(
    name: str | None, position: str, team: str, bye: dict, injury: dict,
) -> dict:
    """A zero projection for a player whose team has no game.

    Same shape as a priced projection so every consumer can read it, with
    `on_bye` set and the reason stated: a zero with no explanation reads as a
    bust, which is a different thing from a bye.
    """
    return {
        "player": name,
        "position": position,
        "team": team,
        "opponent": "BYE",
        "projected_points": 0.0,
        "floor": 0.0,
        "ceiling": 0.0,
        # Certain: a team without a game scores nothing.
        "confidence": 100,
        "confidence_level": "high",
        "matchup_tier": "bye",
        "implied_total": None,
        "opponent_implied_total": None,
        "vegas_active": False,
        "breakdown": {
            "base_ppg": 0.0,
            "base_source": "bye",
            "position_rank": None,
            "matchup_mult": 1.0,
            "matchup_ratio": None,
            "environment_mult": 1.0,
            "usage_mult": 1.0,
            "weather_mult": 1.0,
            "injury_mult": round(_injury_mult(injury.get("status")), 3),
            "starters_out_ahead": [],
            "vacated_volume": {},
        },
        "value_source": "bye",
        "injury_status": injury.get("status"),
        "injury_source": injury.get("source"),
        "on_bye": True,
        "bye_status": BYE,
        "bye_source": bye.get("source"),
        "bye_reason": bye.get("reason"),
    }


class ProjectionEngine:
    """Projects fantasy points by combining value, matchup, environment, usage."""

    def __init__(self, db=None):
        # Kept so teammate availability can be looked up: depth pricing needs
        # the status of players who are not on the roster being projected.
        self.db = db
        self.values = get_values_service(db)
        self.defense = get_defense_analyzer()
        self.vegas = get_vegas_analyzer()

    def _project_one(
        self, player: dict, values_index: dict, rankings: dict, lines: dict,
        opp_index: dict | None = None, week: int | None = None, ppr: float = 1.0,
        depth: dict | None = None, status_of=None, schedule: dict | None = None,
        scoring_model: ScoringModel | None = None,
    ) -> dict:
        name = player.get("name") or player.get("player_name")
        position = (player.get("position") or "").upper()
        team = (player.get("team") or "").upper()
        # The depth map and the injury index are keyed by the canonical code:
        # Sleeper's WAS/JAC/LA missed both and never priced a vacated role.
        depth_team = normalize_team(team) or team
        player_id = player.get("player_id")
        usage = player.get("usage") or {}
        injury = player.get("injury") or {}
        # The league's full scoring; without one, Sleeper's defaults at `ppr`.
        model = scoring_model if scoring_model is not None else ScoringModel.preset(ppr)
        ppr = model.rec

        # 0) Does he have a game at all? A bye used to fall through every
        #    factor to neutral and project the full baseline.
        bye = bye_check(team, player.get("opponent"), schedule, week)
        if bye["status"] == BYE:
            return _bye_projection(name, position, team, bye, injury)
        opponent = bye["opponent"] or ""

        # 1) Baseline. Prefer the opportunity-based projection (backtested to beat
        #    rank-bucket PPG) when we have this player's trailing nflverse volume;
        #    otherwise fall back to the positional-rank baseline.
        market = self.values.lookup(values_index, player_id=player_id, name=name, position=position)
        pos_rank = (market or {}).get("position_rank")
        base = base_ppg(position, pos_rank, ppr, model)
        base_source = "rank_bucket"
        # A higher-valued teammate at the same position who cannot play frees up
        # volume. Only counts when he has *recent* volume to free: a starter who
        # has been out all season vacates nothing, because the backup's own
        # trailing numbers already describe him as the starter. That distinction
        # is what keeps this from inventing points out of an absence.
        starters_out: list[str] = []
        vacated: dict[str, float] = {}
        inherited_from: dict[str, str | None] = {}
        own_base = None
        if depth and status_of and team and opp_index and week:
            starters_out = _starters_ahead(depth, depth_team, position, pos_rank, status_of)
            if starters_out:
                shares = _inherited_shares(depth, depth_team, position, pos_rank,
                                           starters_out, status_of)
                vacated = opportunity_tools.vacated_volume(
                    opp_index, list(shares), week, share=shares)
                if vacated:
                    inherited_from = {n: status_of(n, depth_team) for n in shares}
        if opp_index and week and name:
            opp_base = opportunity_tools.opportunity_base_for(
                opp_index, name, position, week, ppr=ppr,
                extra_volume=vacated or None, scoring=model,
            )
            if opp_base is not None:
                base = round(opp_base, 1)
                base_source = "opportunity"
                if vacated:
                    # His own volume without the absent starter's, so ROS can
                    # price the inherited part only while the starter is out.
                    own = opportunity_tools.opportunity_base_for(
                        opp_index, name, position, week, ppr=ppr, scoring=model)
                    own_base = round(own, 1) if own is not None else None

        # 2) Matchup vs opponent defense
        matchup_tier = "unknown"
        if position in VBD_POSITIONS and opponent:
            try:
                m = self.defense.get_matchup_difficulty(position, opponent, rankings)
                matchup_tier = m.get("matchup_tier", "unknown")
            except Exception:
                matchup_tier = "unknown"
        matchup_mult = matchup_multiplier(position, matchup_tier)
        # The continuous factor replaces the tier where the rankings carry the
        # raw averages: it is priced in this league's reception value and
        # counts from week 2, where the tier stays neutral until week 5.
        ratio = None
        if position in VBD_POSITIONS and opponent:
            ratio = matchup_ratio(_ranking_entry(rankings, position, opponent), ppr)
            if ratio is not None:
                matchup_mult = matchup_factor(position, ratio)

        # 3) Game environment (Vegas implied team total)
        implied_total = None
        opponent_implied_total = None
        env_is_fallback = True
        if team:
            try:
                game = self.vegas.get_game_lines(team, lines, opponent=opponent)
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
            # The tiers are Sleeper's defaults; the league's own points-allowed
            # tiers, TD and takeaway values rescale them.
            base = round(defense_base(usable) * model.defense_scale(usable), 2)
            base_source = "opponent_total"
            matchup_mult = 1.0
            env_mult = 1.0
        elif position == "K":
            usable = None if env_is_fallback else implied_total
            # Distance tiers, misses and PATs at the league's values. A league
            # that does not score kickers projects them at zero.
            base = round(kicker_base(usable) * model.kicker_scale(), 2)
            base_source = "team_total"
            matchup_mult = 1.0
            env_mult = 1.0

        # 4) Usage & 5) injury. The opportunity base already embeds volume/usage
        #    trend, so skip the usage multiplier there to avoid double-counting.
        usage_mult = (
            1.0 if base_source == "opportunity"
            else _usage_mult(usage.get("snap_percentage"), usage.get("usage_trend"))
        )
        inj_mult = practice_adjusted_mult(injury.get("status"), injury.get("practice_status"))

        # 6) Weather (opt-in): only applied when the caller supplies wind/roof
        #    (e.g. from get_weather_forecast). The backtest shows the effect is
        #    small and rare — real in windy games, ~noise otherwise — so it is
        #    deliberately not always-on. Neutral (1.0) when no weather given.
        weather = player.get("weather") or {}
        weather_mult = weather_multiplier(
            position, weather.get("wind_mph") or 0.0, weather.get("precip_in") or 0.0,
            weather.get("temp_f"), bool(weather.get("is_dome")),
        ) if weather else 1.0

        sample = None
        if base_source == "opportunity" and opp_index and week and name:
            sample = opportunity_tools.usage_sample(opp_index, name, week)

        # Two or three games of opportunity are a small sample: the rate is
        # regressed toward the rank bucket exactly as ROS prices later weeks
        # (`ros.regressed_rate`, PRIOR_GAMES games-equivalent of prior), so
        # the weekly and ROS numbers share one per-game rate. Volume inherited
        # from an absent starter is added back on top, unregressed. Weekly
        # backtest (2023-25): model MAE weeks 3-4 5.72 -> 5.54, weeks 5+
        # 5.64 -> 5.55.
        rate = base
        prior_ppg = prior_weight = None
        if base_source == "opportunity":
            games = int((sample or {}).get("games") or 0)
            prior_ppg = base_ppg(position, pos_rank, ppr, model)
            own = own_base if own_base is not None else base
            prior_weight = round(PRIOR_GAMES / (games + PRIOR_GAMES), 2)
            rate = round(regressed_rate(own, prior_ppg, games) + (base - own), 2)

        projected = round(rate * matchup_mult * env_mult * usage_mult * weather_mult * inj_mult, 1)
        vol = _VOLATILITY.get(position, 0.35)
        if inj_mult == 0.0:
            floor = ceiling = 0.0
        else:
            floor = round(projected * (1 - vol), 1)
            ceiling = round(projected * (1 + vol), 1)
        has_real_usage = (
            usage.get("snap_percentage") is not None
            or usage.get("usage_trend") is not None
        )
        conf = projection_confidence(
            has_market=bool(market),
            base_source=base_source,
            usage_games=(sample or {}).get("games", 0),
            volume_cv=(sample or {}).get("volume_cv"),
            has_real_usage=has_real_usage,
            vegas_real=not env_is_fallback,
            injury_mult=inj_mult,
            inherits_volume=bool(vacated),
        )
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
                # Opportunity base only: the rank-bucket prior it is regressed
                # toward, the prior's weight, and the rate actually priced.
                **({"prior_ppg": prior_ppg, "prior_weight": prior_weight,
                    "regressed_base_ppg": rate} if prior_ppg is not None else {}),
                "matchup_mult": round(matchup_mult, 3),
                # Opponent's points allowed to the position vs an average
                # defense, shrunk (None: the tier alone priced the matchup).
                "matchup_ratio": round(ratio, 3) if ratio is not None else None,
                "environment_mult": round(env_mult, 3),
                "usage_mult": round(usage_mult, 3),
                "weather_mult": round(weather_mult, 3),
                "injury_mult": round(inj_mult, 3),
                # What the confidence rests on: weeks of real usage behind the
                # opportunity base, and how much that weekly volume swings.
                "usage_games": (sample or {}).get("games"),
                "volume_cv": (sample or {}).get("volume_cv"),
                # Which unavailable teammates were priced in, and the volume
                # inherited from them. Empty when nobody ahead is out, or when
                # they have no recent volume to vacate.
                "starters_out_ahead": starters_out,
                "vacated_volume": vacated,
                # Only when volume was inherited: the opportunity base without
                # it, and the status of whoever it came from (ROS uses both).
                **({"own_base_ppg": own_base, "inherited_from": inherited_from}
                   if own_base is not None else {}),
            },
            "value_source": (
                "opportunity" if base_source == "opportunity"
                else "fantasycalc" if market else "baseline"
            ),
            "injury_status": injury.get("status"),
            "injury_source": injury.get("source"),
            # The reported practice line priced into injury_mult, if any.
            "practice_status": injury.get("practice_status"),
            "practice_pattern": injury.get("practice_pattern"),
            "practice_source": injury.get("practice_source"),
            "on_bye": False,
            # "unknown" when there was neither an opponent nor a cached
            # schedule to check against: projected as playing, but unverified.
            "bye_status": bye["status"],
            "bye_reason": bye["reason"],
        }

    def _status_lookup(self):
        """``(name, team) -> injury status`` from the ESPN/CBS report table.

        Returns a function so a missing database degrades to "everyone
        available" rather than to an exception. Uses the report table rather
        than Sleeper's player list because a teammate's availability has to be
        known for players who are not on the roster being projected.
        """
        index: dict[tuple[str, str], str] = {}
        # `getattr`, not `self.db`: the engine is legitimately built via
        # `__new__` with only the dependencies a caller needs stubbed, and a
        # missing handle must degrade to "everyone available" rather than raise
        # from inside a projection.
        db = getattr(self, "db", None)
        if db is not None:
            try:
                from .opportunity_tools import norm_name
                for row in db.get_all_current_injuries():
                    name = norm_name(row.get("player_name"))
                    team = normalize_team(row.get("team_id")) or ""
                    status = row.get("injury_status")
                    if name and status:
                        index[(name, team)] = status
            except Exception as e:
                logger.debug(f"injury lookup unavailable for depth pricing: {e}")

        def _get(name: str, team: str) -> str | None:
            from .opportunity_tools import norm_name
            return index.get((norm_name(name), team))

        return _get

    async def project_many(
        self, players: list[dict], scoring: str = "ppr", superflex: bool = False,
        num_teams: int = 12, season: int | None = None, week: int | None = None,
        db=None,
    ) -> dict:
        # `scoring` may be a label, a number, a Sleeper scoring_settings dict or
        # a LeagueScoring carrying the league's full settings.
        model = resolve_scoring(scoring)
        ppr = model.rec
        # The cached schedule decides byes. None when the week is not cached,
        # in which case nobody is assumed to be on bye.
        schedule = week_schedule(db if db is not None else getattr(self, "db", None),
                                 season, week)
        # Values, defense rankings, Vegas lines and the opportunity game logs
        # are independent (mostly network) reads: fetched together. Opportunity
        # baseline needs season + week (>1); otherwise the rank-bucket baseline
        # is used (backward compatible).
        async def _read(fetch, *args):
            # Resolved inside the coroutine so a failure of any kind (even a
            # non-awaitable stub) lands in its own slot, as it did sequentially.
            return await fetch(*args)

        async def _no_logs():
            return {}

        use_logs = bool(season and week and week > 1)
        values_index, rankings, lines, logs = await asyncio.gather(
            _read(lambda: self.values.get_values(ppr, 2 if superflex else 1, num_teams, False)),
            _read(lambda: self.defense.fetch_defense_rankings()),
            _read(lambda: self.vegas.fetch_current_lines()),
            _read(opportunity_tools._fetch_game_logs, season) if use_logs else _no_logs(),
            return_exceptions=True,
        )
        if isinstance(values_index, BaseException):
            raise values_index
        rankings = {} if isinstance(rankings, BaseException) else rankings
        lines = {} if isinstance(lines, BaseException) else lines
        opp_index: dict = {}
        if use_logs and not isinstance(logs, BaseException):
            try:
                opp_index = opportunity_tools.build_name_index(logs)
            except Exception:
                opp_index = {}

        # Depth + availability, so a backup whose starter is out inherits some
        # of the vacated volume instead of being priced as a backup.
        depth = _depth_map(values_index)
        status_of = self._status_lookup()

        projections = [
            self._project_one(p, values_index, rankings, lines, opp_index, week, ppr,
                              depth, status_of, schedule, scoring_model=model)
            for p in players
        ]
        await _apply_unit_fallback(projections, season, model)
        blend_summary = await _apply_sleeper_blend(projections, players, season, week, model)
        return {
            "projections": projections,
            "projection_sources": blend_summary["sources"],
            "sleeper_projections_active": blend_summary["active"],
            **({"warnings": blend_summary["warnings"]} if blend_summary["warnings"] else {}),
            "values_source": values_index.get("source"),
            "vegas_active": bool(lines),
            "opportunity_active": bool(opp_index),
            "schedule_known": schedule is not None,
            "on_bye": [p["player"] for p in projections if p.get("on_bye")],
            # Stated rather than assumed: the same roster is worth visibly
            # different points in full vs half PPR, and the FLEX order changes.
            "scoring": scoring if isinstance(scoring, str) else model.label,
            "ppr": ppr,
            "scoring_used": model.summary(),
        }


async def _unit_matchup(position: str, team: str, opponent: str, season: int, model):
    """The K/DEF offense read start/sit uses (a seam for tests)."""
    from .streaming_tools import unit_matchup
    return await unit_matchup(position, team, opponent, season, model)


_UNIT_FIELDS = ("offense_rank", "offense_side", "points_per_game", "games",
                "matchup_tier", "tier_withheld", "source_season", "is_fallback")


async def _apply_unit_fallback(projections: list[dict], season: int | None, model) -> None:
    """Price K/DEF off the season's scoring when there are no Vegas totals.

    Without a game total `defense_base` / `kicker_base` return one constant for
    all 32 teams, so every kicker and defense projected the same. Start/sit
    already fell back to the offense read (the opponent's scoring for a
    defense, his own team's for a kicker); this is the same fallback in the
    engine, so project_player(s), waivers and ROS see it too. In place; a
    failed lookup leaves the constant.
    """
    if not season:
        return
    for proj in projections:
        position = (proj.get("position") or "").upper()
        if position not in DEFENSE_POSITIONS and position != "K":
            continue
        if proj.get("vegas_active") or proj.get("on_bye") or not proj.get("opponent"):
            continue
        try:
            unit = await _unit_matchup(position, proj.get("team") or "", proj["opponent"],
                                       season, model)
        except Exception as e:
            logger.debug(f"K/DEF offense read failed for {proj.get('player')}: {e}")
            continue
        if not unit or unit.get("projected_points") is None:
            continue
        bd = proj.setdefault("breakdown", {})
        mult = float(bd.get("injury_mult", 1.0)) * float(bd.get("weather_mult", 1.0))
        projected = round(float(unit["projected_points"]) * mult, 1)
        vol = _VOLATILITY.get(position, 0.35)
        proj["projected_points"] = projected
        proj["floor"] = 0.0 if mult == 0 else round(projected * (1 - vol), 1)
        proj["ceiling"] = 0.0 if mult == 0 else round(projected * (1 + vol), 1)
        proj["matchup_tier"] = unit.get("matchup_tier", proj.get("matchup_tier"))
        proj["unit_matchup"] = {k: unit.get(k) for k in _UNIT_FIELDS}
        bd["base_ppg"] = float(unit["projected_points"])
        bd["base_source"] = "offense_rank"


async def _sleeper_index(season: int, week: int) -> dict:
    """Sleeper's week (a seam for tests)."""
    from .sleeper_projections import fetch_week_projections
    return await fetch_week_projections(season, week)


async def _apply_sleeper_blend(projections: list[dict], inputs: list[dict],
                               season: int | None, week: int | None, model) -> dict:
    """Make each projection Sleeper-first, in place (see module doc).

    ``projected_points`` becomes the blend of ours (kept as
    ``model_projection``) and Sleeper's (``sleeper_projection``), floor and
    ceiling are re-centred on it, and ``projection_source`` / ``blend_weights``
    say which it was. Every tool that projects a week goes through
    ``project_many``, so start/sit, the lineup, briefing, waivers, trades and
    ROS's current week all read this one number. Never raises.
    """
    from . import sleeper_projections as sp

    index: dict = {}
    if season and week:
        try:
            index = await _sleeper_index(season, week)
        except Exception as e:  # the model alone must still answer
            logger.warning(f"Sleeper projections unavailable for {season} wk{week}: {e}")
            index = {}
    active = bool((index or {}).get("by_id"))
    sources = {"sleeper_blend": 0, "model_only": 0, "bye": 0}
    missing: list[str] = []
    for proj, given in zip(projections, inputs, strict=False):
        ours = float(proj.get("projected_points") or 0.0)
        proj["model_projection"] = ours
        if proj.get("on_bye"):
            proj.update({"sleeper_projection": 0.0, "projection_source": "bye",
                         "blend_weights": dict(sp.MODEL_ONLY_WEIGHTS)})
            sources["bye"] += 1
            continue
        theirs, status = (None, "unavailable")
        if active:
            theirs, status = sp.points_for(
                index, model, player_id=(given or {}).get("player_id"),
                name=proj.get("player"), team=proj.get("team"), position=proj.get("position"))
        bd = proj.setdefault("breakdown", {})
        bd["sleeper_status"] = status
        if theirs is None:
            proj.update({"sleeper_projection": None, "projection_source": "model_only",
                         "blend_weights": dict(sp.MODEL_ONLY_WEIGHTS)})
            sources["model_only"] += 1
            missing.append(proj.get("player") or "?")
            continue
        inj = float(bd.get("injury_mult", 1.0))
        blended = sp.blend(ours, theirs, availability(proj.get("injury_status")), inj)
        vol = _VOLATILITY.get((proj.get("position") or "").upper(), 0.35)
        proj.update({
            "projected_points": blended,
            "floor": 0.0 if blended == 0 else round(blended * (1 - vol), 1),
            "ceiling": 0.0 if blended == 0 else round(blended * (1 + vol), 1),
            "sleeper_projection": theirs,
            "projection_source": "sleeper_blend",
            "blend_weights": dict(sp.BLEND_WEIGHTS),
        })
        sources["sleeper_blend"] += 1
    warnings = []
    if missing:
        why = ("Sleeper projections unavailable" if not active
               else "no Sleeper projection for " + ", ".join(missing[:10])
               + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""))
        warnings.append(f"{why} — model only (opportunity regressed toward the rank "
                        "prior), less accurate than the Sleeper-first blend.")
        for proj in projections:
            if proj.get("projection_source") == "model_only":
                proj["warning"] = ("No Sleeper projection — our model alone "
                                   "(projection_source: model_only).")
    return {"active": active, "sources": {k: v for k, v in sources.items() if v},
            "warnings": warnings}


_engine: ProjectionEngine | None = None


def get_projection_engine(db=None) -> ProjectionEngine:
    global _engine
    if _engine is None:
        _engine = ProjectionEngine(db=db)
    elif db is not None and getattr(_engine, "db", None) is None:
        # The singleton is often created by a caller that had no database, and
        # depth pricing needs one. Adopt the first real handle offered rather
        # than staying blind for the process lifetime.
        _engine.db = db
    return _engine


# ==========================================================================
# MCP Tool Functions
# ==========================================================================

def _with_injuries(players: list[dict], db, season: int | None = None,
                   week: int | None = None) -> list[dict]:
    """Fill in each player's injury from the database when none was given.

    Start/sit already did this (`injury_match.lookup_injury`); the projection
    tools did not, so the same Out player projected full points here and zero
    there. An explicit status from the caller always wins.
    """
    if db is None:
        return players
    from .injury_match import lookup_injury
    from .practice_reports import lookup_practice
    out = []
    for p in players:
        name = p.get("name") or p.get("player_name")
        injury = p.get("injury") or {}
        if not injury.get("status"):
            found = lookup_injury(db, name, p.get("team"))
            if found:
                injury = {**injury, "status": found["status"], "source": found["source"]}
        # The week's real practice report, only for a designated player: that
        # is the only case it prices (see `practice_adjusted_mult`).
        if injury.get("status") and not injury.get("practice_status"):
            practice = lookup_practice(db, name, p.get("team"), season=season, week=week)
            if practice:
                injury = {**injury, "practice_status": practice["latest"],
                          "practice_pattern": practice["pattern"],
                          "practice_source": practice["source"]}
        if injury != (p.get("injury") or {}):
            p = {**p, "injury": injury}
        out.append(p)
    return out


async def _scoring_for(scoring, league_id: str | None):
    """The league's full scoring when `league_id` is given and loads, unless the
    caller asked for a different reception value; otherwise `scoring`."""
    if not league_id:
        return scoring
    try:
        from . import sleeper_tools
        league = ((await sleeper_tools.get_league(league_id)) or {}).get("league") or {}
    except Exception as e:  # a league lookup must not sink the projection
        logger.debug(f"league lookup for scoring failed: {e}")
        return scoring
    if not league:
        return scoring
    carrier = league_scoring(league)
    if scoring not in (None, "", "ppr") and resolve_scoring(scoring).rec != carrier.model.rec:
        return scoring  # an explicit, different format wins
    return carrier


def _player_id_for(db, player: dict) -> str | None:
    """The Sleeper id for a caller's player dict: given, or by name + team."""
    pid = player.get("player_id") or player.get("id")
    if pid:
        return str(pid)
    name = player.get("name") or player.get("player_name")
    team = normalize_team(player.get("team"))
    if not name or not team or not hasattr(db, "search_athletes_by_name"):
        return None
    wanted = opportunity_tools.norm_name(name)
    for row in db.search_athletes_by_name(name, limit=5) or []:
        if (isinstance(row, dict) and opportunity_tools.norm_name(row.get("full_name")) == wanted
                and normalize_team(row.get("team_id")) == team):
            return str(row.get("id"))
    return None


def _log_for_retro(db, players: list[dict], result: dict, season, week, scoring) -> None:
    """Keep these pre-kickoff projections for get_weekly_retro (never raises).

    ``project_many`` returns one projection per input, in input order, which
    is how each row gets its player's id back.
    """
    if db is None:
        return
    try:
        from .projection_store import log_projections
        rows = [
            {**proj, "player_id": _player_id_for(db, player)}
            for player, proj in zip(players, result.get("projections") or [], strict=False)
        ]
        log_projections(db, season, week, scoring, rows, source="project_players")
    except Exception as e:
        logger.debug(f"projection log skipped: {e}")


@handle_http_errors(default_data={"projections": []}, operation_name="projecting players")
async def project_players(
    players: list[dict],
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    season: int | None = None,
    week: int | None = None,
    db=None,
    league_id: str | None = None,
) -> dict:
    """Project weekly fantasy points for multiple players (transparent, no scraping).

    Args:
        players: list of dicts with name, position, team, opponent, and optional
            usage {snap_percentage, usage_trend} and injury {status}. An
            opponent of "BYE" projects zero; a blank one is filled from the
            cached schedule. A player without an injury status is looked up in
            the injury tables, as start/sit does.
        scoring/superflex/num_teams: league format for the value baseline.
        league_id: Sleeper league id. When given, its full scoring_settings
            price every stat (pass TD, INT, fumbles, TE premium, bonuses, K
            distance and DEF points-allowed tiers) instead of the preset.
        season, week: the week being projected. Inferred from the NFL state
            when omitted (reported as `week_inferred`). With week > 1 the
            opportunity-based baseline is used (trailing nflverse volume,
            backtested to beat rank-bucket PPG), and the week's cached schedule
            decides byes.

    Returns: {projections:[{projected_points, floor, ceiling, confidence, on_bye,
              bye_status, breakdown, ...}], on_bye:[names], schedule_known, ...}
    """
    if not players:
        return create_error_response("No players provided", ErrorType.VALIDATION, {"projections": []})
    season, week, week_inferred = await resolve_season_week(season, week)
    engine = get_projection_engine(db)
    scoring = await _scoring_for(scoring, league_id)
    result = await engine.project_many(
        _with_injuries(players, db, season, week), scoring=scoring, superflex=superflex,
        num_teams=num_teams, season=season, week=week, db=db,
    )
    # Filed under the scoring that priced them: the league's full settings
    # when `league_id` resolved, the preset otherwise.
    _log_for_retro(db, players, result, season, week, scoring)
    # Sleeper's projection as a labelled second opinion (see sleeper_projections).
    from .sleeper_projections import attach
    await attach(result, players, season, week, scoring)
    return create_success_response({
        **result,
        "season": season,
        "week": week,
        "week_inferred": week_inferred,
        "total": len(result["projections"]),
        "message": f"Projected {len(result['projections'])} players ({result.get('values_source')})",
    })


@handle_http_errors(default_data={"projection": None}, operation_name="projecting player")
async def project_player(
    player_name: str,
    position: str,
    team: str,
    opponent: str = "",
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
    league_id: str | None = None,
) -> dict:
    """Project weekly fantasy points for a single player.

    season/week are inferred from the NFL state when omitted; with week > 1
    the opportunity-based baseline is used (trailing nflverse volume,
    backtested to beat rank-bucket PPG). An opponent of "BYE" — or a team the
    cached schedule has no game for — projects zero with `on_bye` set; a blank
    opponent is filled from the schedule. Without `injury_status` the player's
    status is looked up in the injury tables. Optionally pass wind_mph /
    is_dome (e.g. from get_weather_forecast) to apply the weather factor —
    small but real in windy games; neutral otherwise. With `league_id` the
    league's full scoring_settings are used (reported as `scoring_used`).

    Returns: {projection: {projected_points, floor, ceiling, confidence, on_bye, breakdown, ...}}
    """
    player = {
        "name": player_name, "position": position, "team": team, "opponent": opponent,
        "usage": {"snap_percentage": snap_percentage, "usage_trend": usage_trend},
        "injury": {"status": injury_status, "source": "caller" if injury_status else None},
    }
    if wind_mph is not None or is_dome:
        player["weather"] = {"wind_mph": wind_mph, "is_dome": is_dome}
    season, week, week_inferred = await resolve_season_week(season, week)
    engine = get_projection_engine(db)
    scoring = await _scoring_for(scoring, league_id)
    result = await engine.project_many(_with_injuries([player], db, season, week), scoring=scoring,
                                       superflex=superflex, season=season, week=week, db=db)
    from .sleeper_projections import attach
    await attach(result, [player], season, week, scoring)
    proj = result["projections"][0] if result["projections"] else None
    if proj and proj.get("on_bye"):
        message = f"{player_name}: 0 pts — {proj.get('bye_reason')}"
    elif proj:
        message = (f"{player_name}: {proj['projected_points']} pts "
                   f"(floor {proj['floor']}, ceiling {proj['ceiling']}, {proj['confidence_level']} conf)")
    else:
        message = "No projection"
    return create_success_response({
        "projection": proj,
        "values_source": result.get("values_source"),
        "scoring_used": result.get("scoring_used"),
        "sleeper_second_opinion": result.get("sleeper_second_opinion"),
        "season": season,
        "week": week,
        "week_inferred": week_inferred,
        "message": message,
    })
