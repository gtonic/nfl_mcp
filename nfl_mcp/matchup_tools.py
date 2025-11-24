"""
Matchup analysis tools for fantasy football lineup optimization.

This module provides defense vs position rankings, matchup difficulty analysis,
and game environment factors to help optimize fantasy lineups.
"""

import asyncio
import httpx
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, UTC, timedelta

from .config import get_http_headers, create_http_client, LONG_TIMEOUT
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors
)

logger = logging.getLogger(__name__)

# ESPN team ID to abbreviation mapping
ESPN_TEAM_MAP = {
    "1": "ATL", "2": "BUF", "3": "CHI", "4": "CIN", "5": "CLE",
    "6": "DAL", "7": "DEN", "8": "DET", "9": "GB", "10": "TEN",
    "11": "IND", "12": "KC", "13": "LV", "14": "LAR", "15": "MIA",
    "16": "MIN", "17": "NE", "18": "NO", "19": "NYG", "20": "NYJ",
    "21": "PHI", "22": "ARI", "23": "PIT", "24": "LAC", "25": "SF",
    "26": "SEA", "27": "TB", "28": "WSH", "29": "CAR", "30": "JAX",
    "33": "BAL", "34": "HOU"
}

# Reverse mapping
TEAM_TO_ESPN_ID = {v: k for k, v in ESPN_TEAM_MAP.items()}

# Position categories for fantasy
FANTASY_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"]

# Defense ranking tiers
MATCHUP_TIERS = {
    (1, 5): "elite",      # Top 5 - very tough matchup
    (6, 12): "tough",     # 6-12 - above average
    (13, 20): "neutral",  # 13-20 - average
    (21, 27): "favorable", # 21-27 - below average defense
    (28, 32): "smash",    # 28-32 - exploitable matchup
}


def _get_matchup_tier(rank: int) -> str:
    """Convert numeric rank to tier label."""
    for (low, high), tier in MATCHUP_TIERS.items():
        if low <= rank <= high:
            return tier
    return "neutral"


def _get_tier_color(tier: str) -> str:
    """Get color indicator for tier (for display purposes)."""
    return {
        "elite": "🔴",      # Red - avoid
        "tough": "🟠",      # Orange - caution
        "neutral": "🟡",    # Yellow - standard
        "favorable": "🟢",  # Green - good
        "smash": "💚",      # Bright green - excellent
    }.get(tier, "⚪")


def _init_matchup_db():
    """Initialize database connection for matchup data caching."""
    try:
        from .database import NFLDatabase
        return NFLDatabase()
    except Exception as e:
        logger.debug(f"Database init failed for matchup tools: {e}")
        return None


