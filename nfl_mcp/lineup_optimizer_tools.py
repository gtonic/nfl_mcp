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
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, UTC
from dataclasses import dataclass, field
from enum import Enum

from .config import create_http_client, LONG_TIMEOUT
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors
)

logger = logging.getLogger(__name__)


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
    
    # Projection
    projected_points: float = 0.0
    floor: float = 0.0
    ceiling: float = 0.0
    
    # Analysis results
    decision: str = "start"
    confidence: float = 50.0
    confidence_level: str = "medium"
    reasoning: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
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
            "projected_points": self.projected_points,
            "floor": self.floor,
            "ceiling": self.ceiling,
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
    "questionable": 60,
    "doubtful": 25,
    "out": 0,
    "ir": 0,
    "suspended": 0,
    "pup": 0,
}

# Practice status scores
PRACTICE_STATUS_SCORES = {
    "full": 100,
    "full participation": 100,
    "limited": 70,
    "limited participation": 70,
    "dnp": 30,
    "did not participate": 30,
    "rest": 85,  # Veteran rest day is usually fine
}

# Usage trend scores
USAGE_TREND_SCORES = {
    "upward": 85,
    "stable": 60,
    "downward": 35,
}


class LineupOptimizer:
    """
    Lineup optimizer that combines multiple factors for start/sit decisions.
    
    Uses a weighted scoring system to calculate confidence in recommendations.
    """
    
    def __init__(self, db=None, defense_analyzer=None):
        """Initialize the optimizer with optional database and analyzer."""
        self.db = db
        self.defense_analyzer = defense_analyzer
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
    
    def calculate_confidence(self, analysis: PlayerAnalysis) -> Tuple[float, str, List[str]]:
        """
        Calculate confidence score and generate reasoning.
        
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
            # Scale based on position expectations
            position_thresholds = {
                "QB": (18, 25),  # Floor, ceiling for "good" game
                "RB": (12, 18),
                "WR": (10, 16),
                "TE": (8, 14),
                "K": (7, 10),
                "DST": (6, 10),
            }
            floor_thresh, ceil_thresh = position_thresholds.get(
                analysis.position, (10, 16)
            )
            
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
            reasoning.append(f"📈 Usage trending up")
        elif trend_score <= 40:
            reasoning.append(f"📉 Usage trending down")
        
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
        confidence: float,
        matchup_tier: str,
        health_score: float
    ) -> str:
        """
        Determine start/sit decision based on confidence and factors.
        
        Returns:
            Decision string: must_start, start, flex, sit, must_sit
        """
        # Auto-sit injured players
        if health_score <= 25:
            return StartSitDecision.MUST_SIT.value
        
        # High confidence with good matchup = must start
        if confidence >= 80 and matchup_tier in ["smash", "favorable"]:
            return StartSitDecision.MUST_START.value
        
        # High confidence = start
        if confidence >= 70:
            return StartSitDecision.START.value
        
        # Medium confidence = flex consideration
        if confidence >= 50:
            return StartSitDecision.FLEX.value
        
        # Low confidence with bad matchup = must sit
        if confidence <= 35 and matchup_tier in ["elite", "tough"]:
            return StartSitDecision.MUST_SIT.value
        
        # Low confidence = sit
        if confidence < 45:
            return StartSitDecision.SIT.value
        
        return StartSitDecision.FLEX.value
    
    async def analyze_player(
        self,
        player_name: str,
        player_id: str,
        position: str,
        team: str,
        opponent: str,
        usage_data: Optional[Dict] = None,
        injury_data: Optional[Dict] = None,
        projection_data: Optional[Dict] = None,
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
        
        # Apply injury data
        if injury_data:
            analysis.injury_status = injury_data.get("status", "healthy")
            analysis.practice_status = injury_data.get("practice_status", "full")
        
        # Apply projection data
        if projection_data:
            analysis.projected_points = projection_data.get("projected_points", 0.0)
            analysis.floor = projection_data.get("floor", 0.0)
            analysis.ceiling = projection_data.get("ceiling", 0.0)
        
        # Calculate confidence and decision
        confidence, confidence_level, reasoning = self.calculate_confidence(analysis)
        
        analysis.confidence = round(confidence, 1)
        analysis.confidence_level = confidence_level
        analysis.reasoning = reasoning
        
        # Determine decision
        health_score = INJURY_STATUS_SCORES.get(
            analysis.injury_status.lower(), 100
        )
        analysis.decision = self.determine_decision(
            confidence, 
            analysis.matchup_tier, 
            health_score
        )
        
        return analysis
    
    async def analyze_roster(
        self,
        players: List[Dict],
        week: Optional[int] = None
    ) -> Dict[str, List[PlayerAnalysis]]:
        """
        Analyze a full roster and return sorted recommendations by position.
        
        Args:
            players: List of player dicts with name, position, team, opponent
            week: Optional NFL week number
            
        Returns:
            Dict mapping position to sorted list of player analyses
        """
        analyses_by_position: Dict[str, List[PlayerAnalysis]] = {}
        
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
_lineup_optimizer: Optional[LineupOptimizer] = None


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
    player_id: Optional[str] = None,
    target_share: Optional[float] = None,
    snap_percentage: Optional[float] = None,
    injury_status: Optional[str] = None,
    practice_status: Optional[str] = None,
    projected_points: Optional[float] = None,
) -> Dict:
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
        
    Returns:
        Dictionary containing:
        - recommendation: Start/sit decision details
        - confidence: Confidence score (0-100)
        - confidence_level: high/medium/low
        - reasoning: List of factors in the decision
        - matchup_tier: Matchup difficulty tier
        
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
        },
        "confidence": analysis.confidence,
        "confidence_level": analysis.confidence_level,
        "matchup_tier": analysis.matchup_tier,
        "matchup_rank": analysis.matchup_rank,
        "reasoning": analysis.reasoning,
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
    players: List[Dict],
    week: Optional[int] = None,
    include_reasoning: bool = True
) -> Dict:
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
        week: Optional NFL week number
        include_reasoning: Whether to include detailed reasoning (default: True)
        
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
    analyses_by_position = await optimizer.analyze_roster(players, week)
    
    # Flatten and convert to dicts
    all_recommendations = []
    must_starts = []
    sits = []
    
    for position, analyses in analyses_by_position.items():
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
    all_recommendations.sort(key=lambda x: x["confidence"], reverse=True)
    
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
        "message": f"Analyzed {len(all_recommendations)} players"
    })


@handle_http_errors(
    default_data={"comparison": None},
    operation_name="comparing players"
)
async def compare_players_for_slot(
    players: List[Dict],
    slot: str = "FLEX"
) -> Dict:
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
        )
        analyses.append(analysis)
    
    # Sort by confidence
    analyses.sort(key=lambda x: x.confidence, reverse=True)
    
    # Get winner and runner up
    winner = analyses[0]
    runner_up = analyses[1] if len(analyses) > 1 else None
    
    confidence_gap = winner.confidence - runner_up.confidence if runner_up else 100
    
    # Generate verdict
    if confidence_gap >= 20:
        verdict = f"Clear choice: {winner.player_name} is significantly better this week"
    elif confidence_gap >= 10:
        verdict = f"Edge to {winner.player_name}, but {runner_up.player_name} is a reasonable alternative"
    else:
        verdict = f"Coin flip between {winner.player_name} and {runner_up.player_name}"
    
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
            "confidence": winner.confidence,
            "decision": winner.decision,
            "reasoning": winner.reasoning,
        },
        "comparison": comparison_list,
        "confidence_gap": round(confidence_gap, 1),
        "verdict": verdict,
        "total_compared": len(analyses),
        "message": f"For {slot}: Start {winner.player_name} ({winner.confidence:.0f}% confidence)"
    })


@handle_http_errors(
    default_data={"analysis": None},
    operation_name="analyzing lineup"
)
async def analyze_full_lineup(
    lineup: Dict[str, List[Dict]],
    week: Optional[int] = None
) -> Dict:
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
        week: Optional NFL week number
        
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
            # Determine actual position (FLEX might have RB/WR/TE)
            actual_position = player.get("position", position)
            if position == "FLEX" and actual_position not in ["RB", "WR", "TE"]:
                actual_position = "WR"  # Default assumption
            
            analysis = await optimizer.analyze_player(
                player_name=player.get("name", "Unknown"),
                player_id=player.get("player_id", ""),
                position=actual_position,
                team=player.get("team", ""),
                opponent=player.get("opponent", ""),
                usage_data=player.get("usage"),
                injury_data=player.get("injury"),
                projection_data=player.get("projection"),
            )
            position_analyses.append(analysis)
            all_starter_analyses.append(analysis)
            total_projected += analysis.projected_points
            
            # Track weak spots
            if analysis.confidence < 45:
                weak_spots.append({
                    "position": position,
                    "player": analysis.player_name,
                    "confidence": analysis.confidence,
                    "issue": analysis.reasoning[0] if analysis.reasoning else "Low overall confidence"
                })
        
        starters_analysis[position] = [a.to_dict() for a in position_analyses]
    
    # Analyze bench
    if bench_key in lineup and lineup[bench_key]:
        for player in lineup[bench_key]:
            actual_position = player.get("position", "WR")
            
            analysis = await optimizer.analyze_player(
                player_name=player.get("name", "Unknown"),
                player_id=player.get("player_id", ""),
                position=actual_position,
                team=player.get("team", ""),
                opponent=player.get("opponent", ""),
                usage_data=player.get("usage"),
                injury_data=player.get("injury"),
                projection_data=player.get("projection"),
            )
            bench_analysis.append(analysis)
            
            # Check if bench player should start over a starter
            if analysis.decision in ["must_start", "start"]:
                for weak in weak_spots:
                    if analysis.position == weak.get("slot_position", weak["position"]) or weak["position"] == "FLEX":
                        if analysis.confidence > weak["confidence"] + 10:
                            suggested_changes.append({
                                "action": "swap",
                                "bench_in": analysis.player_name,
                                "bench_in_confidence": analysis.confidence,
                                "bench_out": weak["player"],
                                "bench_out_confidence": weak["confidence"],
                                "reason": f"{analysis.player_name} has better matchup/usage"
                            })
    
    # Calculate lineup grade
    if all_starter_analyses:
        avg_confidence = sum(a.confidence for a in all_starter_analyses) / len(all_starter_analyses)
        
        if avg_confidence >= 75:
            grade = "A"
        elif avg_confidence >= 65:
            grade = "B"
        elif avg_confidence >= 55:
            grade = "C"
        elif avg_confidence >= 45:
            grade = "D"
        else:
            grade = "F"
    else:
        grade = "N/A"
        avg_confidence = 0
    
    return create_success_response({
        "starters": starters_analysis,
        "bench": [a.to_dict() for a in bench_analysis],
        "suggested_changes": suggested_changes[:5],  # Top 5 changes
        "weak_spots": weak_spots,
        "lineup_grade": grade,
        "average_confidence": round(avg_confidence, 1),
        "total_projected": round(total_projected, 1),
        "total_starters": len(all_starter_analyses),
        "week": week,
        "message": f"Lineup Grade: {grade} | Avg Confidence: {avg_confidence:.0f}%"
    })
