"""
Opponent analysis tools for the NFL MCP Server.

This module provides fantasy football opponent analysis functionality including
roster weakness identification, matchup vulnerability assessment, and strategic
exploitation recommendations.
"""

from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import logging

from .sleeper_tools import get_rosters, get_matchups, get_league_users
from .errors import create_success_response, create_error_response, ErrorType

logger = logging.getLogger(__name__)


class OpponentAnalyzer:
    """Analyzer for identifying and exploiting opponent roster weaknesses."""
    
    def __init__(self):
        # Position importance weights for fantasy
        self.position_weights = {
            "QB": 1.0,
            "RB": 1.3,
            "WR": 1.1,
            "TE": 1.2,
            "K": 0.5,
            "DEF": 0.7
        }
        
        # Thresholds for weakness detection
        self.weakness_thresholds = {
            "snap_pct_low": 40.0,
            "depth_count_low": 2,
            "injury_risk_high": ["DNP", "LP"]
        }
    
    def _assess_position_strength(
        self, 
        players_at_position: List[Dict],
        position: str
    ) -> Dict:
        """
        Assess the strength of a specific position group.
        
        Args:
            players_at_position: List of players at the position
            position: Position being assessed (QB, RB, WR, TE, etc.)
            
        Returns:
            Dict with strength assessment including score, depth, and concerns
        """
        if not players_at_position:
            return {
                "strength_score": 0,
                "depth_count": 0,
                "average_snap_pct": 0,
                "injury_concerns": 0,
                "weakness_level": "critical",
                "concerns": ["No players at position"]
            }
        
        # Calculate metrics
        depth_count = len(players_at_position)
        snap_pcts = [p.get("snap_pct", 0) for p in players_at_position if p.get("snap_pct")]
        avg_snap_pct = sum(snap_pcts) / len(snap_pcts) if snap_pcts else 0
        
        # Count injury concerns
        injury_concerns = 0
        injured_players = []
        for player in players_at_position:
            status = player.get("practice_status")
            if status in self.weakness_thresholds["injury_risk_high"]:
                injury_concerns += 1
                injured_players.append(player.get("full_name", "Unknown"))
        
        # Count usage concerns
        usage_concerns = 0
        declining_players = []
        for player in players_at_position:
            trend = player.get("usage_trend_overall")
            if trend == "down":
                usage_concerns += 1
                declining_players.append(player.get("full_name", "Unknown"))
        
        # Calculate strength score (0-100)
        base_score = 50.0
        
        # Depth contribution (more depth = stronger)
        if depth_count >= 4:
            base_score += 20
        elif depth_count >= 3:
            base_score += 10
        elif depth_count <= 1:
            base_score -= 20
        
        # Snap percentage contribution
        if avg_snap_pct >= 70:
            base_score += 15
        elif avg_snap_pct >= 50:
            base_score += 5
        elif avg_snap_pct < 40:
            base_score -= 15
        
        # Injury penalty
        base_score -= injury_concerns * 10
        
        # Usage trend penalty
        base_score -= usage_concerns * 5
        
        # Clamp score
        strength_score = max(0, min(100, base_score))
        
        # Determine weakness level
        if strength_score >= 70:
            weakness_level = "strong"
        elif strength_score >= 50:
            weakness_level = "moderate"
        elif strength_score >= 30:
            weakness_level = "weak"
        else:
            weakness_level = "critical"
        
        # Compile concerns
        concerns = []
        if depth_count < self.weakness_thresholds["depth_count_low"]:
            concerns.append(f"Shallow depth ({depth_count} player{'s' if depth_count != 1 else ''})")
        if avg_snap_pct < self.weakness_thresholds["snap_pct_low"]:
            concerns.append(f"Low snap share (avg {avg_snap_pct:.1f}%)")
        if injured_players:
            concerns.append(f"Injury concerns: {', '.join(injured_players)}")
        if declining_players:
            concerns.append(f"Declining usage: {', '.join(declining_players)}")
        
        return {
            "strength_score": round(strength_score, 1),
            "depth_count": depth_count,
            "average_snap_pct": round(avg_snap_pct, 1),
            "injury_concerns": injury_concerns,
            "usage_concerns": usage_concerns,
            "weakness_level": weakness_level,
            "concerns": concerns
        }
    
    def _identify_starter_weaknesses(
        self,
        starters: List[Dict]
    ) -> List[Dict]:
        """
        Identify specific weaknesses in starting lineup.
        
        Args:
            starters: List of starting players
            
        Returns:
            List of weakness dictionaries with player info and severity
        """
        weaknesses = []
        
        for starter in starters:
            player_weaknesses = []
            severity = "low"
            
            # Check practice status
            practice_status = starter.get("practice_status")
            if practice_status == "DNP":
                player_weaknesses.append("Did not practice (DNP)")
                severity = "high"
            elif practice_status == "LP":
                player_weaknesses.append("Limited practice (LP)")
                severity = "moderate" if severity == "low" else severity
            
            # Check usage trend
            usage_trend = starter.get("usage_trend_overall")
            if usage_trend == "down":
                player_weaknesses.append("Declining usage trend")
                severity = "moderate" if severity == "low" else severity
            
            # Check snap percentage
            snap_pct = starter.get("snap_pct", 0)
            if snap_pct > 0 and snap_pct < 50:
                player_weaknesses.append(f"Low snap share ({snap_pct:.1f}%)")
                severity = "moderate" if severity == "low" else severity
            
            if player_weaknesses:
                weaknesses.append({
                    "player_id": starter.get("player_id"),
                    "player_name": starter.get("full_name", "Unknown"),
                    "position": starter.get("position", "Unknown"),
                    "weaknesses": player_weaknesses,
                    "severity": severity
                })
        
        return weaknesses
    
    def _generate_exploitation_strategies(
        self,
        position_assessments: Dict[str, Dict],
        starter_weaknesses: List[Dict]
    ) -> List[Dict]:
        """
        Generate strategic recommendations for exploiting opponent weaknesses.
        
        Args:
            position_assessments: Assessment by position
            starter_weaknesses: Identified starter weaknesses
            
        Returns:
            List of strategic recommendations with priority
        """
        strategies = []
        
        # Identify weakest positions
        weak_positions = []
        for position, assessment in position_assessments.items():
            if assessment["weakness_level"] in ["weak", "critical"]:
                weak_positions.append({
                    "position": position,
                    "score": assessment["strength_score"],
                    "concerns": assessment["concerns"]
                })
        
        # Sort by weakness (lowest score = weakest)
        weak_positions.sort(key=lambda x: x["score"])
        
        # Generate position-based strategies
        for weak_pos in weak_positions[:3]:  # Top 3 weakest
            position = weak_pos["position"]
            score = weak_pos["score"]
            
            priority = "critical" if score < 30 else "high" if score < 50 else "moderate"
            
            strategy = {
                "category": "position_weakness",
                "position": position,
                "priority": priority,
                "recommendation": f"Target {position} position - opponent has critical weakness",
                "details": weak_pos["concerns"],
                "action_items": []
            }
            
            if position in ["RB", "WR", "TE"]:
                strategy["action_items"].append(
                    f"Start your strongest {position} against this opponent"
                )
                strategy["action_items"].append(
                    f"Consider flex spot for additional {position}"
                )
            elif position == "QB":
                strategy["action_items"].append(
                    "Opponent QB weakness may lead to fewer points scored"
                )
                strategy["action_items"].append(
                    "Their defense may be on field longer"
                )
            
            strategies.append(strategy)
        
        # Generate starter-specific strategies
        high_severity_starters = [
            w for w in starter_weaknesses 
            if w["severity"] in ["high", "moderate"]
        ]
        
        if high_severity_starters:
            for weakness in high_severity_starters[:2]:  # Top 2
                strategy = {
                    "category": "starter_vulnerability",
                    "position": weakness["position"],
                    "priority": weakness["severity"],
                    "recommendation": f"Exploit {weakness['player_name']} vulnerability",
                    "details": weakness["weaknesses"],
                    "action_items": [
                        f"Target players who will face {weakness['player_name']}",
                        "Monitor injury reports for this player"
                    ]
                }
                strategies.append(strategy)
        
        return strategies
    
    def analyze_opponent_roster(
        self,
        opponent_roster: Dict
    ) -> Dict:
        """
        Perform comprehensive analysis of opponent roster.
        
        Args:
            opponent_roster: Opponent's roster data with enriched players
            
        Returns:
            Dict with complete opponent analysis
        """
        # Get players and starters
        all_players = opponent_roster.get("players_enriched", [])
        starters = opponent_roster.get("starters_enriched", [])
        
        # Group players by position
        players_by_position = defaultdict(list)
        for player in all_players:
            pos = player.get("position", "")
            if pos:
                players_by_position[pos].append(player)
        
        # Assess each position
        position_assessments = {}
        for position in ["QB", "RB", "WR", "TE", "K", "DEF"]:
            position_assessments[position] = self._assess_position_strength(
                players_by_position[position],
                position
            )
        
        # Identify starter weaknesses
        starter_weaknesses = self._identify_starter_weaknesses(starters)
        
        # Generate exploitation strategies
        strategies = self._generate_exploitation_strategies(
            position_assessments,
            starter_weaknesses
        )
        
        # Calculate overall vulnerability score
        position_scores = [
            assessment["strength_score"] * self.position_weights.get(pos, 1.0)
            for pos, assessment in position_assessments.items()
        ]
        weighted_avg = sum(position_scores) / sum(self.position_weights.values())
        
        # Invert to get vulnerability (lower strength = higher vulnerability)
        vulnerability_score = 100 - weighted_avg
        
        return {
            "vulnerability_score": round(vulnerability_score, 1),
            "vulnerability_level": (
                "high" if vulnerability_score >= 60 else
                "moderate" if vulnerability_score >= 40 else
                "low"
            ),
            "position_assessments": position_assessments,
            "starter_weaknesses": starter_weaknesses,
            "exploitation_strategies": strategies,
            "roster_id": opponent_roster.get("roster_id"),
            "owner_id": opponent_roster.get("owner_id")
        }