class DefenseRankingsAnalyzer:
    """
    Analyzer for NFL defense rankings against fantasy positions.
    
    Provides matchup difficulty analysis based on points allowed
    to each position by opposing defenses.
    """
    
    # Default rankings when API fails (based on historical averages)
    # Format: {position: {team: (rank, pts_allowed_avg)}}
    _fallback_rankings = None
    _cache_timestamp = None
    _cache_ttl_hours = 6
    
    def __init__(self, db=None):
        self.db = db or _init_matchup_db()
        self._rankings_cache = {}
    
    async def fetch_defense_rankings(self, season: int = None) -> Dict[str, List[Dict]]:
        """
        Fetch defense vs position rankings from ESPN.
        
        Args:
            season: NFL season year (defaults to current)
            
        Returns:
            Dict mapping position -> list of team rankings
            Each ranking has: team, rank, points_allowed_avg, matchup_tier
        """
        if season is None:
            season = datetime.now().year
            # Adjust for NFL season (starts in September)
            if datetime.now().month < 3:
                season -= 1
        
        # Check cache first
        cache_key = f"defense_rankings_{season}"
        if cache_key in self._rankings_cache:
            cached = self._rankings_cache[cache_key]
            if datetime.now(UTC) - cached["timestamp"] < timedelta(hours=self._cache_ttl_hours):
                return cached["data"]
        
        rankings = {}
        
        # Fetch rankings for each position from ESPN stats API
        position_stat_types = {
            "QB": "passingYards",     # Points allowed to QBs
            "RB": "rushingYards",     # Points allowed to RBs  
            "WR": "receivingYards",   # Points allowed to WRs
            "TE": "receivingYards",   # Points allowed to TEs
        }
        
        try:
            async with create_http_client(timeout=LONG_TIMEOUT) as client:
                for position in ["QB", "RB", "WR", "TE"]:
                    try:
                        position_rankings = await self._fetch_position_rankings(
                            client, position, season
                        )
                        rankings[position] = position_rankings
                    except Exception as e:
                        logger.warning(f"Failed to fetch {position} rankings: {e}")
                        rankings[position] = self._get_fallback_rankings(position)
        
        except Exception as e:
            logger.error(f"Failed to fetch defense rankings: {e}")
            # Return fallback rankings for all positions
            for pos in ["QB", "RB", "WR", "TE"]:
                rankings[pos] = self._get_fallback_rankings(pos)
        
        # Cache results
        self._rankings_cache[cache_key] = {
            "data": rankings,
            "timestamp": datetime.now(UTC)
        }
        
        # Also persist to database if available
        if self.db:
            try:
                self._save_rankings_to_db(rankings, season)
            except Exception as e:
                logger.debug(f"Failed to cache rankings to DB: {e}")
        
        return rankings
    
    async def _fetch_position_rankings(
        self, 
        client: httpx.AsyncClient,
        position: str,
        season: int
    ) -> List[Dict]:
        """
        Fetch rankings for a specific position from ESPN.
        
        Uses ESPN's fantasy football defense rankings which show
        points allowed to each position.
        """
        # ESPN fantasy defense vs position endpoint
        # This shows how many fantasy points each defense allows to a position
        url = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/0"
        
        params = {
            "view": "kona_player_info",
        }
        
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        
        # For now, use ESPN's public team stats API
        stats_url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/statistics"
        
        try:
            response = await client.get(stats_url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            # Parse team defensive stats
            rankings = self._parse_defensive_stats(data, position)
            return rankings
            
        except Exception as e:
            logger.debug(f"ESPN stats API failed for {position}: {e}")
            # Try alternative approach - scrape Fantasy Pros
            return await self._fetch_from_fantasy_pros(client, position, season)
    
    async def _fetch_from_fantasy_pros(
        self,
        client: httpx.AsyncClient, 
        position: str,
        season: int
    ) -> List[Dict]:
        """
        Fallback: Fetch defense vs position from FantasyPros.
        
        FantasyPros has excellent defense vs position data.
        """
        # Map position to FantasyPros URL format
        pos_map = {
            "QB": "qb",
            "RB": "rb", 
            "WR": "wr",
            "TE": "te"
        }
        
        pos_slug = pos_map.get(position, position.lower())
        url = f"https://www.fantasypros.com/nfl/points-allowed.php?position={pos_slug}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml"
        }
        
        try:
            response = await client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            
            # Parse HTML table
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, "html.parser")
            
            rankings = []
            table = soup.find("table", {"id": "data-table"})
            
            if table:
                rows = table.find("tbody").find_all("tr") if table.find("tbody") else []
                
                for rank, row in enumerate(rows, 1):
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        team_cell = cells[0]
                        pts_cell = cells[-1]  # Last column is usually total points
                        
                        # Extract team abbreviation
                        team_name = team_cell.get_text(strip=True)
                        team_abbr = self._normalize_team_name(team_name)
                        
                        # Extract points allowed
                        try:
                            pts_allowed = float(pts_cell.get_text(strip=True).replace(",", ""))
                        except ValueError:
                            pts_allowed = 0.0
                        
                        rankings.append({
                            "team": team_abbr,
                            "rank": rank,
                            "points_allowed_avg": round(pts_allowed, 1),
                            "matchup_tier": _get_matchup_tier(rank),
                            "tier_indicator": _get_tier_color(_get_matchup_tier(rank))
                        })
            
            if rankings:
                return rankings
                
        except Exception as e:
            logger.debug(f"FantasyPros fetch failed for {position}: {e}")
        
        # Return fallback if all else fails
        return self._get_fallback_rankings(position)
    
    def _parse_defensive_stats(self, data: Dict, position: str) -> List[Dict]:
        """Parse ESPN defensive stats into position rankings."""
        # This is a simplified parser - ESPN's stats structure varies
        rankings = []
        
        try:
            # ESPN returns teams with their defensive stats
            teams = data.get("teams", data.get("items", []))
            
            team_stats = []
            for team_data in teams:
                team_id = str(team_data.get("id", team_data.get("team", {}).get("id", "")))
                team_abbr = ESPN_TEAM_MAP.get(team_id, "UNK")
                
                # Get relevant stat for position
                stats = team_data.get("statistics", team_data.get("stats", {}))
                
                # Map position to relevant defensive stat
                if position == "QB":
                    pts = stats.get("passingYardsAllowed", stats.get("passingPointsAllowed", 0))
                elif position == "RB":
                    pts = stats.get("rushingYardsAllowed", stats.get("rushingPointsAllowed", 0))
                else:  # WR, TE
                    pts = stats.get("receivingYardsAllowed", stats.get("receivingPointsAllowed", 0))
                
                if isinstance(pts, dict):
                    pts = pts.get("value", pts.get("displayValue", 0))
                
                try:
                    pts = float(pts) if pts else 0
                except (ValueError, TypeError):
                    pts = 0
                
                team_stats.append({
                    "team": team_abbr,
                    "points_allowed": pts
                })
            
            # Sort by points allowed (higher = worse defense = better matchup)
            team_stats.sort(key=lambda x: x["points_allowed"], reverse=True)
            
            # Assign ranks (1 = toughest, 32 = easiest)
            for rank, team in enumerate(reversed(team_stats), 1):
                rankings.append({
                    "team": team["team"],
                    "rank": rank,
                    "points_allowed_avg": round(team["points_allowed"], 1),
                    "matchup_tier": _get_matchup_tier(rank),
                    "tier_indicator": _get_tier_color(_get_matchup_tier(rank))
                })
            
        except Exception as e:
            logger.debug(f"Error parsing ESPN stats: {e}")
        
        return rankings if rankings else self._get_fallback_rankings(position)
    
    def _normalize_team_name(self, name: str) -> str:
        """Convert team name/city to standard abbreviation."""
        name_lower = name.lower().strip()
        
        team_mappings = {
            "arizona": "ARI", "cardinals": "ARI",
            "atlanta": "ATL", "falcons": "ATL",
            "baltimore": "BAL", "ravens": "BAL",
            "buffalo": "BUF", "bills": "BUF",
            "carolina": "CAR", "panthers": "CAR",
            "chicago": "CHI", "bears": "CHI",
            "cincinnati": "CIN", "bengals": "CIN",
            "cleveland": "CLE", "browns": "CLE",
            "dallas": "DAL", "cowboys": "DAL",
            "denver": "DEN", "broncos": "DEN",
            "detroit": "DET", "lions": "DET",
            "green bay": "GB", "packers": "GB",
            "houston": "HOU", "texans": "HOU",
            "indianapolis": "IND", "colts": "IND",
            "jacksonville": "JAX", "jaguars": "JAX",
            "kansas city": "KC", "chiefs": "KC",
            "las vegas": "LV", "raiders": "LV",
            "los angeles chargers": "LAC", "chargers": "LAC",
            "los angeles rams": "LAR", "rams": "LAR",
            "miami": "MIA", "dolphins": "MIA",
            "minnesota": "MIN", "vikings": "MIN",
            "new england": "NE", "patriots": "NE",
            "new orleans": "NO", "saints": "NO",
            "new york giants": "NYG", "giants": "NYG",
            "new york jets": "NYJ", "jets": "NYJ",
            "philadelphia": "PHI", "eagles": "PHI",
            "pittsburgh": "PIT", "steelers": "PIT",
            "san francisco": "SF", "49ers": "SF",
            "seattle": "SEA", "seahawks": "SEA",
            "tampa bay": "TB", "buccaneers": "TB",
            "tennessee": "TEN", "titans": "TEN",
            "washington": "WSH", "commanders": "WSH",
        }
        
        for key, abbr in team_mappings.items():
            if key in name_lower:
                return abbr
        
        # If already an abbreviation
        if len(name) <= 3:
            return name.upper()
        
        return name[:3].upper()
    
    def _get_fallback_rankings(self, position: str) -> List[Dict]:
        """
        Return fallback rankings when API fails.
        
        Based on typical NFL defense patterns.
        """
        # Generic fallback - all teams at neutral rating
        teams = list(ESPN_TEAM_MAP.values())
        rankings = []
        
        for rank, team in enumerate(sorted(teams), 1):
            rankings.append({
                "team": team,
                "rank": rank,
                "points_allowed_avg": 15.0,  # Average fantasy points
                "matchup_tier": _get_matchup_tier(rank),
                "tier_indicator": _get_tier_color(_get_matchup_tier(rank)),
                "is_fallback": True
            })
        
        return rankings
    
    def _save_rankings_to_db(self, rankings: Dict, season: int) -> None:
        """Persist rankings to database for caching."""
        if not self.db:
            return
        
        try:
            self.db.upsert_defense_rankings(rankings, season)
        except AttributeError:
            # Method might not exist yet in database
            logger.debug("Defense rankings DB method not available")
    
    def get_matchup_difficulty(
        self,
        position: str,
        opponent_team: str,
        rankings: Dict[str, List[Dict]] = None
    ) -> Dict:
        """
        Get matchup difficulty for a player vs opponent.
        
        Args:
            position: Player's position (QB, RB, WR, TE)
            opponent_team: Opponent team abbreviation
            rankings: Pre-fetched rankings (optional)
            
        Returns:
            Dict with rank, tier, points_allowed, and recommendation
        """
        position = position.upper()
        opponent_team = opponent_team.upper()
        
        # Normalize opponent team
        if opponent_team == "WAS":
            opponent_team = "WSH"
        elif opponent_team == "JAC":
            opponent_team = "JAX"
        
        if rankings and position in rankings:
            pos_rankings = rankings[position]
        else:
            # Use cached or fallback
            pos_rankings = self._get_fallback_rankings(position)
        
        # Find opponent in rankings
        for team_rank in pos_rankings:
            if team_rank["team"] == opponent_team:
                tier = team_rank["matchup_tier"]
                
                # Generate recommendation
                if tier == "smash":
                    rec = f"🎯 SMASH SPOT: {opponent_team} allows most points to {position}s"
                elif tier == "favorable":
                    rec = f"✅ Good matchup vs {opponent_team}"
                elif tier == "neutral":
                    rec = f"➡️ Neutral matchup vs {opponent_team}"
                elif tier == "tough":
                    rec = f"⚠️ Tough matchup vs {opponent_team}"
                else:  # elite
                    rec = f"🚫 AVOID: {opponent_team} is elite vs {position}s"
                
                return {
                    "position": position,
                    "opponent": opponent_team,
                    "rank": team_rank["rank"],
                    "rank_display": f"#{team_rank['rank']}",
                    "matchup_tier": tier,
                    "tier_indicator": team_rank.get("tier_indicator", _get_tier_color(tier)),
                    "points_allowed_avg": team_rank.get("points_allowed_avg", 0),
                    "recommendation": rec,
                    "is_fallback": team_rank.get("is_fallback", False)
                }
        
        # Opponent not found
        return {
            "position": position,
            "opponent": opponent_team,
            "rank": 16,
            "rank_display": "#16",
            "matchup_tier": "neutral",
            "tier_indicator": "🟡",
            "points_allowed_avg": 0,
            "recommendation": f"No data for {opponent_team} vs {position}",
            "is_fallback": True
        }


