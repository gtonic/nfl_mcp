"""
Trade analyzer tools for the NFL MCP Server.

This module provides fantasy football trade analysis functionality including
trade fairness evaluation, player value assessment, positional need analysis,
and trade recommendations.
"""

from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import logging

from .sleeper_tools import get_rosters, get_league, get_trending_players
from .athlete_tools import lookup_athlete
from .errors import create_success_response, create_error_response, ErrorType

logger = logging.getLogger(__name__)


class TradeAnalyzer:
    """Analyzer for fantasy football trade evaluation with fairness scoring."""
    
    def __init__(self):
        self.position_tiers = {
            "QB": {"tier1": 5, "tier2": 10, "tier3": 20},
            "RB": {"tier1": 12, "tier2": 24, "tier3": 36},
            "WR": {"tier1": 15, "tier2": 30, "tier3": 45},
            "TE": {"tier1": 5, "tier2": 10, "tier3": 20},
            "K": {"tier1": 5, "tier2": 10, "tier3": 15},
            "DEF": {"tier1": 5, "tier2": 10, "tier3": 15}
        }
    
    def _calculate_player_value(self, player: Dict, nfl_db, trending_data: Optional[Dict] = None) -> float:
        """
        Calculate player value based on available data.
        
        Args:
            player: Player data dictionary with enriched fields
            nfl_db: Database instance for player lookups
            trending_data: Optional trending players data
            
        Returns:
            float: Player value score (0-100)
        """
        value = 50.0  # Base value
        
        position = player.get("position", "")
        player_id = player.get("player_id", "")
        
        # Adjust for position scarcity
        position_multipliers = {
            "QB": 1.0,
            "RB": 1.3,  # RBs more valuable due to scarcity
            "WR": 1.1,
            "TE": 1.2,
            "K": 0.7,
            "DEF": 0.8
        }
        value *= position_multipliers.get(position, 1.0)
        
        # Check trending status
        if trending_data and player_id:
            trending_players = trending_data.get("trending_players", [])
            for trending in trending_players:
                if trending.get("player_id") == player_id:
                    # Trending status means higher value
                    add_count = trending.get("count", 0)
                    value += min(add_count * 2, 20)  # Cap at +20 for trending
                    break
        
        # Check for advanced enrichment data
        if player.get("practice_status"):
            status = player.get("practice_status")
            if status == "DNP":
                value *= 0.7  # Significant penalty for not practicing
            elif status == "LP":
                value *= 0.85  # Moderate penalty
            elif status in ["FP", "Full"]:
                value *= 1.05  # Slight bonus for full practice
        
        # Usage trends
        usage_trend = player.get("usage_trend_overall")
        if usage_trend == "up":
            value *= 1.15
        elif usage_trend == "down":
            value *= 0.85
        
        # Snap percentage consideration
        snap_pct = player.get("snap_pct")
        if snap_pct:
            if snap_pct > 80:
                value *= 1.1
            elif snap_pct < 30:
                value *= 0.8
        
        return min(max(value, 0), 100)  # Clamp between 0-100
    
    def _calculate_positional_needs(self, roster: Dict) -> Dict[str, int]:
        """
        Calculate positional needs based on roster composition.
        
        Args:
            roster: Roster data with players_enriched
            
        Returns:
            Dict mapping position to need score (0-10, higher = more need)
        """
        needs = defaultdict(int)
        
        players = roster.get("players_enriched", [])
        starters = roster.get("starters_enriched", [])
        
        # Count by position
        position_counts = defaultdict(int)
        starter_counts = defaultdict(int)
        
        for player in players:
            pos = player.get("position", "")
            if pos:
                position_counts[pos] += 1
        
        for starter in starters:
            pos = starter.get("position", "")
            if pos:
                starter_counts[pos] += 1
        
        # Calculate needs based on position depth
        for pos in ["QB", "RB", "WR", "TE", "K", "DEF"]:
            total = position_counts.get(pos, 0)
            starters_at_pos = starter_counts.get(pos, 0)
            
            # More need if fewer players at position
            if pos == "QB":
                if total < 2:
                    needs[pos] = 8
                elif total < 3:
                    needs[pos] = 5
                else:
                    needs[pos] = 2
            elif pos in ["RB", "WR"]:
                if total < 3:
                    needs[pos] = 9
                elif total < 4:
                    needs[pos] = 6
                elif total < 5:
                    needs[pos] = 3
                else:
                    needs[pos] = 1
            elif pos == "TE":
                if total < 2:
                    needs[pos] = 7
                elif total < 3:
                    needs[pos] = 4
                else:
                    needs[pos] = 2
            else:
                if total < 1:
                    needs[pos] = 6
                elif total < 2:
                    needs[pos] = 3
                else:
                    needs[pos] = 1
        
        return dict(needs)
    
    def _evaluate_trade_fairness(
        self,
        team1_gives: List[Dict],
        team2_gives: List[Dict],
        team1_needs: Dict[str, int],
        team2_needs: Dict[str, int]
    ) -> Tuple[str, float, Dict]:
        """
        Evaluate trade fairness based on player values and positional needs.
        
        Args:
            team1_gives: List of players team 1 is giving up
            team2_gives: List of players team 2 is giving up
            team1_needs: Team 1's positional needs
            team2_needs: Team 2's positional needs
            
        Returns:
            Tuple of (recommendation, fairness_score, details)
        """
        team1_value = sum(p.get("calculated_value", 50) for p in team1_gives)
        team2_value = sum(p.get("calculated_value", 50) for p in team2_gives)
        
        # Adjust for positional needs
        team1_need_adjustment = 0
        team2_need_adjustment = 0
        
        for player in team2_gives:  # What team 1 receives
            pos = player.get("position", "")
            need = team1_needs.get(pos, 5)
            team1_need_adjustment += need * 2  # Each need point worth 2 value points
        
        for player in team1_gives:  # What team 2 receives
            pos = player.get("position", "")
            need = team2_needs.get(pos, 5)
            team2_need_adjustment += need * 2
        
        adjusted_team1_receives = team2_value + team1_need_adjustment
        adjusted_team2_receives = team1_value + team2_need_adjustment
        
        # Calculate fairness score (0-100, 100 = perfectly fair)
        value_diff = abs(adjusted_team1_receives - adjusted_team2_receives)
        max_value = max(adjusted_team1_receives, adjusted_team2_receives)
        
        if max_value > 0:
            fairness_score = max(0, 100 - (value_diff / max_value * 100))
        else:
            fairness_score = 100
        
        # Determine recommendation
        if fairness_score >= 90:
            recommendation = "fair"
        elif fairness_score >= 75:
            recommendation = "slightly_favors_team_" + ("1" if adjusted_team1_receives > adjusted_team2_receives else "2")
        elif fairness_score >= 60:
            recommendation = "needs_adjustment"
        else:
            recommendation = "unfair"
        
        details = {
            "team1_gives_value": round(team1_value, 2),
            "team2_gives_value": round(team2_value, 2),
            "team1_receives_adjusted_value": round(adjusted_team1_receives, 2),
            "team2_receives_adjusted_value": round(adjusted_team2_receives, 2),
            "team1_need_bonus": round(team1_need_adjustment, 2),
            "team2_need_bonus": round(team2_need_adjustment, 2),
            "value_difference": round(value_diff, 2)
        }
        
        return recommendation, fairness_score, details


