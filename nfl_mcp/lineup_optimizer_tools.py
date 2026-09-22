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
from enum import Enum

from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .player_values import scoring_to_ppr
from .projections import _RECEPTION_SHARE

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
}
_GOOD_GAME_DEFAULT = (10, 16)

# A suggested swap has to beat the projection's own error to be worth making.
# The backtest puts weekly MAE at ~5.8 points; below a couple of points a
# "better" player is indistinguishable from the one already in the slot.
MEANINGFUL_SWAP_GAIN = 2.0


def _good_game_thresholds(position: str, ppr: float = 1.0) -> tuple[float, float]:
    """(adequate, good) point marks for a position in a league's scoring."""
    low, high = _GOOD_GAME_PPR.get((position or "").upper(), _GOOD_GAME_DEFAULT)
    scale = 1.0 - (1.0 - ppr) * _RECEPTION_SHARE.get((position or "").upper(), 0.0)
    return low * scale, high * scale


# Which positions may legally fill a slot. Mirrors `win_probability._eligible`,
# which the win-probability optimizer already enforces.
SLOT_ELIGIBILITY = {
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "WRT": frozenset({"RB", "WR", "TE"}),
    "SUPERFLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "DST": frozenset({"DST", "DEF"}),
    "DEF": frozenset({"DST", "DEF"}),
}


def slot_accepts(slot: str, position: str) -> bool:
    """True when `position` may be started in `slot`.

    The swap suggester used to treat a weak FLEX as matching *any* bench player,
    so it would offer a quarterback, a kicker or a defense for a flex spot —
    none of which can legally fill one. An unknown slot falls back to an exact
    position match rather than to "anything goes".
    """
    slot = (slot or "").upper()
    position = (position or "").upper()
    allowed = SLOT_ELIGIBILITY.get(slot)
    return position in allowed if allowed else slot == position


class StartSitDecision(Enum):
    """Enum for start/sit recommendation types."""
    MUST_START = "must_start"
    START = "start"
    FLEX = "flex"
    SIT = "sit"
    MUST_SIT = "must_sit"


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
    practice_status: str = "full"
    # Where the status came from: "caller" when passed in, otherwise the
    # injury tables ("report", "sleeper" or "both"), None when nothing is known.
    injury_source: str | None = None

    # Projection
    projected_points: float = 0.0
    floor: float = 0.0
    ceiling: float = 0.0
    # Which baseline produced it. `rank_bucket` means a static per-position
    # placeholder, not a read on this player — worth surfacing rather than
    # letting a generic number pass for a projection.
    base_source: str | None = None

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
            "injury_source": self.injury_source,
            "projected_points": self.projected_points,
            "floor": self.floor,
            "ceiling": self.ceiling,
            "base_source": self.base_source,
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
}

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
            if not scoring:
                return str(_scoring_ppr(league)), num_teams, "league"
    if scoring:
        return scoring, num_teams, "caller"
    return "ppr", num_teams, "default"