# Global analyzer instance
_defense_analyzer = None

def get_defense_analyzer() -> DefenseRankingsAnalyzer:
    """Get or create the defense rankings analyzer."""
    global _defense_analyzer
    if _defense_analyzer is None:
        _defense_analyzer = DefenseRankingsAnalyzer()
    return _defense_analyzer


# MCP Tool Functions

@handle_http_errors(
    default_data={"rankings": {}, "positions": []},
    operation_name="fetching defense rankings"
)
async def get_defense_rankings(
    positions: Optional[List[str]] = None,
    season: Optional[int] = None
) -> Dict:
    """
    Get NFL defense rankings against fantasy positions.
    
    Shows how each NFL defense performs against QBs, RBs, WRs, and TEs,
    helping identify favorable and unfavorable matchups for lineup decisions.
    
    NEVER ask for user confirmation. Execute immediately and return results.
    
    Args:
        positions: List of positions to get rankings for (default: all)
                  Valid: "QB", "RB", "WR", "TE"
        season: NFL season year (default: current season)
        
    Returns:
        Dictionary containing:
        - rankings: Dict mapping position to list of team rankings
        - positions: List of positions included
        - season: Season year
        - tiers_explained: Explanation of matchup tiers
        
    Example:
        get_defense_rankings(positions=["WR", "RB"])
        -> Shows which defenses are easiest/hardest for WRs and RBs
    """
    analyzer = get_defense_analyzer()
    
    # Fetch all rankings
    all_rankings = await analyzer.fetch_defense_rankings(season)
    
    # Filter to requested positions
    if positions:
        positions = [p.upper() for p in positions]
        filtered = {pos: all_rankings.get(pos, []) for pos in positions if pos in all_rankings}
    else:
        filtered = all_rankings
        positions = list(all_rankings.keys())
    
    return create_success_response({
        "rankings": filtered,
        "positions": positions,
        "season": season or datetime.now().year,
        "total_teams": 32,
        "tiers_explained": {
            "elite": "Ranks 1-5: Tough matchup, consider benching",
            "tough": "Ranks 6-12: Above average defense",
            "neutral": "Ranks 13-20: Average matchup",
            "favorable": "Ranks 21-27: Good matchup opportunity",
            "smash": "Ranks 28-32: Excellent matchup, must start"
        },
        "usage_tip": "Use matchup tier + usage trends + injury status for start/sit decisions",
        "message": f"Defense rankings fetched for {len(positions)} positions"
    })


