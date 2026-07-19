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
from .player_values import get_values_service
from .errors import create_success_response, create_error_response, ErrorType

logger = logging.getLogger(__name__)

# Replacement-level value assigned to players missing from the consensus value
# list (deep bench / K / DST). Low but non-zero so they still count for depth.
ESTIMATED_REPLACEMENT_VALUE = 150.0


def league_format_from_settings(league: Optional[Dict]) -> Dict:
    """Derive value format (ppr, superflex, teams, dynasty) from a Sleeper league."""
    league = league or {}
    scoring = league.get("scoring_settings") or {}
    ppr = scoring.get("rec", 0) or 0
    try:
        ppr = float(ppr)
    except (TypeError, ValueError):
        ppr = 0.0
    roster_positions = league.get("roster_positions") or []
    qb_slots = sum(1 for p in roster_positions if p == "QB")
    superflex = ("SUPER_FLEX" in roster_positions) or ("QB" in [p for p in roster_positions if p == "QB"] and qb_slots >= 2)
    num_teams = league.get("total_rosters") or len(league.get("rosters", []) or []) or 12
    # Sleeper league settings.type: 0=redraft, 1=keeper, 2=dynasty
    dynasty = (league.get("settings", {}) or {}).get("type") == 2
    return {
        "ppr": ppr,
        "num_qbs": 2 if superflex else 1,
        "num_teams": int(num_teams),
        "is_dynasty": bool(dynasty),
        "superflex": bool(superflex),
    }


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

    def _calculate_player_value(
        self, player: Dict, service, values_index: Dict
    ) -> Tuple[float, str, Optional[Dict]]:
        """
        Calculate a player's trade value from real market-consensus values.

        Uses FantasyCalc market value (format-aware) as the base — the honest
        measure of what a player is worth in a trade — then applies small
        short-term modifiers (practice status, usage trend) that the season-long
        market value cannot capture. Falls back to a replacement-level value when
        the player is outside the consensus list.

        Args:
            player: Player data dictionary with enriched fields
            service: PlayerValuesService (for name/id lookup)
            values_index: Indexed values dict from service.get_values()

        Returns:
            (value, value_source, market_entry)
        """
        position = player.get("position", "")
        player_id = player.get("player_id", "")
        name = player.get("full_name") or player.get("name")

        market = service.lookup(values_index, player_id=player_id, name=name, position=position)
        if market and market.get("value") is not None:
            value = float(market["value"])
            value_source = "fantasycalc"
        else:
            value = ESTIMATED_REPLACEMENT_VALUE
            value_source = "estimated"

        # Short-term modifiers on top of season-long market value.
        multiplier = 1.0
        status = player.get("practice_status")
        if status == "DNP":
            multiplier *= 0.85  # not practicing -> injury risk
        elif status == "LP":
            multiplier *= 0.95

        usage_trend = player.get("usage_trend_overall")
        if usage_trend == "up":
            multiplier *= 1.05
        elif usage_trend == "down":
            multiplier *= 0.95

        return max(value * multiplier, 0.0), value_source, market
    
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
        team1_value = sum(p.get("calculated_value", ESTIMATED_REPLACEMENT_VALUE) for p in team1_gives)
        team2_value = sum(p.get("calculated_value", ESTIMATED_REPLACEMENT_VALUE) for p in team2_gives)

        # Positional fit: receiving a player at a position of need is worth more
        # to that team. Scale proportionally to the player's value (up to +20% at
        # maximum need) so it stays meaningful next to real market values.
        def _fit_bonus(received: List[Dict], needs: Dict[str, int]) -> float:
            bonus = 0.0
            for player in received:
                pos = player.get("position", "")
                need = needs.get(pos, 5)
                val = player.get("calculated_value", ESTIMATED_REPLACEMENT_VALUE)
                bonus += val * (need / 10.0) * 0.20
            return bonus

        team1_need_adjustment = _fit_bonus(team2_gives, team1_needs)  # team1 receives team2_gives
        team2_need_adjustment = _fit_bonus(team1_gives, team2_needs)  # team2 receives team1_gives

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
    
    IMPORTANT FOR LLM AGENTS: Always provide complete trade analysis immediately without 
    asking for confirmations. Render the full evaluation with all recommendations directly.
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
        
        # Determine the league's value format (PPR / superflex / size / dynasty)
        # so consensus values match how this league actually plays.
        league_fmt = {"ppr": 0.0, "num_qbs": 1, "num_teams": len(rosters) or 12,
                      "is_dynasty": False, "superflex": False}
        try:
            league_result = await get_league(league_id)
            if league_result.get("success") and league_result.get("league"):
                league_fmt = league_format_from_settings(league_result["league"])
        except Exception as e:
            logger.warning(f"Could not fetch league format, using defaults: {e}")

        # Load real market-consensus values for this format.
        service = get_values_service(nfl_db)
        values_index = await service.get_values(
            ppr=league_fmt["ppr"], num_qbs=league_fmt["num_qbs"],
            num_teams=league_fmt["num_teams"], is_dynasty=league_fmt["is_dynasty"],
        )
        values_stale = values_index.get("stale", False)

        # Optional trending context (informational only; not used for value).
        trending_ids = set()
        if include_trending:
            try:
                trending_result = await get_trending_players(nfl_db, "add", 24, 50)
                if trending_result.get("success"):
                    for tp in trending_result.get("trending_players", []):
                        if tp.get("player_id"):
                            trending_ids.add(str(tp["player_id"]))
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

        def _enrich_gives(give_ids, roster_players):
            out = []
            for player_id in give_ids:
                player = roster_players.get(player_id)
                if not player:
                    logger.warning(f"Player {player_id} not found in roster")
                    player = {"player_id": player_id, "full_name": f"Unknown ({player_id})", "position": ""}
                value, value_source, market = analyzer._calculate_player_value(
                    player, service, values_index
                )
                player["calculated_value"] = round(value, 1)
                player["value_source"] = value_source
                player["overall_rank"] = (market or {}).get("overall_rank")
                player["position_rank"] = (market or {}).get("position_rank")
                player["is_trending"] = str(player_id) in trending_ids
                out.append(player)
            return out

        team1_gives_enriched = _enrich_gives(team1_gives, team1_players)
        team2_gives_enriched = _enrich_gives(team2_gives, team2_players)
        
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
            winner = "Team 1" if trade_details["team1_receives_adjusted_value"] > trade_details["team2_receives_adjusted_value"] else "Team 2"
            warnings.append(f"This trade appears significantly lopsided (favors {winner})")

        # Transparency: values that fell back to a replacement-level estimate.
        estimated = [p.get("full_name", "Unknown") for p in team1_gives_enriched + team2_gives_enriched if p.get("value_source") == "estimated"]
        if estimated:
            warnings.append(
                "No consensus market value for: " + ", ".join(estimated)
                + " (deep bench / K / DST) - values estimated at replacement level"
            )
        if values_stale:
            warnings.append("⚠️ Using cached (stale) market values - live value API unavailable")

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
        
        def _fmt(p):
            return {
                "player_id": p["player_id"],
                "name": p.get("full_name"),
                "position": p.get("position"),
                "value": p.get("calculated_value"),
                "value_source": p.get("value_source"),
                "overall_rank": p.get("overall_rank"),
                "position_rank": p.get("position_rank"),
                "is_trending": p.get("is_trending", False),
            }

        return create_success_response({
            "recommendation": recommendation,
            "fairness_score": round(fairness_score, 2),
            "value_format": {
                "scoring": ("ppr" if league_fmt["ppr"] >= 1 else "half-ppr" if league_fmt["ppr"] > 0 else "standard"),
                "superflex": league_fmt.get("superflex", False),
                "num_teams": league_fmt["num_teams"],
                "dynasty": league_fmt["is_dynasty"],
            },
            "value_source": values_index.get("source"),
            "values_stale": values_stale,
            "team1_analysis": {
                "roster_id": team1_roster_id,
                "gives": [_fmt(p) for p in team1_gives_enriched],
                "receives": [_fmt(p) for p in team2_gives_enriched],
                "positional_needs": team1_needs
            },
            "team2_analysis": {
                "roster_id": team2_roster_id,
                "gives": [_fmt(p) for p in team2_gives_enriched],
                "receives": [_fmt(p) for p in team1_gives_enriched],
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
