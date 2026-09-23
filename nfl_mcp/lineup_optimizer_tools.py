"""
Lineup optimizer tools for fantasy football start/sit decisions.

This module provides intelligent start/sit recommendations by combining:
- Defense vs position rankings (matchup difficulty)
- Player usage trends (targets, snap count, routes)
- Injury status and practice participation
- CBS expert projections (when available)
- Historical performance patterns

The confidence score system uses multiple weighted factors to provide
actionable recommendations for fantasy lineup decisions.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .game_clock import game_lock, week_games

# Slot eligibility lives in `lineup_slots`, shared with every other lineup
# builder; `SLOT_ELIGIBILITY` and `slot_accepts` are re-exported for callers.
from .lineup_slots import (
    NON_STARTING_SLOTS,
    is_known_slot,
    normalize_slot,
    optimal_lineup,
    slot_accepts,
)
from .lineup_slots import SLOT_ELIGIBILITY as SLOT_ELIGIBILITY
from .player_values import scoring_to_ppr
from .projections import _RECEPTION_SHARE, _VOLATILITY, availability
from .scoring import league_scoring, resolve_scoring, scoring_used
from .teams import normalize_team
from .week_context import BYE, bye_check, resolve_season_week, week_schedule

logger = logging.getLogger(__name__)

# Full-PPR points that mark a merely-adequate and a genuinely good week, per
# position. Rebased to the league's scoring by `_good_game_thresholds`.
_GOOD_GAME_PPR = {
    "QB": (18, 25),
    "RB": (12, 18),
    "WR": (10, 16),
    "TE": (8, 14),
    "K": (7, 10),
    "DST": (6, 10),
    "DEF": (6, 10),
}
_GOOD_GAME_DEFAULT = (10, 16)



def _now() -> datetime:
    """The clock kickoff locks are read against; a seam for tests."""
    return datetime.now(UTC)


# A suggested swap has to beat the projection's own error to be worth making.
# The backtest puts weekly MAE at ~5.8 points; below a couple of points a
# "better" player is indistinguishable from the one already in the slot.
MEANINGFUL_SWAP_GAIN = 2.0


def _good_game_thresholds(position: str, ppr: float = 1.0,
                          unit_scale: float = 1.0) -> tuple[float, float]:
    """(adequate, good) point marks for a position in a league's scoring.

    `unit_scale` rebases the K/DEF marks the way the projection rebases K/DEF
    points (see `unit_threshold_scale`); a reception value does not touch them.
    """
    low, high = _GOOD_GAME_PPR.get((position or "").upper(), _GOOD_GAME_DEFAULT)
    scale = 1.0 - (1.0 - ppr) * _RECEPTION_SHARE.get((position or "").upper(), 0.0)
    return low * scale * unit_scale, high * scale * unit_scale


UNIT_POSITIONS = frozenset({"K", "DEF", "DST"})


def unit_threshold_scale(position: str, scoring) -> float:
    """This league's typical K/DEF week over Sleeper's default one (1.0 for
    everyone else): a league that pays 3 points for holding a team to 14-20
    has a higher bar for a good defensive week than one that pays 1."""
    pos = (position or "").upper()
    if pos not in UNIT_POSITIONS:
        return 1.0
    model = resolve_scoring(scoring)
    return model.kicker_scale() if pos == "K" else model.defense_scale(None)


class StartSitDecision(Enum):
    """Enum for start/sit recommendation types."""
    MUST_START = "must_start"
    START = "start"
    FLEX = "flex"
    SIT = "sit"
    MUST_SIT = "must_sit"


_BETTER_THAN_SIT = frozenset({
    StartSitDecision.MUST_START.value, StartSitDecision.START.value,
    StartSitDecision.FLEX.value,
})


class ConfidenceLevel(Enum):
    """Enum for confidence levels."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class PlayerAnalysis:
    """Data class for player analysis results."""
    player_name: str
    player_id: str
    position: str
    team: str
    opponent: str

    # Matchup factors
    matchup_rank: int = 16  # Default to average
    matchup_tier: str = "neutral"

    # Usage factors
    target_share: float = 0.0
    snap_percentage: float = 0.0
    red_zone_opportunities: int = 0
    usage_trend: str = "stable"

    # Health factors
    injury_status: str = "healthy"
    # None when nobody reported one. It used to default to "full", which told
    # the user an Out player had practised fully.
    practice_status: str | None = None
    # The week's reported practice line ("DNP-LP-FP") and its direction, and
    # where it came from: "caller", "nfl.com", "espn_news", or None when no
    # report exists. Never derived from the injury designation.
    practice_pattern: str | None = None
    practice_trend: str | None = None
    practice_source: str | None = None
    # Where the status came from: "caller" when passed in, otherwise the
    # injury tables ("report", "sleeper" or "both"), None when nothing is known.
    injury_source: str | None = None

    # Schedule: "bye", "playing" or "unknown" (no opponent and no cached
    # schedule to check against — treated as playing, but unverified).
    on_bye: bool = False
    bye_status: str = "unknown"

    # Projection
    projected_points: float = 0.0
    floor: float = 0.0
    ceiling: float = 0.0
    # Which baseline produced it. `rank_bucket` means a static per-position
    # placeholder, not a read on this player — worth surfacing rather than
    # letting a generic number pass for a projection.
    base_source: str | None = None
    # Vegas implied totals for his game (None without live lines). For a
    # defense the opponent's is the one that matters, for a kicker his own.
    implied_total: float | None = None
    opponent_implied_total: float | None = None
    # K/DEF only: the offense rank the matchup is read from (see
    # `streaming_tools.unit_matchup`) and the multiplier on the good-week marks.
    unit_matchup: dict | None = None
    threshold_scale: float = 1.0

    # Sleeper's projection priced in the league's scoring — a second opinion,
    # never the number decisions are made on (see `sleeper_projections`).
    sleeper_projection: float | None = None
    consensus: float | None = None
    disagreement: bool = False
    projection_gap: float | None = None

    # This week's kickoff (UTC and Europe/Vienna) and whether it has passed:
    # a locked player can no longer be moved into or out of a lineup.
    kickoff: str | None = None
    kickoff_local: str | None = None
    kickoff_weekday: str | None = None
    locked: bool = False
    game_status: str = "unknown"

    # Analysis results
    decision: str = "start"
    confidence: float = 50.0
    confidence_level: str = "medium"
    reasoning: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "player_name": self.player_name,
            "player_id": self.player_id,
            "position": self.position,
            "team": self.team,
            "opponent": self.opponent,
            "matchup_rank": self.matchup_rank,
            "matchup_tier": self.matchup_tier,
            "target_share": self.target_share,
            "snap_percentage": self.snap_percentage,
            "red_zone_opportunities": self.red_zone_opportunities,
            "usage_trend": self.usage_trend,
            "injury_status": self.injury_status,
            "practice_status": self.practice_status,
            "practice_pattern": self.practice_pattern,
            "practice_trend": self.practice_trend,
            "practice_source": self.practice_source,
            "injury_source": self.injury_source,
            "on_bye": self.on_bye,
            "bye_status": self.bye_status,
            "projected_points": self.projected_points,
            "floor": self.floor,
            "ceiling": self.ceiling,
            "base_source": self.base_source,
            "implied_total": self.implied_total,
            "opponent_implied_total": self.opponent_implied_total,
            "unit_matchup": self.unit_matchup,
            "sleeper_projection": self.sleeper_projection,
            "consensus": self.consensus,
            "disagreement": self.disagreement,
            "projection_gap": self.projection_gap,
            "kickoff": self.kickoff,
            "kickoff_local": self.kickoff_local,
            "kickoff_weekday": self.kickoff_weekday,
            "locked": self.locked,
            "game_status": self.game_status,
            "decision": self.decision,
            "confidence": self.confidence,
            "confidence_level": self.confidence_level,
            "reasoning": self.reasoning
        }


# Weight factors for confidence calculation
CONFIDENCE_WEIGHTS = {
    "matchup": 0.25,        # Defense vs position ranking
    "usage": 0.25,          # Target share / snap count
    "health": 0.20,         # Injury/practice status
    "projection": 0.15,     # CBS projections
    "trend": 0.15,          # Recent performance trend
}

# Matchup tier scores (inverted - higher score = easier matchup)
MATCHUP_TIER_SCORES = {
    "smash": 95,
    "favorable": 75,
    "neutral": 50,
    "tough": 30,
    "elite": 10,
}