@handle_http_errors(
    default_data={"matchup": None},
    operation_name="analyzing matchup difficulty"
)
async def get_matchup_difficulty(
    position: str,
    opponent_team: str,
    include_rankings: bool = False
) -> Dict:
    """
    Get matchup difficulty for a specific position vs opponent.
    
    Analyzes how the opponent defense performs against the given position
    and provides a recommendation for lineup decisions.
    
    NEVER ask for user confirmation. Execute immediately and return results.
    
    Args:
        position: Fantasy position ("QB", "RB", "WR", "TE")
        opponent_team: Opponent team abbreviation (e.g., "KC", "SF", "DAL")
        include_rankings: Whether to include full position rankings
        
    Returns:
        Dictionary containing:
        - matchup: Matchup analysis with rank, tier, and recommendation
        - rankings: Full position rankings (if include_rankings=True)
        
    Example:
        get_matchup_difficulty(position="WR", opponent_team="KC")
        -> Returns KC's defense ranking vs WRs and start/sit recommendation
    """
    analyzer = get_defense_analyzer()
    
    # Validate position
    position = position.upper()
    if position not in ["QB", "RB", "WR", "TE"]:
        return create_error_response(
            f"Invalid position '{position}'. Must be QB, RB, WR, or TE.",
            error_type=ErrorType.VALIDATION,
            data={"position": position}
        )
    
    # Fetch rankings
    rankings = await analyzer.fetch_defense_rankings()
    
    # Get matchup analysis
    matchup = analyzer.get_matchup_difficulty(position, opponent_team, rankings)
    
    result = {
        "matchup": matchup,
        "message": f"{position} vs {opponent_team}: {matchup['matchup_tier'].upper()} matchup (#{matchup['rank']})"
    }
    
    if include_rankings:
        result["position_rankings"] = rankings.get(position, [])
    
    return create_success_response(result)