async def _resolve_season_week(
    season: int | None, week: int | None
) -> tuple[int | None, int | None, bool]:
    """Fill in season/week from NFL state when the caller omitted them.

    Omitting them is the common case — an agent rarely knows the current week —
    and it silently downgraded every projection to the positional-rank baseline:
    six static values per position, so a workhorse RB came out at 16.8 instead
    of 31.1 for the same week. All differentiation then came from the matchup
    tier, which the engine's own backtest rates at zero for WRs.

    Returns ``(season, week, inferred)`` so callers can report which values were
    used rather than leaving it to be guessed from the numbers.
    """
    if season is not None and week is not None:
        return season, week, False
    try:
        from .nfl_tools import get_current_season_and_week
        got_season, got_week = await get_current_season_and_week()
    except Exception as e:
        logger.debug(f"season/week inference failed: {e}")
        return season, week, False
    resolved_season = season if season is not None else got_season
    resolved_week = week if week is not None else got_week
    return resolved_season, resolved_week, True


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
                from .database import NFLDatabase
                self.db = NFLDatabase()
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

        if matchup_score >= 75:
            reasoning.append(f"✅ Favorable matchup (#{analysis.matchup_rank} vs {analysis.position})")
        elif matchup_score <= 30:
            reasoning.append(f"⚠️ Tough matchup (#{analysis.matchup_rank} vs {analysis.position})")

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
        injury_score = INJURY_STATUS_SCORES.get(
            analysis.injury_status.lower(),
            100 if analysis.injury_status.lower() == "healthy" else 50
        )
        practice_score = PRACTICE_STATUS_SCORES.get(
            analysis.practice_status.lower(),
            100 if analysis.practice_status.lower() == "full" else 70
        )
        health_score = (injury_score * 0.6 + practice_score * 0.4)
        scores["health"] = health_score

        if injury_score < 60:
            reasoning.append(f"⚠️ Injury concern: {analysis.injury_status}")
        if practice_score < 70:
            reasoning.append(f"⚠️ Limited practice: {analysis.practice_status}")
        if health_score >= 90:
            reasoning.append("✅ Healthy, full practice")

        # 4. Projection score
        projection_score = 50  # Default
        if analysis.projected_points > 0:
            # Scale based on position expectations, rebased to this league's
            # scoring — these are full-PPR "good game" marks, and comparing a
            # half-PPR projection against them demoted every pass catcher.
            floor_thresh, ceil_thresh = _good_game_thresholds(analysis.position, ppr)

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
    ) -> str:
        """Start/sit decision from expected points, not from how much we know.

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
        if health_score <= 25:
            return StartSitDecision.MUST_SIT.value

        adequate, good = _good_game_thresholds(position, ppr)
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
            position: Player position (QB, RB, WR, TE)
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
            analysis.practice_status = injury_data.get("practice_status") or "full"

        # Apply projection data — or auto-project when the caller didn't supply
        # points, so start/sit works without manual point entry.
        if projection_data and projection_data.get("projected_points"):
            analysis.projected_points = projection_data.get("projected_points", 0.0)
            analysis.floor = projection_data.get("floor", 0.0)
            analysis.ceiling = projection_data.get("ceiling", 0.0)
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
            except Exception as e:
                logger.debug(f"Auto-projection failed for {player_name}: {e}")

        # Calculate confidence and decision
        confidence, confidence_level, reasoning = self.calculate_confidence(
            analysis, ppr=scoring_to_ppr(scoring)
        )

        analysis.confidence = round(confidence, 1)
        analysis.confidence_level = confidence_level
        analysis.reasoning = reasoning

        # Determine decision
        health_score = INJURY_STATUS_SCORES.get(
            analysis.injury_status.lower(), 100
        )
        analysis.decision = self.determine_decision(
            analysis.projected_points,
            analysis.position,
            health_score,
            ppr=scoring_to_ppr(scoring),
            confidence=analysis.confidence,
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

        # Sort each position by confidence (highest first)
        for position in analyses_by_position:
            analyses_by_position[position].sort(
                key=lambda x: x.confidence,
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
    opponent: str,
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
        position: Fantasy position (QB, RB, WR, TE)
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
        "factors": {
            "matchup": f"#{analysis.matchup_rank} ({analysis.matchup_tier})",
            "usage": f"Snaps: {analysis.snap_percentage}%, Targets: {analysis.target_share}%",
            "health": f"{analysis.injury_status}, Practice: {analysis.practice_status}",
            "projection": f"{analysis.projected_points} pts" if analysis.projected_points > 0 else "N/A",
        },
        "message": f"{analysis.player_name}: {decision_display} (Confidence: {analysis.confidence:.0f}%)"
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
            - position (str): QB, RB, WR, TE
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
        - recommendations: List of all player recommendations sorted by confidence
        - by_position: Recommendations grouped by position
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

    # Sort all by confidence
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

    return create_success_response({
        "recommendations": all_recommendations,
        "by_position": by_position,
        "must_starts": must_starts,
        "sits": sits,
        "summary": summary_lines,
        "total_analyzed": len(all_recommendations),
        "week": week,
        "season": season,
        "week_inferred": week_inferred,
        "scoring_source": scoring_source,
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

    if len(players) > 5:
        players = players[:5]  # Limit to 5 players

    optimizer = get_lineup_optimizer()
    season, week, week_inferred = await _resolve_season_week(season, week)
    scoring, num_teams, scoring_source = await _league_scoring(league_id, scoring)

    # Analyze all players
    analyses = []
    for player in players:
        analysis = await optimizer.analyze_player(
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
        analyses.append(analysis)

    # Points decide the slot, confidence only breaks a tie.
    analyses.sort(key=lambda x: (x.projected_points, x.confidence), reverse=True)

    # Get winner and runner up
    winner = analyses[0]
    runner_up = analyses[1] if len(analyses) > 1 else None

    confidence_gap = winner.confidence - runner_up.confidence if runner_up else 100
    # The gap that matters for a slot decision is in points, not in how much we
    # know. Compared against the projection's own error (MAE ~5.8), so "clear"
    # means clear relative to what the model can actually resolve.
    points_gap = round(winner.projected_points - runner_up.projected_points, 1) if runner_up else 0.0

    # Verdict scaled to the projection's own error. The backtest puts weekly
    # MAE at ~5.8 points, so a 1-point edge is not an edge — calling it one is
    # the false precision this tool used to trade in.
    if not runner_up:
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
            "reasoning": winner.reasoning,
        },
        "comparison": comparison_list,
        "points_gap": points_gap,
        "scoring": scoring,
        "season": season,
        "week": week,
        "week_inferred": week_inferred,
        "scoring_source": scoring_source,
        "confidence_gap": round(confidence_gap, 1),
        "verdict": verdict,
        "total_compared": len(analyses),
        "message": (f"For {slot}: Start {winner.player_name} "
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
) -> dict:
    """
    Analyze a complete fantasy lineup with optimal lineup suggestions.

    Takes a full lineup organized by position and provides:
    - Analysis of each starter
    - Identification of weak spots
    - Bench players who should start
    - Overall lineup grade

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        lineup: Dict with position keys containing player lists
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
        - suggested_changes: List of recommended lineup changes
        - lineup_grade: Overall grade (A-F)
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

    starter_positions = ["QB", "RB", "WR", "TE", "FLEX", "K", "DST"]
    bench_key = "BENCH"

    starters_analysis = {}
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
            total_projected += analysis.projected_points

            # Track weak spots. A slot is weak when it projects poorly for the
            # position, not when we happen to know little about the player —
            # a well-documented 3-point starter is the weak spot, a thinly
            # covered 18-point starter is not.
            adequate, _ = _good_game_thresholds(analysis.position, scoring_to_ppr(scoring))
            if analysis.projected_points < adequate * 0.7:
                weak_spots.append({
                    "position": position,
                    "player": analysis.player_name,
                    "projected_points": analysis.projected_points,
                    "confidence": analysis.confidence,
                    "issue": (f"projects {analysis.projected_points} "
                              f"(adequate for {analysis.position} is {adequate:.1f})"),
                })

        starters_analysis[position] = [a.to_dict() for a in position_analyses]

    # Analyze bench
    if lineup.get(bench_key):
        for player in lineup[bench_key]:
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

            # Check if a bench player should start over a starter. Compared in
            # points, not confidence: a bench player can be better known and
            # still project fewer points, and swapping on that basis lowers the
            # lineup total. The margin is wide enough to sit outside noise.
            for weak in weak_spots:
                slot = weak.get("slot_position", weak["position"])
                if slot_accepts(slot, analysis.position):
                    gain = analysis.projected_points - weak.get("projected_points", 0.0)
                    if gain >= MEANINGFUL_SWAP_GAIN:
                        suggested_changes.append({
                            "action": "swap",
                            "bench_in": analysis.player_name,
                            "bench_in_points": analysis.projected_points,
                            "bench_out": weak["player"],
                            "bench_out_points": weak.get("projected_points", 0.0),
                            "gain": round(gain, 1),
                            "reason": (f"{analysis.player_name} projects "
                                       f"{round(gain, 1)} more points"),
                        })

    # Grade the lineup, not the data. This used to average `confidence`, which
    # scores how much we know about the starters — a roster of well-documented
    # mediocrities graded an A. What a lineup grade should answer is "did you
    # start your best available players", so it is the share of the points you
    # could have had, the same thing Sleeper's own best-manager metric measures.
    if all_starter_analyses:
        avg_confidence = sum(a.confidence for a in all_starter_analyses) / len(all_starter_analyses)
        best_possible = total_projected + sum(
            max(0.0, c["gain"]) for c in suggested_changes
        )
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
        "scoring": scoring,
        "message": (f"Lineup Grade: {grade} | {efficiency:.0f}% of available points started "
                    f"| {len(suggested_changes)} change(s) suggested")
    })