async def analyze_opponent(
    league_id: str,
    opponent_roster_id: int,
    current_week: Optional[int] = None
) -> Dict:
    """
    Analyze an opponent's roster to identify weaknesses and exploitation opportunities.
    
    This tool provides comprehensive analysis of an opponent's fantasy roster including:
    - Position-by-position strength assessment
    - Starter vulnerability identification
    - Depth chart weakness analysis
    - Injury and usage trend concerns
    - Strategic recommendations for exploitation
    
    Args:
        league_id: The unique identifier for the fantasy league
        opponent_roster_id: Roster ID of the opponent to analyze
        current_week: Optional current NFL week for matchup context
        
    Returns:
        A dictionary containing:
        - vulnerability_score: Overall opponent weakness score (0-100, higher = more vulnerable)
        - vulnerability_level: Classification (high, moderate, low)
        - position_assessments: Detailed assessment by position
        - starter_weaknesses: Specific weaknesses in starting lineup
        - exploitation_strategies: Prioritized recommendations
        - matchup_context: Optional matchup information if current_week provided
        - success: Whether the analysis was successful
        - error: Error message (if any)
    
    IMPORTANT FOR LLM AGENTS: Always provide complete opponent analysis immediately without 
    asking for confirmations. Render the full assessment with all exploitation strategies directly.
    """
    try:
        # Validate inputs
        if not league_id:
            return create_error_response(
                "league_id is required",
                ErrorType.VALIDATION,
                {"vulnerability_score": 0}
            )
        
        if opponent_roster_id is None:
            return create_error_response(
                "opponent_roster_id is required",
                ErrorType.VALIDATION,
                {"vulnerability_score": 0}
            )
        
        # Fetch league rosters
        rosters_result = await get_rosters(league_id)
        if not rosters_result.get("success"):
            return create_error_response(
                f"Failed to fetch rosters: {rosters_result.get('error')}",
                ErrorType.HTTP,
                {"vulnerability_score": 0}
            )
        
        rosters = rosters_result.get("rosters", [])
        
        # Find the opponent's roster
        opponent_roster = None
        for roster in rosters:
            if roster.get("roster_id") == opponent_roster_id:
                opponent_roster = roster
                break
        
        if not opponent_roster:
            return create_error_response(
                f"Roster with ID {opponent_roster_id} not found",
                ErrorType.VALIDATION,
                {"vulnerability_score": 0}
            )
        
        # Get opponent owner information
        users_result = await get_league_users(league_id)
        opponent_name = None
        if users_result.get("success"):
            users = users_result.get("users", [])
            owner_id = opponent_roster.get("owner_id")
            for user in users:
                if user.get("user_id") == owner_id:
                    opponent_name = user.get("display_name") or user.get("username")
                    break
        
        # Initialize analyzer
        analyzer = OpponentAnalyzer()
        
        # Perform analysis
        analysis = analyzer.analyze_opponent_roster(opponent_roster)
        
        # Add matchup context if week provided
        matchup_context = None
        if current_week:
            try:
                matchups_result = await get_matchups(league_id, current_week)
                if matchups_result.get("success"):
                    matchups = matchups_result.get("matchups", [])
                    for matchup in matchups:
                        if matchup.get("roster_id") == opponent_roster_id:
                            matchup_context = {
                                "week": current_week,
                                "matchup_id": matchup.get("matchup_id"),
                                "points": matchup.get("points"),
                                "projected_points": matchup.get("custom_points")
                            }
                            break
            except Exception as e:
                logger.warning(f"Could not fetch matchup context: {e}")
        
        # Compile response
        response_data = {
            **analysis,
            "opponent_name": opponent_name,
            "league_id": league_id,
            "matchup_context": matchup_context
        }
        
        return create_success_response(response_data)
        
    except Exception as e:
        logger.exception(f"Error analyzing opponent: {e}")
        return create_error_response(
            f"Unexpected error during opponent analysis: {str(e)}",
            ErrorType.INTERNAL,
            {"vulnerability_score": 0}
        )