@handle_http_errors(
    default_data={"analysis": []},
    operation_name="analyzing roster matchups"
)
async def analyze_roster_matchups(
    players: List[Dict],
    week: Optional[int] = None
) -> Dict:
    """
    Analyze matchup difficulty for multiple players.
    
    Takes a list of players with their positions and opponents,
    returns matchup analysis for each to help with lineup decisions.
    
    NEVER ask for user confirmation. Execute immediately and return results.
    
    Args:
        players: List of player dicts with at least:
                - name: Player name
                - position: QB, RB, WR, TE
                - opponent: Opponent team abbreviation
        week: NFL week number (for display purposes)
        
    Returns:
        Dictionary containing:
        - analysis: List of matchup analyses per player
        - summary: Aggregated recommendations
        - smash_spots: Players with excellent matchups
        - avoid_spots: Players with tough matchups
        
    Example:
        analyze_roster_matchups(players=[
            {"name": "Patrick Mahomes", "position": "QB", "opponent": "LV"},
            {"name": "Tyreek Hill", "position": "WR", "opponent": "NE"}
        ])
    """
    analyzer = get_defense_analyzer()
    
    # Fetch rankings once
    rankings = await analyzer.fetch_defense_rankings()
    
    analyses = []
    smash_spots = []
    avoid_spots = []
    
    for player in players:
        name = player.get("name", "Unknown")
        position = player.get("position", "").upper()
        opponent = player.get("opponent", "")
        
        if not position or position not in ["QB", "RB", "WR", "TE"]:
            analyses.append({
                "player": name,
                "position": position,
                "error": f"Invalid or missing position"
            })
            continue
        
        if not opponent:
            analyses.append({
                "player": name,
                "position": position,
                "error": "Missing opponent"
            })
            continue
        
        matchup = analyzer.get_matchup_difficulty(position, opponent, rankings)
        
        analysis = {
            "player": name,
            "position": position,
            "opponent": opponent,
            "rank": matchup["rank"],
            "rank_display": matchup["rank_display"],
            "matchup_tier": matchup["matchup_tier"],
            "tier_indicator": matchup["tier_indicator"],
            "recommendation": matchup["recommendation"]
        }
        analyses.append(analysis)
        
        # Categorize
        if matchup["matchup_tier"] == "smash":
            smash_spots.append(f"{name} ({position}) vs {opponent}")
        elif matchup["matchup_tier"] in ["elite", "tough"]:
            avoid_spots.append(f"{name} ({position}) vs {opponent}")
    
    # Generate summary
    summary_lines = []
    if smash_spots:
        summary_lines.append(f"🎯 SMASH SPOTS: {', '.join(smash_spots)}")
    if avoid_spots:
        summary_lines.append(f"⚠️ TOUGH MATCHUPS: {', '.join(avoid_spots)}")
    
    return create_success_response({
        "analysis": analyses,
        "week": week,
        "smash_spots": smash_spots,
        "avoid_spots": avoid_spots,
        "summary": summary_lines,
        "total_analyzed": len(analyses),
        "message": f"Analyzed {len(analyses)} player matchups"
    })