# Injury status scores
INJURY_STATUS_SCORES = {
    "healthy": 100,
    "active": 95,
    "probable": 95,
    "questionable": 60,
    "doubtful": 25,
    "out": 0,
    "ir": 0,
    "injured reserve": 0,
    "suspended": 0,
    "pup": 0,
    "nfi": 0,
    # Sleeper's short codes — what the player feed actually sends. Without
    # these a suspended player scored 100 on health and was never auto-benched.
    "sus": 0,
    "na": 0,
    "dnr": 0,
    "cov": 0,
    "inactive": 0,
    "reserve": 0,
    # "We do not know" — not a green light, not a red one.
    "unknown": 70,
}

# Fallback by availability class (see `projections.availability`) for a
# status the table does not list, so the health score and the projection
# multiplier read every status the same way. A listed status used to be the
# only kind that could bench a player: anything else scored a perfect 100.
_HEALTH_BY_AVAILABILITY = {
    "healthy": 100, "out": 0, "doubtful": 25, "questionable": 60,
    "uncertain": 70, "unrecognised": 60,
}


def injury_score(status: str | None) -> float:
    """Health score (0-100) for an injury status."""
    s = (status or "").strip().lower()
    if not s:
        return 100
    if s in INJURY_STATUS_SCORES:
        return INJURY_STATUS_SCORES[s]
    return _HEALTH_BY_AVAILABILITY[availability(s)]

# Practice status scores
PRACTICE_STATUS_SCORES = {
    "full": 100,
    "full participation": 100,
    "limited": 70,
    "limited participation": 70,
    "dnp": 30,
    "did not participate": 30,
    # The short codes roster enrichment emits; without them an "FP" scored as
    # an unknown 70 rather than a full practice.
    "fp": 100,
    "lp": 70,
    "rest": 85,  # Veteran rest day is usually fine
}

# Usage trend scores
USAGE_TREND_SCORES = {
    "upward": 85,
    "stable": 60,
    "downward": 35,
}


async def _league_scoring(
    league_id: str | None, scoring: str | None
) -> tuple[str, int, str]:
    """(scoring, num_teams, scoring_source) for a start/sit call.

    The briefing reads scoring and league size from the league; these tools
    defaulted to full PPR and 12 teams, so the same player in the same week got
    different points depending on which tool was asked. An explicit `scoring`
    still wins; `league_id` supplies it otherwise; with neither, full PPR is
    assumed and labelled as such rather than passed off as the league's.

    The league's scoring comes back as a `scoring.LeagueScoring`: the same
    exact-reception string as before, carrying the full scoring_settings to
    the projections. An explicit `scoring` at the league's own reception value
    keeps those settings too.
    """
    num_teams = 12
    if league_id:
        try:
            from . import sleeper_tools
            from .briefing_tools import _scoring_ppr
            league = ((await sleeper_tools.get_league(league_id)) or {}).get("league") or {}
        except Exception as e:  # a league lookup must not sink the recommendation
            logger.debug(f"league lookup for scoring failed: {e}")
            league = {}
        if league:
            num_teams = int(league.get("total_rosters") or 12)
            carrier = league_scoring(league)
            if not scoring:
                return carrier, num_teams, "league"
            if scoring_to_ppr(scoring) == _scoring_ppr(league):
                return carrier, num_teams, "caller"
    if scoring:
        return scoring, num_teams, "caller"
    return "ppr", num_teams, "default"


# Shared with the projection tools, which used to skip this step and answer
# the same question off a weaker baseline (see `week_context`).
_resolve_season_week = resolve_season_week