async def analyze_trade(
    league_id: str,
    team1_roster_id: int,
    team2_roster_id: int,
    team1_gives: List[str],
    team2_gives: List[str],
    nfl_db=None,
    include_trending: bool = True
) -> Dict:
    """
    Analyze a fantasy football trade for fairness and fit.
    
    This tool evaluates a proposed trade between two teams by:
    - Calculating player values based on stats, projections, and trends
    - Assessing positional needs for both teams
    - Evaluating trade fairness with adjustments for team context
    - Providing actionable recommendations
    
    Args:
        league_id: The unique identifier for the fantasy league
        team1_roster_id: Roster ID for team 1 (giving team1_gives)
        team2_roster_id: Roster ID for team 2 (giving team2_gives)
        team1_gives: List of player IDs that team 1 is giving up
        team2_gives: List of player IDs that team 2 is giving up
        nfl_db: Database instance for player lookups (optional)
        include_trending: Whether to include trending player data (default: True)
        
    Returns:
        A dictionary containing:
        - recommendation: Trade recommendation (fair, needs_adjustment, etc.)
        - fairness_score: Score from 0-100 (100 = perfectly fair)
        - team1_analysis: Analysis for team 1 (gives, receives, needs)
        - team2_analysis: Analysis for team 2 (gives, receives, needs)
        - trade_details: Detailed value breakdown
        - warnings: Any warnings or concerns about the trade
        - success: Whether the analysis was successful
        - error: Error message (if any)
    """
    try:
        # Validate inputs
        if not league_id or not team1_gives or not team2_gives:
            return create_error_response(
                "league_id, team1_gives, and team2_gives are required",
                ErrorType.VALIDATION,
                {"recommendation": None, "fairness_score": 0}
            )
        
        # Fetch league rosters
        rosters_result = await get_rosters(league_id)
        if not rosters_result.get("success"):
            return create_error_response(
                f"Failed to fetch rosters: {rosters_result.get('error')}",
                ErrorType.HTTP,
                {"recommendation": None, "fairness_score": 0}
            )
        
        rosters = rosters_result.get("rosters", [])
        
        # Find the two rosters involved
        team1_roster = None
        team2_roster = None
        
        for roster in rosters:
            if roster.get("roster_id") == team1_roster_id:
                team1_roster = roster
            elif roster.get("roster_id") == team2_roster_id:
                team2_roster = roster
        
        if not team1_roster or not team2_roster:
            return create_error_response(
                "Could not find one or both rosters",
                ErrorType.VALIDATION,
                {"recommendation": None, "fairness_score": 0}
            )
        
        # Get trending data if requested
        trending_data = None
        if include_trending:
            try:
                trending_result = await get_trending_players(nfl_db, "add", 24, 50)
                if trending_result.get("success"):
                    trending_data = trending_result
            except Exception as e:
                logger.warning(f"Could not fetch trending data: {e}")
        
        # Initialize analyzer
        analyzer = TradeAnalyzer()
        
        # Calculate positional needs
        team1_needs = analyzer._calculate_positional_needs(team1_roster)
        team2_needs = analyzer._calculate_positional_needs(team2_roster)
        
        # Enrich and calculate values for players being traded
        team1_gives_enriched = []
        team2_gives_enriched = []
        
        team1_players = {p.get("player_id"): p for p in team1_roster.get("players_enriched", [])}
        team2_players = {p.get("player_id"): p for p in team2_roster.get("players_enriched", [])}
        
        for player_id in team1_gives:
            player = team1_players.get(player_id)
            if not player:
                logger.warning(f"Player {player_id} not found in team1 roster")
                player = {"player_id": player_id, "full_name": f"Unknown ({player_id})", "position": ""}
            
            value = analyzer._calculate_player_value(player, nfl_db, trending_data)
            player["calculated_value"] = value
            team1_gives_enriched.append(player)
        
        for player_id in team2_gives:
            player = team2_players.get(player_id)
            if not player:
                logger.warning(f"Player {player_id} not found in team2 roster")
                player = {"player_id": player_id, "full_name": f"Unknown ({player_id})", "position": ""}
            
            value = analyzer._calculate_player_value(player, nfl_db, trending_data)
            player["calculated_value"] = value
            team2_gives_enriched.append(player)
        
        # Evaluate trade fairness
        recommendation, fairness_score, trade_details = analyzer._evaluate_trade_fairness(
            team1_gives_enriched,
            team2_gives_enriched,
            team1_needs,
            team2_needs
        )
        
        # Generate warnings
        warnings = []
        
        # Check for injured players
        for player in team1_gives_enriched + team2_gives_enriched:
            if player.get("practice_status") == "DNP":
                warnings.append(f"{player.get('full_name', 'Unknown')} has DNP status (injury concern)")
        
        # Check for lopsided trades
        if fairness_score < 60:
            warnings.append("This trade appears significantly lopsided")
        
        # Check if trading away too many at one position
        team1_gives_positions = [p.get("position") for p in team1_gives_enriched]
        team2_gives_positions = [p.get("position") for p in team2_gives_enriched]
        
        for pos in ["QB", "RB", "WR", "TE"]:
            team1_pos_count = team1_gives_positions.count(pos)
            team2_pos_count = team2_gives_positions.count(pos)
            
            if team1_pos_count >= 2 and pos in ["RB", "WR"]:
                warnings.append(f"Team 1 is giving up {team1_pos_count} {pos}s - may create depth issues")
            if team2_pos_count >= 2 and pos in ["RB", "WR"]:
                warnings.append(f"Team 2 is giving up {team2_pos_count} {pos}s - may create depth issues")
        
        return create_success_response({
            "recommendation": recommendation,
            "fairness_score": round(fairness_score, 2),
            "team1_analysis": {
                "roster_id": team1_roster_id,
                "gives": [{"player_id": p["player_id"], "name": p.get("full_name"), "position": p.get("position"), "value": p.get("calculated_value")} for p in team1_gives_enriched],
                "receives": [{"player_id": p["player_id"], "name": p.get("full_name"), "position": p.get("position"), "value": p.get("calculated_value")} for p in team2_gives_enriched],
                "positional_needs": team1_needs
            },
            "team2_analysis": {
                "roster_id": team2_roster_id,
                "gives": [{"player_id": p["player_id"], "name": p.get("full_name"), "position": p.get("position"), "value": p.get("calculated_value")} for p in team2_gives_enriched],
                "receives": [{"player_id": p["player_id"], "name": p.get("full_name"), "position": p.get("position"), "value": p.get("calculated_value")} for p in team1_gives_enriched],
                "positional_needs": team2_needs
            },
            "trade_details": trade_details,
            "warnings": warnings,
            "league_id": league_id
        })
        
    except Exception as e:
        logger.error(f"Error in analyze_trade: {e}", exc_info=True)
        return create_error_response(
            f"Unexpected error analyzing trade: {str(e)}",
            ErrorType.UNEXPECTED,
            {"recommendation": None, "fairness_score": 0}
        )