class LineupOptimizer:
    """
    Lineup optimizer that combines multiple factors for start/sit decisions.

    Uses a weighted scoring system to calculate confidence in recommendations.
    """

    def __init__(self, db=None, defense_analyzer=None, auto_project=True):
        """Initialize the optimizer with optional database and analyzer.

        auto_project: when True, fill projected points from the internal
        projection engine whenever the caller doesn't supply them.
        """
        self.db = db
        self.defense_analyzer = defense_analyzer
        self.auto_project = auto_project
        self._init_dependencies()

    def _init_dependencies(self):
        """Initialize dependencies if not provided."""
        if self.db is None:
            try:
                from .database import get_shared_db
                self.db = get_shared_db()
            except Exception as e:
                logger.debug(f"Database init failed: {e}")

        if self.defense_analyzer is None:
            try:
                from .matchup_tools import get_defense_analyzer
                self.defense_analyzer = get_defense_analyzer()
            except Exception as e:
                logger.debug(f"Defense analyzer init failed: {e}")

    def calculate_confidence(
        self, analysis: PlayerAnalysis, ppr: float = 1.0
    ) -> tuple[float, str, list[str]]:
        """
        Calculate confidence score and generate reasoning.

        Args:
            analysis: the player analysis to score
            ppr: the league's per-reception value, so "a good game" is judged on
                the same scale the projection is on

        Returns:
            Tuple of (confidence_score, confidence_level, reasoning_list)
        """
        scores = {}
        reasoning = []

        # 1. Matchup score
        matchup_score = MATCHUP_TIER_SCORES.get(analysis.matchup_tier, 50)
        scores["matchup"] = matchup_score

        # A kicker's or defense's rank is an *offense* rank, and saying "#30
        # vs DEF" would read as a defense-vs-position number.
        unit = analysis.unit_matchup
        where = (f"{'opponent' if unit['offense_side'] == 'opponent' else 'own'} offense "
                 f"#{unit['offense_rank']}" if unit
                 else f"#{analysis.matchup_rank} vs {analysis.position}")
        if matchup_score >= 75:
            reasoning.append(f"✅ Favorable matchup ({where})")
        elif matchup_score <= 30:
            reasoning.append(f"⚠️ Tough matchup ({where})")

        # 2. Usage score
        usage_score = 50  # Base score
        if analysis.snap_percentage > 0:
            # Normalize snap percentage to 0-100 score
            usage_score = min(100, analysis.snap_percentage * 1.2)  # 83%+ snaps = 100

            if analysis.snap_percentage >= 80:
                reasoning.append(f"✅ High snap count ({analysis.snap_percentage:.0f}%)")
            elif analysis.snap_percentage < 50:
                reasoning.append(f"⚠️ Low snap share ({analysis.snap_percentage:.0f}%)")

        if analysis.target_share > 0:
            # Add target share bonus for pass catchers
            if analysis.position in ["WR", "TE", "RB"]:
                target_bonus = min(30, analysis.target_share * 1.5)
                usage_score = min(100, usage_score * 0.7 + target_bonus + 20)

                if analysis.target_share >= 25:
                    reasoning.append(f"✅ High target share ({analysis.target_share:.1f}%)")

        scores["usage"] = usage_score

        # 3. Health score
        inj_score = injury_score(analysis.injury_status)
        practice = (analysis.practice_status or "").strip().lower()
        if practice:
            practice_score = PRACTICE_STATUS_SCORES.get(practice, 70)
            health_score = (inj_score * 0.6 + practice_score * 0.4)
        else:
            # No practice report: score the designation alone rather than
            # inventing a full practice to average it with.
            practice_score = None
            health_score = float(inj_score)
        scores["health"] = health_score

        if inj_score < 60:
            reasoning.append(f"⚠️ Injury concern: {analysis.injury_status}")
        elif availability(analysis.injury_status) == "uncertain":
            reasoning.append(f"⚠️ Status {analysis.injury_status!r} — verify before kickoff")
        if practice_score is not None and practice_score < 70:
            line = (f" ({analysis.practice_pattern}, {analysis.practice_trend})"
                    if analysis.practice_pattern and "-" in analysis.practice_pattern else "")
            reasoning.append(f"⚠️ Limited practice: {analysis.practice_status}{line}")
        elif analysis.practice_trend == "worsening":
            reasoning.append(f"⚠️ Practice trending down: {analysis.practice_pattern}")
        if health_score >= 90:
            reasoning.append("✅ Healthy, full practice" if practice_score is not None
                             else "✅ No injury designation")

        # 4. Projection score
        projection_score = 50  # Default
        if analysis.projected_points > 0:
            # Scale based on position expectations, rebased to this league's
            # scoring — these are full-PPR "good game" marks, and comparing a
            # half-PPR projection against them demoted every pass catcher.
            floor_thresh, ceil_thresh = _good_game_thresholds(
                analysis.position, ppr, analysis.threshold_scale)

            if analysis.projected_points >= ceil_thresh:
                projection_score = 90
                reasoning.append(f"✅ Strong projection ({analysis.projected_points:.1f} pts)")
            elif analysis.projected_points >= floor_thresh:
                projection_score = 70
            elif analysis.projected_points < floor_thresh * 0.7:
                projection_score = 30
                reasoning.append(f"⚠️ Low projection ({analysis.projected_points:.1f} pts)")
            else:
                projection_score = 50

        scores["projection"] = projection_score

        # 5. Trend score
        trend_score = USAGE_TREND_SCORES.get(analysis.usage_trend.lower(), 60)
        scores["trend"] = trend_score

        if trend_score >= 80:
            reasoning.append("📈 Usage trending up")
        elif trend_score <= 40:
            reasoning.append("📉 Usage trending down")

        # Calculate weighted confidence
        total_confidence = sum(
            scores[factor] * weight
            for factor, weight in CONFIDENCE_WEIGHTS.items()
        )

        # Determine confidence level
        if total_confidence >= 75:
            confidence_level = ConfidenceLevel.HIGH.value
        elif total_confidence >= 50:
            confidence_level = ConfidenceLevel.MEDIUM.value
        else:
            confidence_level = ConfidenceLevel.LOW.value

        return total_confidence, confidence_level, reasoning

    def determine_decision(
        self,
        projected_points: float,
        position: str,
        health_score: float,
        ppr: float = 1.0,
        confidence: float | None = None,
        injury_status: str | None = None,
        on_bye: bool = False,
        unit_scale: float = 1.0,
    ) -> str:
        """Start/sit decision from expected points, not from how much we know.

        Availability comes first and is read the way the projection reads it
        (`projections.availability`):

        - on bye, or a status that rules him out (Out, IR, Inactive, Reserve,
          Sus, PUP, ...): must_sit;
        - Doubtful: decided on the projection, which already carries the ×0.35
          discount, but never better than `sit` — "risky, avoid". It used to be
          a flat must_sit here while the projection priced him at a third of
          his points, so the two tools disagreed about whether he was a zero.
          He is not: doubtful players do sometimes play. But no lineup should
          count on one;
        - an "Unknown" designation: at most `start`, never `must_start`.

        Without `injury_status` (older callers) a health score of 25 or less
        still means must_sit.

        This used to key off `confidence`, which is a *data-quality* score:
        25% matchup, 25% snap share, 20% health, 15% projection, 15% trend. The
        expected points — the only quantity that decides a lineup — carried 15%
        of the weight and were quantised into four buckets. The result was an
        inverted ranking: a 17-point receiver in a tough matchup came out below
        an 8-point receiver in a smash matchup.

        Worse, `must_start` required a "smash" or "favorable" matchup, so a star
        facing a top defense could never be a must-start — while the projection
        engine's own backtest had already concluded that matchup is worth
        *nothing* for WRs (`_MATCHUP_POS_STRENGTH["WR"] == 0.0`). The ranking
        layer weighted the same signal at 25% and contradicted the model
        underneath it.

        Points are compared against the position's "good week" marks, rebased to
        the league's scoring. `confidence` is still reported — it answers "how
        much do we know", which is a real question, just not this one.
        """
        # An unavailable player is not a lineup question.
        kind = availability(injury_status) if injury_status is not None else None
        if on_bye or health_score <= 0 or kind == "out":
            return StartSitDecision.MUST_SIT.value
        if kind is None and health_score <= 25:
            return StartSitDecision.MUST_SIT.value

        decision = self._decision_from_points(projected_points, position, ppr, unit_scale)
        if kind == "doubtful" and decision in _BETTER_THAN_SIT:
            return StartSitDecision.SIT.value
        if kind == "uncertain" and decision == StartSitDecision.MUST_START.value:
            return StartSitDecision.START.value
        return decision

    @staticmethod
    def _decision_from_points(projected_points: float, position: str, ppr: float,
                              unit_scale: float = 1.0) -> str:
        adequate, good = _good_game_thresholds(position, ppr, unit_scale)
        if projected_points <= 0:
            # No projection at all: say we cannot tell rather than implying a
            # read. FLEX is the honest "your call" bucket.
            return StartSitDecision.FLEX.value
        if projected_points >= good:
            return StartSitDecision.MUST_START.value
        if projected_points >= adequate:
            return StartSitDecision.START.value
        if projected_points >= adequate * 0.7:
            return StartSitDecision.FLEX.value
        if projected_points >= adequate * 0.45:
            return StartSitDecision.SIT.value
        return StartSitDecision.MUST_SIT.value

    @staticmethod
    def _unit_fallback(analysis: PlayerAnalysis, pp: dict, injury_data: dict | None) -> None:
        """Price a K/DEF off his offense read when there are no Vegas totals.

        Without live lines the projection engine has no game total to price a
        kicker or defense on and returns the same constant for all 32 teams,
        which makes every K/DEF start/sit a coin flip. The season's scoring
        (the streaming planner's key-free projection) separates them.
        """
        unit = analysis.unit_matchup
        if (analysis.position not in UNIT_POSITIONS or pp.get("vegas_active")
                or not unit or unit.get("projected_points") is None):
            return
        inj = (pp.get("breakdown") or {}).get("injury_mult", 1.0)
        projected = round(unit["projected_points"] * inj, 1)
        vol = _VOLATILITY.get(analysis.position, 0.9)
        analysis.projected_points = projected
        analysis.floor = round(projected * (1 - vol), 1)
        analysis.ceiling = round(projected * (1 + vol), 1)
        analysis.base_source = "offense_rank"

    async def _second_opinion(self, analysis: PlayerAnalysis, player_id: str | None,
                              scoring, season: int | None, week: int | None) -> None:
        """Attach Sleeper's projection, the consensus and the disagreement flag."""
        if analysis.on_bye or not season or not week:
            return
        try:
            from . import sleeper_projections as sp
            index = await sp.fetch_week_projections(season, week)
            row = sp.lookup(index, player_id=player_id, name=analysis.player_name,
                            team=analysis.team, position=analysis.position)
            theirs = sp.price_stats(row["stats"], resolve_scoring(scoring)) if row else None
        except Exception as e:  # a second opinion must never sink the first
            logger.debug(f"Sleeper second opinion failed for {analysis.player_name}: {e}")
            return
        from .sleeper_projections import second_opinion
        # No projection at all (0 without a reason) is not a number to compare;
        # a zero because he is ruled out is.
        ruled_out = availability(analysis.injury_status) == "out"
        ours = analysis.projected_points if (analysis.projected_points or ruled_out) else None
        op = second_opinion(ours, theirs, ruled_out=ruled_out)
        analysis.sleeper_projection = op["sleeper_projection"]
        analysis.consensus = op["consensus"]
        analysis.disagreement = op["disagreement"]
        analysis.projection_gap = op["gap"]

    @staticmethod
    def _unit_reasons(analysis: PlayerAnalysis) -> list[str]:
        """The matchup and game-environment lines for a kicker or defense."""
        reasons = []
        unit = analysis.unit_matchup
        is_def = analysis.position in ("DEF", "DST")
        if unit:
            who = f"Opponent {analysis.opponent}'s" if is_def else f"{analysis.team}'s own"
            line = (f"{'🛡️' if is_def else '🦵'} {who} offense ranks "
                    f"#{unit['offense_rank']} of 32 ({unit['points_per_game']} pts/game)")
            if unit.get("tier_withheld"):
                line += f" — only {unit['games']} games, tier withheld"
            if unit.get("is_fallback"):
                line += f" — {unit['source_season']} data"
            reasons.append(line)
        total = analysis.opponent_implied_total if is_def else analysis.implied_total
        if total is not None:
            reasons.append(f"🎲 {'Opponent' if is_def else 'Team'} implied total {total}")
        if analysis.base_source == "offense_rank":
            reasons.append("ℹ️ No live Vegas totals — projected from the season's scoring")
        elif total is None and not unit:
            reasons.append("⚠️ No Vegas totals or offense rankings — generic "
                           f"{'defense' if is_def else 'kicker'} baseline")
        return reasons

    async def analyze_player(
        self,
        player_name: str,
        player_id: str,
        position: str,
        team: str,
        opponent: str,
        usage_data: dict | None = None,
        injury_data: dict | None = None,
        projection_data: dict | None = None,
        scoring: str = "ppr",
        season: int | None = None,
        week: int | None = None,
        num_teams: int = 12,
    ) -> PlayerAnalysis:
        """
        Analyze a single player for start/sit recommendation.

        Args:
            player_name: Player's full name
            player_id: Player's ID
            position: Player position (QB, RB, WR, TE, K, DEF)
            team: Player's team abbreviation
            opponent: Opponent team abbreviation
            usage_data: Optional usage statistics
            injury_data: Optional injury information
            projection_data: Optional projection data
            scoring: League scoring for the auto-projection ('ppr', 'half_ppr',
                'standard', or a raw per-reception value like '0.5')
            season, week: pass both to use the opportunity baseline (week > 1)
            num_teams: League size, for the market-value baseline

        Returns:
            PlayerAnalysis with decision and confidence
        """
        analysis = PlayerAnalysis(
            player_name=player_name,
            player_id=player_id,
            position=position.upper(),
            team=team.upper(),
            opponent=opponent.upper()
        )

        # A team without a game this week: nothing else about him matters. This
        # used to fall through to a full projection, and a receiver on bye came
        # back as a 17-point must-start.
        bye = bye_check(team, opponent, week_schedule(self.db, season, week), week)
        analysis.bye_status = bye["status"]
        if bye["status"] == BYE:
            analysis.on_bye = True
            analysis.opponent = "BYE"
            analysis.matchup_tier = "bye"
            if (injury_data or {}).get("status"):
                analysis.injury_status = injury_data["status"]
                analysis.injury_source = "caller"
            analysis.base_source = "bye"
            analysis.decision = StartSitDecision.MUST_SIT.value
            # Certain, not merely well-documented: no game, no points.
            analysis.confidence = 100.0
            analysis.confidence_level = ConfidenceLevel.HIGH.value
            analysis.reasoning = [f"🚫 {bye['reason']}"]
            return analysis
        # A blank opponent is filled from the schedule when it knows the game.
        opponent = bye["opponent"] or opponent
        analysis.opponent = opponent.upper()

        # Kickoff and lock. Informational for the player himself — his
        # projection still stands — but it decides what the lineup tools may
        # move: a locked player stays where he is.
        lock = game_lock(week_games(self.db, season, week).get(normalize_team(team) or ""), _now())
        analysis.kickoff = lock["kickoff"]
        analysis.kickoff_local = lock["kickoff_local"]
        analysis.kickoff_weekday = lock["kickoff_weekday"]
        analysis.locked = lock["locked"]
        analysis.game_status = lock["game_status"]

        # Get matchup data
        if self.defense_analyzer and position.upper() in ["QB", "RB", "WR", "TE"]:
            try:
                rankings = await self.defense_analyzer.fetch_defense_rankings()
                matchup = self.defense_analyzer.get_matchup_difficulty(
                    position.upper(),
                    opponent.upper(),
                    rankings
                )
                analysis.matchup_rank = matchup.get("rank", 16)
                analysis.matchup_tier = matchup.get("matchup_tier", "neutral")
            except Exception as e:
                logger.debug(f"Matchup lookup failed: {e}")
        elif position.upper() in UNIT_POSITIONS:
            # A kicker's or defense's matchup is an offense: the opponent's for
            # a defense, his own for a kicker. There is no defense-vs-K table.
            analysis.threshold_scale = unit_threshold_scale(position, scoring)
            try:
                from .streaming_tools import unit_matchup
                unit = await unit_matchup(position, team, opponent, season,
                                          resolve_scoring(scoring))
            except Exception as e:
                logger.debug(f"K/DEF matchup lookup failed: {e}")
                unit = None
            if unit:
                analysis.unit_matchup = unit
                analysis.matchup_rank = unit["offense_rank"]
                analysis.matchup_tier = unit["matchup_tier"]

        # Apply usage data
        if usage_data:
            analysis.target_share = usage_data.get("target_share", 0.0)
            analysis.snap_percentage = usage_data.get("snap_percentage", 0.0)
            analysis.red_zone_opportunities = usage_data.get("red_zone_opportunities", 0)
            analysis.usage_trend = usage_data.get("usage_trend", "stable")

        # A caller that says nothing about health is not saying "healthy". Look
        # the player up in the same injury tables the briefing reads, or start/
        # sit starts a player the briefing benches (Brock Bowers, Out, came back
        # as a starter here and 0 points there).
        if not (injury_data or {}).get("status"):
            from .injury_match import lookup_injury
            found = lookup_injury(self.db, player_name, team)
            if found:
                injury_data = {**(injury_data or {}), "status": found["status"]}
                analysis.injury_source = found["source"]

        # Apply injury data. `or`, not a .get default: an explicit null status
        # used to reach `.lower()` and crash the whole tool.
        if injury_data:
            analysis.injury_source = analysis.injury_source or "caller"
            analysis.injury_status = injury_data.get("status") or "healthy"
            analysis.practice_status = injury_data.get("practice_status") or None
            if analysis.practice_status:
                analysis.practice_source = "caller"

        # Practice the same way: a caller that passes none gets this week's real
        # report (official NFL.com, else a dated news note), or nothing at all.
        if not analysis.practice_status:
            from .practice_reports import lookup_practice
            practice = lookup_practice(self.db, player_name, team, season=season, week=week)
            if practice:
                analysis.practice_status = practice["latest"]
                analysis.practice_pattern = practice["pattern"]
                analysis.practice_trend = practice["trend"]
                analysis.practice_source = practice["source"]
                # The projection prices the latest report too.
                injury_data = {**(injury_data or {}), "practice_status": practice["latest"]}

        # Apply projection data — or auto-project when the caller didn't supply
        # points, so start/sit works without manual point entry.
        if projection_data and projection_data.get("projected_points"):
            analysis.projected_points = projection_data.get("projected_points", 0.0)
            analysis.floor = projection_data.get("floor", 0.0)
            analysis.ceiling = projection_data.get("ceiling", 0.0)
            # The caller's number is a projection *if he plays*. An Out player
            # scores nothing, as on the auto-projected path (multiplier 0);
            # used as-is it made him the winner of a slot comparison.
            if availability(analysis.injury_status) == "out":
                analysis.projected_points = analysis.floor = analysis.ceiling = 0.0
        elif self.auto_project:
            try:
                from .projections import get_projection_engine
                engine = get_projection_engine(self.db)
                # scoring/season/week travel with the request rather than being
                # defaulted here: without them this path silently produced
                # full-PPR points off the weaker rank-bucket baseline, so a
                # half-PPR league got a different answer from start/sit than
                # from get_weekly_briefing for the very same player.
                pr = await engine.project_many([{
                    "name": player_name, "player_id": player_id,
                    "position": position.upper(), "team": team.upper(),
                    "opponent": opponent.upper(),
                    "usage": usage_data or {}, "injury": injury_data or {},
                }], scoring=scoring, num_teams=num_teams, season=season, week=week)
                if pr.get("projections"):
                    pp = pr["projections"][0]
                    analysis.projected_points = pp["projected_points"]
                    analysis.floor = pp["floor"]
                    analysis.ceiling = pp["ceiling"]
                    analysis.base_source = (pp.get("breakdown") or {}).get("base_source")
                    analysis.implied_total = pp.get("implied_total") if pp.get("vegas_active") else None
                    analysis.opponent_implied_total = (
                        pp.get("opponent_implied_total") if pp.get("vegas_active") else None)
                    self._unit_fallback(analysis, pp, injury_data)
            except Exception as e:
                logger.debug(f"Auto-projection failed for {player_name}: {e}")

        await self._second_opinion(analysis, player_id, scoring, season, week)
        extra_reasons = self._unit_reasons(analysis) if analysis.position in UNIT_POSITIONS else []
        if analysis.disagreement:
            extra_reasons.append(
                f"🔍 Sleeper projects {analysis.sleeper_projection} vs our "
                f"{analysis.projected_points} — the two disagree, worth a look")

        # Calculate confidence and decision
        confidence, confidence_level, reasoning = self.calculate_confidence(
            analysis, ppr=scoring_to_ppr(scoring)
        )

        analysis.confidence = round(confidence, 1)
        analysis.confidence_level = confidence_level
        analysis.reasoning = reasoning + extra_reasons
        if analysis.locked:
            analysis.reasoning.insert(0, (
                f"🔒 Game {'over' if analysis.game_status == 'final' else 'under way'} "
                f"(kickoff {analysis.kickoff_local}) — locked, he can no longer be "
                "moved into or out of the lineup"))

        # Determine decision. Health is read through the same vocabulary as the
        # projection's multiplier: an unlisted status used to score 100 here.
        analysis.decision = self.determine_decision(
            analysis.projected_points,
            analysis.position,
            injury_score(analysis.injury_status),
            ppr=scoring_to_ppr(scoring),
            confidence=analysis.confidence,
            injury_status=analysis.injury_status,
            unit_scale=analysis.threshold_scale,
        )
        if availability(analysis.injury_status) == "doubtful":
            analysis.reasoning.append(
                "⚠️ Doubtful — risky, avoid: projected at 35% of his points, "
                "and rarely plays"
            )

        return analysis

    async def analyze_roster(
        self,
        players: list[dict],
        week: int | None = None,
        season: int | None = None,
        scoring: str = "ppr",
        num_teams: int = 12,
    ) -> dict[str, list[PlayerAnalysis]]:
        """
        Analyze a full roster and return sorted recommendations by position.

        Args:
            players: List of player dicts with name, position, team, opponent
            week: NFL week — reaches the projection, not just the response
            season: Season; with `week` > 1 this selects the opportunity baseline
            scoring: League scoring for the projections

        Returns:
            Dict mapping position to sorted list of player analyses
        """
        analyses_by_position: dict[str, list[PlayerAnalysis]] = {}

        # Analyze all players concurrently
        tasks = []
        for player in players:
            task = self.analyze_player(
                player_name=player.get("name", "Unknown"),
                player_id=player.get("player_id", ""),
                position=player.get("position", ""),
                team=player.get("team", ""),
                opponent=player.get("opponent", ""),
                usage_data=player.get("usage"),
                injury_data=player.get("injury"),
                projection_data=player.get("projection"),
                scoring=scoring,
                num_teams=num_teams,
                season=season,
                week=week,
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Group by position
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Analysis failed: {result}")
                continue

            position = result.position
            if position not in analyses_by_position:
                analyses_by_position[position] = []
            analyses_by_position[position].append(result)

        # Sort each position by projected points; confidence only breaks ties,
        # the same order as the flat recommendation list.
        for position in analyses_by_position:
            analyses_by_position[position].sort(
                key=lambda x: (x.projected_points, x.confidence),
                reverse=True
            )

        return analyses_by_position


# Singleton instance
_lineup_optimizer: LineupOptimizer | None = None


def get_lineup_optimizer() -> LineupOptimizer:
    """Get or create singleton LineupOptimizer instance."""
    global _lineup_optimizer
    if _lineup_optimizer is None:
        _lineup_optimizer = LineupOptimizer()
    return _lineup_optimizer


# MCP Tool Functions

@handle_http_errors(
    default_data={"recommendation": None},
    operation_name="generating start/sit recommendation"
)
async def get_start_sit_recommendation(
    player_name: str,
    position: str,
    team: str,
    opponent: str = "",
    player_id: str | None = None,
    target_share: float | None = None,
    snap_percentage: float | None = None,
    injury_status: str | None = None,
    practice_status: str | None = None,
    projected_points: float | None = None,
    scoring: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
    week: int | None = None,
) -> dict:
    """
    Get a start/sit recommendation for a single player.

    Analyzes matchup difficulty, usage trends, health status, and projections
    to provide a confidence-weighted recommendation.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        player_name: Player's full name
        position: Fantasy position (QB, RB, WR, TE, K, DEF — a DEF is its team code)
        team: Player's team abbreviation
        opponent: Opponent team abbreviation
        player_id: Optional player ID for database lookup
        target_share: Optional target share percentage (0-100)
        snap_percentage: Optional snap count percentage (0-100)
        injury_status: Optional injury status (healthy, questionable, doubtful, out)
        practice_status: Optional practice status (full, limited, dnp)
        projected_points: Optional projected fantasy points
        scoring: League scoring ('ppr', 'half_ppr', 'standard', or '0.5').
            Pass your league's real setting — it changes both the points and
            what counts as a good week.
        season, week: pass both (week > 1) for the opportunity baseline

    Returns:
        Dictionary containing:
        - recommendation: Start/sit decision details, with projected_points,
          floor and ceiling
        - confidence: Confidence score (0-100)
        - confidence_level: high/medium/low
        - reasoning: List of factors in the decision
        - matchup_tier: Matchup difficulty tier
        - scoring: The scoring the projection was made in

    Example:
        get_start_sit_recommendation(
            player_name="Tyreek Hill",
            position="WR",
            team="MIA",
            opponent="NE",
            target_share=28.5,
            snap_percentage=95
        )
    """
    optimizer = get_lineup_optimizer()
    season, week, week_inferred = await _resolve_season_week(season, week)
    scoring, num_teams, scoring_source = await _league_scoring(league_id, scoring)

    # Build optional data dicts
    usage_data = {}
    if target_share is not None:
        usage_data["target_share"] = target_share
    if snap_percentage is not None:
        usage_data["snap_percentage"] = snap_percentage

    injury_data = {}
    if injury_status:
        injury_data["status"] = injury_status
    if practice_status:
        injury_data["practice_status"] = practice_status

    projection_data = {}
    if projected_points is not None:
        projection_data["projected_points"] = projected_points

    # Analyze player
    analysis = await optimizer.analyze_player(
        player_name=player_name,
        player_id=player_id or "",
        position=position,
        team=team,
        opponent=opponent,
        usage_data=usage_data if usage_data else None,
        injury_data=injury_data if injury_data else None,
        projection_data=projection_data if projection_data else None,
        scoring=scoring,
        num_teams=num_teams,
        season=season,
        week=week,
    )

    # Format decision display
    decision_emoji = {
        "must_start": "🟢🟢",
        "start": "🟢",
        "flex": "🟡",
        "sit": "🔴",
        "must_sit": "🔴🔴",
    }

    decision_display = f"{decision_emoji.get(analysis.decision, '⚪')} {analysis.decision.upper().replace('_', ' ')}"

    return create_success_response({
        "recommendation": {
            "player": analysis.player_name,
            "position": analysis.position,
            "team": analysis.team,
            "opponent": analysis.opponent,
            "decision": analysis.decision,
            "decision_display": decision_display,
            # The band is calibrated (see evals/backtest/calibration.py) and was
            # being dropped here, leaving only a points string in `factors`.
            "projected_points": analysis.projected_points,
            "floor": analysis.floor,
            "ceiling": analysis.ceiling,
            "base_source": analysis.base_source,
            # Second opinion: Sleeper's projection in this league's scoring.
            # The decision above is made on `projected_points` alone.
            "sleeper_projection": analysis.sleeper_projection,
            "consensus": analysis.consensus,
            "disagreement": analysis.disagreement,
            "projection_gap": analysis.projection_gap,
            "implied_total": analysis.implied_total,
            "opponent_implied_total": analysis.opponent_implied_total,
            "unit_matchup": analysis.unit_matchup,
            "on_bye": analysis.on_bye,
            "bye_status": analysis.bye_status,
            "kickoff": analysis.kickoff,
            "kickoff_local": analysis.kickoff_local,
            "kickoff_weekday": analysis.kickoff_weekday,
            # His game has started: whatever the decision, it can no longer
            # be acted on.
            "locked": analysis.locked,
            "game_status": analysis.game_status,
        },
        "confidence": analysis.confidence,
        "confidence_level": analysis.confidence_level,
        "matchup_tier": analysis.matchup_tier,
        "matchup_rank": analysis.matchup_rank,
        "reasoning": analysis.reasoning,
        "scoring": scoring,
        "season": season,
        "week": week,
        "week_inferred": week_inferred,
        "scoring_source": scoring_source,
        "scoring_used": scoring_used(scoring),
        "factors": {
            "matchup": f"#{analysis.matchup_rank} ({analysis.matchup_tier})",
            "usage": f"Snaps: {analysis.snap_percentage}%, Targets: {analysis.target_share}%",
            # Practice is only stated when someone reported it: the default
            # used to print "Practice: full" next to an Out designation.
            "health": (f"{analysis.injury_status}, Practice: "
                       f"{analysis.practice_pattern or analysis.practice_status}"
                       if analysis.practice_status else analysis.injury_status),
            "practice": {
                "status": analysis.practice_status,
                "pattern": analysis.practice_pattern,
                "trend": analysis.practice_trend,
                "source": analysis.practice_source or "unreported",
            },
            "schedule": "on bye" if analysis.on_bye else analysis.bye_status,
            "projection": f"{analysis.projected_points} pts" if analysis.projected_points > 0 else "N/A",
        },
        "message": (f"{analysis.player_name}: {decision_display} (Confidence: {analysis.confidence:.0f}%)"
                    + (f" — locked, his game kicked off {analysis.kickoff_local}"
                       if analysis.locked else ""))
    })


@handle_http_errors(
    default_data={"recommendations": []},
    operation_name="generating roster recommendations"
)
async def get_roster_recommendations(
    players: list[dict],
    week: int | None = None,
    include_reasoning: bool = True,
    scoring: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
) -> dict:
    """
    Get start/sit recommendations for multiple players.

    Analyzes all players and returns sorted recommendations by position,
    helping identify optimal lineup decisions.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        players: List of player dicts with:
            - name (str): Player name
            - position (str): QB, RB, WR, TE, K, DEF
            - team (str): Team abbreviation
            - opponent (str): Opponent team abbreviation
            - usage (dict, optional): {target_share, snap_percentage}
            - injury (dict, optional): {status, practice_status}
            - projection (dict, optional): {projected_points}
        week: NFL week — with `season` and week > 1 this selects the opportunity
            baseline for the projections, not just a label on the response
        include_reasoning: Whether to include detailed reasoning (default: True)
        scoring: League scoring ('ppr', 'half_ppr', 'standard', or '0.5')
        season: Season year, needed with `week` for the opportunity baseline

    Returns:
        Dictionary containing:
        - recommendations: All player recommendations sorted by projected
          points (confidence breaks ties)
        - by_position: Recommendations grouped by position, in the same order
        - must_starts: Players with must_start decision
        - sits: Players with sit or must_sit decision
        - summary: Quick summary text

    Example:
        get_roster_recommendations(players=[
            {"name": "Patrick Mahomes", "position": "QB", "team": "KC", "opponent": "LV"},
            {"name": "Tyreek Hill", "position": "WR", "team": "MIA", "opponent": "NE",
             "usage": {"target_share": 28, "snap_percentage": 95}}
        ])
    """
    if not players:
        return create_error_response(
            "No players provided for analysis",
            error_type=ErrorType.VALIDATION,
            data={"recommendations": [], "by_position": {}}
        )

    optimizer = get_lineup_optimizer()

    # Analyze roster
    season, week, week_inferred = await _resolve_season_week(season, week)
    scoring, num_teams, scoring_source = await _league_scoring(league_id, scoring)
    analyses_by_position = await optimizer.analyze_roster(
        players, week=week, season=season, scoring=scoring, num_teams=num_teams
    )

    # Flatten and convert to dicts
    all_recommendations = []
    must_starts = []
    sits = []
    on_bye = []
    locked = []

    for _position, analyses in analyses_by_position.items():
        for analysis in analyses:
            rec = analysis.to_dict()
            if not include_reasoning:
                rec.pop("reasoning", None)
            all_recommendations.append(rec)

            if analysis.decision == "must_start":
                must_starts.append(f"{analysis.player_name} ({analysis.position})")
            elif analysis.decision in ["sit", "must_sit"]:
                sits.append(f"{analysis.player_name} ({analysis.position})")
            if analysis.on_bye:
                on_bye.append(f"{analysis.player_name} ({analysis.position})")
            if analysis.locked:
                locked.append(f"{analysis.player_name} ({analysis.position})")

    # Ranked by expected points. `confidence` breaks ties only: it measures how
    # much we know about a player, which is not the same question as who scores
    # more, and sorting by it inverted the ranking (see `determine_decision`).
    all_recommendations.sort(
        key=lambda x: (x["projected_points"], x["confidence"]), reverse=True
    )

    # Convert by_position to serializable format
    by_position = {
        pos: [a.to_dict() for a in analyses]
        for pos, analyses in analyses_by_position.items()
    }

    # Generate summary
    summary_lines = []
    if must_starts:
        summary_lines.append(f"🟢 MUST STARTS: {', '.join(must_starts)}")
    if sits:
        summary_lines.append(f"🔴 CONSIDER SITTING: {', '.join(sits)}")
    if on_bye:
        summary_lines.append(f"🚫 ON BYE: {', '.join(on_bye)}")
    if locked:
        summary_lines.append(f"🔒 LOCKED (game started, cannot be moved): {', '.join(locked)}")

    return create_success_response({
        "recommendations": all_recommendations,
        "by_position": by_position,
        "must_starts": must_starts,
        "sits": sits,
        "on_bye": on_bye,
        # Players whose game has started: their start/sit is already decided.
        "locked": locked,
        "summary": summary_lines,
        "total_analyzed": len(all_recommendations),
        "week": week,
        "season": season,
        "week_inferred": week_inferred,
        "scoring_source": scoring_source,
        "scoring_used": scoring_used(scoring),
        "scoring": scoring,
        "message": f"Analyzed {len(all_recommendations)} players"
    })


@handle_http_errors(
    default_data={"comparison": None},
    operation_name="comparing players"
)
async def compare_players_for_slot(
    players: list[dict],
    slot: str = "FLEX",
    scoring: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
    week: int | None = None,
) -> dict:
    """
    Compare multiple players competing for the same roster slot.

    Useful for deciding between players for a specific position or flex spot.
    Returns a ranked comparison with the recommended starter.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        players: List of player dicts to compare (2-5 players)
            Each should have: name, position, team, opponent
            Optional: usage, injury, projection dicts
        slot: The roster slot being filled (e.g., "WR2", "FLEX", "RB1")
        scoring: League scoring ('ppr', 'half_ppr', 'standard', or '0.5').
            This is the comparison most sensitive to it — half PPR is what makes
            a runner competitive with a volume receiver for a flex spot.
        season, week: pass both (week > 1) for the opportunity baseline

    Returns:
        Dictionary containing:
        - winner: The recommended player to start
        - comparison: Ranked list of players with analysis
        - confidence_gap: Difference between top 2 options
        - verdict: Summary of the decision

    Example:
        compare_players_for_slot(
            players=[
                {"name": "Player A", "position": "WR", "team": "KC", "opponent": "LV"},
                {"name": "Player B", "position": "RB", "team": "SF", "opponent": "ARI"}
            ],
            slot="FLEX"
        )
    """
    if not players or len(players) < 2:
        return create_error_response(
            "Need at least 2 players to compare",
            error_type=ErrorType.VALIDATION,
            data={"comparison": None}
        )

    # Only players the slot can legally hold are candidates: a FLEX comparison
    # used to rank a quarterback first and "start" him in a seat he cannot
    # fill. A player without a position is kept (nothing to judge him on), and
    # a slot name we do not know filters nobody.
    ineligible = []
    if is_known_slot(slot):
        eligible = []
        for player in players:
            pos = player.get("position")
            if pos and not slot_accepts(slot, pos):
                ineligible.append({"player": player.get("name", "Unknown"),
                                   "position": (pos or "").upper()})
            else:
                eligible.append(player)
        players = eligible
        if not players:
            return create_error_response(
                f"None of these players can start in a {slot} slot",
                error_type=ErrorType.VALIDATION,
                data={"comparison": None, "ineligible": ineligible},
            )

    if len(players) > 5:
        players = players[:5]  # Limit to 5 players

    optimizer = get_lineup_optimizer()
    season, week, week_inferred = await _resolve_season_week(season, week)
    scoring, num_teams, scoring_source = await _league_scoring(league_id, scoring)

    # Analyze all players
    analyses = []
    starting_ids: set[int] = set()
    for player in players:
        analysis = await optimizer.analyze_player(
            player_name=player.get("name", "Unknown"),
            player_id=player.get("player_id") or "",
            # `or`, not a .get default: an unresolved name arrives with an
            # explicit None, which crashed `.upper()`.
            position=player.get("position") or "",
            team=player.get("team") or "",
            opponent=player.get("opponent") or "",
            usage_data=player.get("usage"),
            injury_data=player.get("injury"),
            projection_data=player.get("projection"),
            scoring=scoring,
            num_teams=num_teams,
            season=season,
            week=week,
        )
        analyses.append(analysis)
        if player.get("starting"):
            starting_ids.add(id(analysis))

    # Points decide the slot, confidence only breaks a tie. A player on bye
    # sorts last whatever the caller claimed he projects, and a player whose
    # game has started sorts behind everyone still movable: he cannot be put
    # into the slot any more.
    analyses.sort(key=lambda x: (not x.locked, not x.on_bye, x.projected_points, x.confidence),
                  reverse=True)
    locked = [a for a in analyses if a.locked]
    # The one case no ranking can change: the slot's current occupant (the
    # caller marks him `starting`) is already playing.
    locked_starter = next((a for a in locked if id(a) in starting_ids), None)

    # Get winner and runner up
    # Winner and runner-up among the players who can still be moved.
    movable = [a for a in analyses if not a.locked] or analyses
    winner = movable[0]
    runner_up = movable[1] if len(movable) > 1 else None

    confidence_gap = winner.confidence - runner_up.confidence if runner_up else 100
    # The gap that matters for a slot decision is in points, not in how much we
    # know. Compared against the projection's own error (MAE ~5.8), so "clear"
    # means clear relative to what the model can actually resolve.
    points_gap = round(winner.projected_points - runner_up.projected_points, 1) if runner_up else 0.0

    # Verdict scaled to the projection's own error. The backtest puts weekly
    # MAE at ~5.8 points, so a 1-point edge is not an edge — calling it one is
    # the false precision this tool used to trade in.
    if winner.on_bye:
        verdict = "Every player compared is on bye — none of them can score in this slot"
    elif runner_up and runner_up.on_bye:
        verdict = (f"Start {winner.player_name}: "
                   + ", ".join(a.player_name for a in analyses if a.on_bye)
                   + " on bye")
    elif not runner_up:
        verdict = f"Only {winner.player_name} to consider"
    elif points_gap >= 5.0:
        verdict = (f"Clear choice: {winner.player_name} projects {points_gap} more "
                   f"points than {runner_up.player_name}")
    elif points_gap >= 2.0:
        verdict = (f"Edge to {winner.player_name} (+{points_gap}), but "
                   f"{runner_up.player_name} is a reasonable alternative")
    else:
        verdict = (f"Coin flip: {winner.player_name} and {runner_up.player_name} are "
                   f"{points_gap} points apart, inside the model's error")

    if locked_starter:
        winner = locked_starter
        verdict = (f"Slot locked: {locked_starter.player_name}'s game kicked off "
                   f"{locked_starter.kickoff_local} — he stays in the {slot}, nothing to change")
    elif locked and len(locked) == len(analyses):
        verdict = "Every player compared has already kicked off — the slot can no longer change"
    elif locked:
        verdict += (" (locked, game started: "
                    + ", ".join(a.player_name for a in locked) + ")")

    # Decision emoji for display
    decision_emoji = {
        "must_start": "🟢🟢",
        "start": "🟢",
        "flex": "🟡",
        "sit": "🔴",
        "must_sit": "🔴🔴",
    }

    comparison_list = []
    for i, analysis in enumerate(analyses, 1):
        comparison_list.append({
            "rank": i,
            "player": analysis.player_name,
            "position": analysis.position,
            "opponent": analysis.opponent,
            "projected_points": analysis.projected_points,
            "floor": analysis.floor,
            "ceiling": analysis.ceiling,
            # Reported, not ranked on: this says how much we know, not who wins.
            "confidence": analysis.confidence,
            "decision": analysis.decision,
            "decision_display": f"{decision_emoji.get(analysis.decision, '⚪')} {analysis.decision.upper().replace('_', ' ')}",
            "matchup_tier": analysis.matchup_tier,
            "sleeper_projection": analysis.sleeper_projection,
            "consensus": analysis.consensus,
            "disagreement": analysis.disagreement,
            "on_bye": analysis.on_bye,
            "kickoff": analysis.kickoff,
            "kickoff_local": analysis.kickoff_local,
            "kickoff_weekday": analysis.kickoff_weekday,
            "locked": analysis.locked,
            "reasoning": analysis.reasoning[:3] if analysis.reasoning else [],  # Top 3 reasons
        })

    return create_success_response({
        "slot": slot,
        "winner": {
            "player": winner.player_name,
            "position": winner.position,
            "projected_points": winner.projected_points,
            "floor": winner.floor,
            "ceiling": winner.ceiling,
            "confidence": winner.confidence,
            "decision": winner.decision,
            "on_bye": winner.on_bye,
            "kickoff": winner.kickoff,
            "kickoff_local": winner.kickoff_local,
            "locked": winner.locked,
            "reasoning": winner.reasoning,
        },
        "comparison": comparison_list,
        # Players left out because the slot cannot hold their position.
        "ineligible": ineligible,
        "points_gap": points_gap,
        "scoring": scoring,
        "season": season,
        "week": week,
        "week_inferred": week_inferred,
        "scoring_source": scoring_source,
        "scoring_used": scoring_used(scoring),
        "confidence_gap": round(confidence_gap, 1),
        "verdict": verdict,
        "total_compared": len(analyses),
        "message": (f"For {slot}: {verdict}" if locked_starter or winner.locked
                    else f"For {slot}: every player compared is on bye" if winner.on_bye
                    else f"For {slot}: Start {winner.player_name} "
                         f"({winner.projected_points} projected, {winner.confidence:.0f}% confidence)")
    })


@handle_http_errors(
    default_data={"analysis": None},
    operation_name="analyzing lineup"
)
async def analyze_full_lineup(
    lineup: dict[str, list[dict]],
    week: int | None = None,
    scoring: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
    empty_slots: list[str] | None = None,
) -> dict:
    """
    Analyze a complete fantasy lineup with optimal lineup suggestions.

    ``empty_slots`` names starting slots nobody holds (Sleeper's "0"): they
    are open seats the optimal lineup fills and the grade counts.

    Takes a full lineup organized by position and provides:
    - Analysis of each starter
    - Identification of weak spots
    - Bench players who should start
    - Overall lineup grade

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        lineup: Dict of slot keys to player lists. Any Sleeper slot name
            works (QB, RB, WR, TE, FLEX, WRRB_FLEX, REC_FLEX, SUPER_FLEX, K,
            DEF/DST); BENCH (or BN) lists the alternatives.
            Example: {
                "QB": [{"name": "...", "team": "...", "opponent": "..."}],
                "RB": [{"name": "...", ...}, {"name": "...", ...}],
                "WR": [...],
                "TE": [...],
                "FLEX": [...],
                "BENCH": [...]
            }
        week: NFL week — with `season` and week > 1 this selects the opportunity
            baseline for the projections, not just a label on the response
        scoring: League scoring ('ppr', 'half_ppr', 'standard', or '0.5')
        season: Season year, needed with `week` for the opportunity baseline

    Returns:
        Dictionary containing:
        - starters: Analysis of each starting position
        - bench: Analysis of bench players
        - suggested_changes: Swaps that turn the set lineup into the optimal one
        - optimal_lineup / optimal_projected: the best legal lineup and its total
        - lineup_grade: Overall grade (A-F), from the share of optimal points started
        - total_projected: Sum of projected points for starters
        - weak_spots: Positions with low confidence

    Example:
        analyze_full_lineup(lineup={
            "QB": [{"name": "Patrick Mahomes", "team": "KC", "opponent": "LV"}],
            "RB": [
                {"name": "Derrick Henry", "team": "BAL", "opponent": "CIN"},
                {"name": "Bijan Robinson", "team": "ATL", "opponent": "NO"}
            ],
            ...
        })
    """
    if not lineup:
        return create_error_response(
            "No lineup provided",
            error_type=ErrorType.VALIDATION,
            data={"analysis": None}
        )

    optimizer = get_lineup_optimizer()
    season, week, week_inferred = await _resolve_season_week(season, week)
    scoring, num_teams, scoring_source = await _league_scoring(league_id, scoring)

    # Every key the caller sends is a slot, in Sleeper's names or ours
    # (SUPER_FLEX, DEF, WRRB_FLEX, ...). A fixed key list used to drop a
    # SUPER_FLEX or DEF starter without a word. Bench keys are the available
    # alternatives; IR/taxi hold nobody who can play.
    bench_keys = [k for k in lineup if (k or "").upper() in ("BENCH", "BN")]
    starter_positions = [k for k in lineup if normalize_slot(k) not in NON_STARTING_SLOTS]

    starters_analysis = {}
    # (slot key, starter) in order; starter None for an empty seat.
    seats: list[tuple[str, PlayerAnalysis | None]] = []
    bench_analysis = []
    all_starter_analyses = []
    suggested_changes = []
    weak_spots = []
    total_projected = 0.0

    # Analyze starters
    for position in starter_positions:
        if position not in lineup:
            continue

        players = lineup[position]
        if not players:
            continue

        position_analyses = []
        for player in players:
            # The slot says where he plays; his own `position` says what he is,
            # and that is what the projection needs. A flex entry carrying an
            # ineligible position used to be silently rewritten to WR, which
            # projected a kicker off receiver baselines — now it is reported.
            actual_position = (player.get("position") or "").upper()
            if not actual_position:
                actual_position = position.upper()
            if not slot_accepts(position, actual_position):
                logger.warning(
                    f"{player.get('name', '?')!r} is a {actual_position} in a "
                    f"{position} slot, which cannot legally hold one — analysed "
                    "as given rather than reassigned"
                )

            analysis = await optimizer.analyze_player(
                player_name=player.get("name", "Unknown"),
                player_id=player.get("player_id", ""),
                position=actual_position,
                team=player.get("team", ""),
                opponent=player.get("opponent", ""),
                usage_data=player.get("usage"),
                injury_data=player.get("injury"),
                projection_data=player.get("projection"),
                scoring=scoring,
                num_teams=num_teams,
                season=season,
                week=week,
            )
            position_analyses.append(analysis)
            all_starter_analyses.append(analysis)
            seats.append((position, analysis))
            total_projected += analysis.projected_points

            # Track weak spots. A slot is weak when it projects poorly for the
            # position, not when we happen to know little about the player —
            # a well-documented 3-point starter is the weak spot, a thinly
            # covered 18-point starter is not.
            adequate, _ = _good_game_thresholds(analysis.position, scoring_to_ppr(scoring),
                                                analysis.threshold_scale)
            if analysis.projected_points < adequate * 0.7:
                weak_spots.append({
                    "position": position,
                    "player": analysis.player_name,
                    "projected_points": analysis.projected_points,
                    "confidence": analysis.confidence,
                    "on_bye": analysis.on_bye,
                    "issue": ("on bye — no game this week" if analysis.on_bye
                              else f"projects {analysis.projected_points} "
                                   f"(adequate for {analysis.position} is {adequate:.1f})"),
                })

        starters_analysis[position] = [a.to_dict() for a in position_analyses]

    # An empty starting slot is a seat like any other — just unoccupied. Left
    # out, an empty FLEX graded A while a 12-point RB sat on the bench.
    for slot_key in empty_slots or []:
        if normalize_slot(slot_key) not in NON_STARTING_SLOTS:
            seats.append((slot_key, None))

    # Analyze bench
    for bench_key in bench_keys:
        for player in lineup.get(bench_key) or []:
            # No silent default: a bench entry without a position used to be
            # treated as a WR, which made a kicker or a defense eligible for a
            # flex spot and projected it off WR baselines.
            actual_position = (player.get("position") or "").upper()
            if not actual_position:
                logger.warning(
                    f"bench player {player.get('name', '?')!r} has no position; "
                    "skipped rather than guessed at"
                )
                continue

            analysis = await optimizer.analyze_player(
                player_name=player.get("name", "Unknown"),
                player_id=player.get("player_id", ""),
                position=actual_position,
                team=player.get("team", ""),
                opponent=player.get("opponent", ""),
                usage_data=player.get("usage"),
                injury_data=player.get("injury"),
                projection_data=player.get("projection"),
                scoring=scoring,
                num_teams=num_teams,
                season=season,
                week=week,
            )
            bench_analysis.append(analysis)

    # The best lineup these players allow, from the same exact optimizer every
    # other tool uses. Suggestions are the difference between it and what is
    # set: comparing each bench player only against "weak" starters missed a
    # 25-point bench WR behind a 10-point starter, offered one bench player for
    # every weak spot at once, and summed those overlapping gains into a best
    # possible total no lineup could reach.
    # A starter whose game has started keeps his seat, and a bench player
    # whose game has started cannot take one: only the rest is optimized.
    open_seats = [i for i, (_, a) in enumerate(seats) if a is None or not a.locked]
    open_best = optimal_lineup(
        [seats[i][1] for i in open_seats if seats[i][1] is not None]
        + [a for a in bench_analysis if not a.locked],
        [normalize_slot(seats[i][0]) for i in open_seats],
        value=lambda a: a.projected_points, position=lambda a: a.position,
    )
    best = [a for _, a in seats]
    for i, a in zip(open_seats, open_best, strict=True):
        best[i] = a
    locked_players = [
        {"slot": key, "player": a.player_name, "kickoff_local": a.kickoff_local,
         "game_status": a.game_status, "starting": True}
        for key, a in seats if a is not None and a.locked
    ] + [
        {"slot": "BENCH", "player": a.player_name, "kickoff_local": a.kickoff_local,
         "game_status": a.game_status, "starting": False}
        for a in bench_analysis if a.locked
    ]
    optimal_total = sum(a.projected_points for a in best if a is not None)
    starting = {id(a) for _, a in seats if a is not None}
    best_ids = {id(a) for a in best if a is not None}
    ins = sorted(
        ((key, a) for key, a in zip((k for k, _ in seats), best, strict=True)
         if a is not None and id(a) not in starting),
        key=lambda t: t[1].projected_points, reverse=True,
    )
    outs = sorted(
        ((key, a) for key, a in seats if a is not None and id(a) not in best_ids),
        key=lambda t: t[1].projected_points,
    )
    # Filling an empty seat costs nobody. A player the optimum puts straight
    # into an empty seat fills it; any empty seat filled by shifting a starter
    # takes one of the remaining newcomers (the others shift over for him).
    empty_keys = [key for key, a in seats if a is None]
    fills: list[tuple[str, PlayerAnalysis]] = []
    for (key, a), b in zip(seats, best, strict=True):
        if a is None and b is not None and id(b) not in starting:
            fills.append((key, b))
            empty_keys.remove(key)
    filled_ids = {id(b) for _, b in fills}
    ins = [(k, b) for k, b in ins if id(b) not in filled_ids]
    filled_by_shift = sum(1 for (_, a), b in zip(seats, best, strict=True)
                          if a is None and b is not None and id(b) in starting)
    for _ in range(min(filled_by_shift, len(empty_keys), max(0, len(ins) - len(outs)))):
        key, b = ins.pop(0)
        fills.append((empty_keys.pop(0), b))
    for slot_key, filler in fills:
        suggested_changes.append({
            "action": "fill",
            "bench_in": filler.player_name,
            "bench_in_points": filler.projected_points,
            "bench_out": None,
            "bench_out_points": 0.0,
            "slot": slot_key,
            "out_slot": None,
            "gain": round(filler.projected_points, 1),
            "reason": f"Fill empty {slot_key} with {filler.player_name} "
                      f"({filler.projected_points} projected)",
        })
    for (in_slot, bench_in), (out_slot, bench_out) in zip(ins, outs, strict=False):
        # Points, not confidence, and wide enough to sit outside the noise.
        gain = bench_in.projected_points - bench_out.projected_points
        if gain < MEANINGFUL_SWAP_GAIN:
            continue
        reason = f"{bench_in.player_name} projects {round(gain, 1)} more points"
        if normalize_slot(in_slot) != normalize_slot(out_slot):
            reason += (f"; he starts at {in_slot} and the others shift to "
                       f"free {bench_out.player_name}'s {out_slot} seat")
        suggested_changes.append({
            "action": "swap",
            "bench_in": bench_in.player_name,
            "bench_in_points": bench_in.projected_points,
            "bench_out": bench_out.player_name,
            "bench_out_points": bench_out.projected_points,
            "slot": in_slot,
            "out_slot": out_slot,
            "gain": round(gain, 1),
            "reason": reason,
        })
    optimal_lineup_out = [
        {"slot": key, "player": a.player_name, "position": a.position,
         "projected_points": a.projected_points}
        for (key, _), a in zip(seats, best, strict=True) if a is not None
    ]

    # Grade the lineup, not the data. This used to average `confidence`, which
    # scores how much we know about the starters — a roster of well-documented
    # mediocrities graded an A. What a lineup grade should answer is "did you
    # start your best available players", so it is the share of the points you
    # could have had, the same thing Sleeper's own best-manager metric measures.
    if all_starter_analyses:
        avg_confidence = sum(a.confidence for a in all_starter_analyses) / len(all_starter_analyses)
        # A starter the slot cannot legally hold can push the set lineup past
        # the best legal one; that is not better than optimal.
        best_possible = max(optimal_total, total_projected)
        efficiency = (total_projected / best_possible * 100) if best_possible > 0 else 100.0

        if efficiency >= 99:
            grade = "A"
        elif efficiency >= 95:
            grade = "B"
        elif efficiency >= 90:
            grade = "C"
        elif efficiency >= 82:
            grade = "D"
        else:
            grade = "F"
    else:
        grade = "N/A"
        avg_confidence = 0
        efficiency = 0.0

    return create_success_response({
        "starters": starters_analysis,
        "bench": [a.to_dict() for a in bench_analysis],
        "suggested_changes": suggested_changes[:5],  # Top 5 changes
        "optimal_lineup": optimal_lineup_out,
        "optimal_projected": round(optimal_total, 1),
        # Whose game has started: fixed where they are, never part of a
        # suggested change.
        "locked_players": locked_players,
        "weak_spots": weak_spots,
        "lineup_grade": grade,
        # Share of the points available from starters+bench that you actually
        # started. This is what the grade is based on.
        "lineup_efficiency_pct": round(efficiency, 1),
        "average_confidence": round(avg_confidence, 1),
        "total_projected": round(total_projected, 1),
        "total_starters": len(all_starter_analyses),
        "week": week,
        "season": season,
        "week_inferred": week_inferred,
        "scoring_source": scoring_source,
        "scoring_used": scoring_used(scoring),
        "scoring": scoring,
        "message": (f"Lineup Grade: {grade} | {efficiency:.0f}% of available points started "
                    f"| {len(suggested_changes)} change(s) suggested"
                    + (f" | {len(locked_players)} locked (game started)" if locked_players else ""))
    })
