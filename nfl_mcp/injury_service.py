import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from enum import IntEnum
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class InjurySeverity(IntEnum):
    """Injury severity scale (1-5)."""
    MINOR = 1       # Day-to-day, likely to play
    QUESTIONABLE = 2  # Game-time decision
    MODERATE = 3    # Expected to miss 1-2 weeks
    SIGNIFICANT = 4  # Multi-week absence
    SEVERE = 5      # Season-ending or long-term


@dataclass
class InjuryReport:
    """Normalized injury report with confidence scoring."""
    player_id: str
    player_name: str
    team_id: str
    position: Optional[str] = None
    injury_status: str = "Unknown"
    injury_type: Optional[str] = None
    injury_description: Optional[str] = None
    game_status: Optional[str] = None  # Active/Inactive/IR/PUP
    severity: Optional[int] = None
    confidence: int = 50  # 0-100
    sources: List[str] = field(default_factory=lambda: ["ESPN"])
    date_reported: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for database storage."""
        return {
            "player_id": self.player_id,
            "player_name": self.player_name,
            "team_id": self.team_id,
            "position": self.position,
            "injury_status": self.injury_status,
            "injury_type": self.injury_type,
            "injury_description": self.injury_description,
            "game_status": self.game_status,
            "severity": self.severity,
            "confidence": self.confidence,
            "sources": self.sources,
            "date_reported": self.date_reported,
        }


# Status normalization mappings
STATUS_NORMALIZATIONS = {
    # ESPN statuses
    "out": "Out",
    "doubtful": "Doubtful",
    "questionable": "Questionable",
    "probable": "Probable",
    "active": "Active",
    "day-to-day": "Questionable",
    "injured reserve": "IR",
    "ir": "IR",
    "pup": "PUP",
    "nfi": "NFI",
    "suspension": "Suspended",
    "reserve/covid-19": "Reserve",
    
    # CBS/other variations
    "inj": "Out",
    "q": "Questionable",
    "d": "Doubtful",
    "o": "Out",
    "p": "Probable",
    "i/r": "IR",
    "injured": "Out",
    "healthy": "Active",
    "limited": "Questionable",
    "did not practice": "DNP",
    "dnp": "DNP",
    "limited participation": "LP",
    "lp": "LP",
    "full participation": "FP",
    "fp": "FP",
}

# Severity mapping based on status
STATUS_SEVERITY = {
    "Active": InjurySeverity.MINOR,
    "Probable": InjurySeverity.MINOR,
    "FP": InjurySeverity.MINOR,
    "LP": InjurySeverity.QUESTIONABLE,
    "Questionable": InjurySeverity.QUESTIONABLE,
    "DNP": InjurySeverity.MODERATE,
    "Doubtful": InjurySeverity.SIGNIFICANT,
    "Out": InjurySeverity.SIGNIFICANT,
    "IR": InjurySeverity.SEVERE,
    "PUP": InjurySeverity.SEVERE,
    "NFI": InjurySeverity.SEVERE,
    "Suspended": InjurySeverity.SEVERE,
}


class InjuryAggregator:
    """Aggregates injury data from multiple sources with confidence scoring."""
    
    # NFL team abbreviations
    NFL_TEAMS = [
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
        "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
        "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
        "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WSH"
    ]
    
    def __init__(self, http_client=None, db=None):
        """Initialize the aggregator.
        
        Args:
            http_client: Optional httpx.AsyncClient to use
            db: Optional NFLDatabase instance for caching
        """
        self._http_client = http_client
        self._db = db
        self._own_client = False
    
    async def __aenter__(self):
        """Async context manager entry."""
        if self._http_client is None:
            from .config import create_http_client
            self._http_client = await create_http_client().__aenter__()
            self._own_client = True
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._own_client and self._http_client:
            await self._http_client.__aexit__(exc_type, exc_val, exc_tb)
    
    @staticmethod
    def normalize_status(status: str) -> str:
        """Normalize injury status to standard format.
        
        Args:
            status: Raw status string from any source
            
        Returns:
            Normalized status string
        """
        if not status:
            return "Unknown"
        
        status_lower = status.lower().strip()
        return STATUS_NORMALIZATIONS.get(status_lower, status.title())
    
    @staticmethod
    def get_severity(status: str) -> int:
        """Get severity score from normalized status.
        
        Args:
            status: Normalized status string
            
        Returns:
            Severity score 1-5
        """
        return STATUS_SEVERITY.get(status, InjurySeverity.MODERATE)
    
    @staticmethod
    def calculate_confidence(sources: List[str], statuses_match: bool) -> int:
        """Calculate confidence score based on sources and agreement.
        
        Args:
            sources: List of source names
            statuses_match: Whether statuses from different sources match
            
        Returns:
            Confidence score 0-100
        """
        base_score = 40
        
        # Add points for each source
        source_points = min(len(sources) * 20, 40)  # Max 40 from sources
        
        # Add points for source agreement
        agreement_points = 20 if statuses_match else 0
        
        return min(base_score + source_points + agreement_points, 100)
    
    async def fetch_espn_injuries(self, teams: List[str] = None) -> List[InjuryReport]:
        """Fetch injury reports from ESPN Core API.
        
        Args:
            teams: Optional list of team abbreviations. If None, fetches all teams.
            
        Returns:
            List of InjuryReport objects
        """
        teams = teams or self.NFL_TEAMS
        all_injuries = []
        
        try:
            from .config import get_http_headers
            headers = get_http_headers("nfl_teams")
            
            for team in teams:
                try:
                    team_injuries = await self._fetch_team_espn_injuries(team, headers)
                    all_injuries.extend(team_injuries)
                except Exception as e:
                    logger.debug(f"[InjuryAggregator] ESPN fetch failed for {team}: {e}")
                    continue
            
            logger.info(f"[InjuryAggregator] ESPN: fetched {len(all_injuries)} injuries from {len(teams)} teams")
            return all_injuries
            
        except Exception as e:
            logger.error(f"[InjuryAggregator] ESPN fetch failed: {e}")
            return []
    
    async def _fetch_team_espn_injuries(self, team: str, headers: Dict) -> List[InjuryReport]:
        """Fetch injuries for a single team from ESPN.
        
        Args:
            team: Team abbreviation
            headers: HTTP headers to use
            
        Returns:
            List of InjuryReport objects for the team
        """
        injuries = []
        page = 1
        page_count = 1
        
        while page <= page_count:
            url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{team}/injuries?limit=50&page={page}"
            
            try:
                resp = await self._http_client.get(url, headers=headers)
                if resp.status_code != 200:
                    break
                
                data = resp.json()
                if page == 1:
                    page_count = data.get("pageCount", 1)
                
                # ESPN Core API returns $ref URLs for each injury
                for injury_ref in data.get("items", []):
                    injury_url = injury_ref.get("$ref")
                    if not injury_url:
                        continue
                    
                    try:
                        injury_detail = await self._fetch_espn_injury_detail(injury_url, headers)
                        if injury_detail:
                            injury_detail.team_id = team
                            injuries.append(injury_detail)
                    except Exception as e:
                        logger.debug(f"[InjuryAggregator] Failed to fetch injury detail: {e}")
                        continue
                
                page += 1
                
            except Exception as e:
                logger.debug(f"[InjuryAggregator] ESPN page {page} failed for {team}: {e}")
                break
        
        return injuries
    
    async def _fetch_espn_injury_detail(self, url: str, headers: Dict) -> Optional[InjuryReport]:
        """Fetch individual injury detail from ESPN.
        
        Args:
            url: ESPN injury detail URL
            headers: HTTP headers
            
        Returns:
            InjuryReport or None
        """
        resp = await self._http_client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        
        data = resp.json()
        
        # Extract athlete info
        athlete_ref = data.get("athlete", {})
        if not athlete_ref:
            return None
        
        # Extract player ID from athlete URL
        athlete_url = athlete_ref.get("$ref", "")
        id_match = re.search(r"/athletes/(\d+)/", athlete_url)
        if not id_match:
            return None
        
        player_id = id_match.group(1)
        
        # Get player name - may need to fetch
        player_name = athlete_ref.get("displayName")
        if not player_name:
            try:
                athlete_resp = await self._http_client.get(athlete_url, headers=headers)
                if athlete_resp.status_code == 200:
                    athlete_data = athlete_resp.json()
                    player_name = athlete_data.get("displayName", "Unknown")
                else:
                    player_name = "Unknown"
            except:
                player_name = "Unknown"
        
        # Extract status and type
        status_data = data.get("status", {})
        type_data = data.get("type", {})
        
        raw_status = status_data if isinstance(status_data, str) else status_data.get("description", "Unknown")
        normalized_status = self.normalize_status(raw_status)
        
        return InjuryReport(
            player_id=str(player_id),
            player_name=player_name,
            team_id="",  # Will be set by caller
            position=None,  # Not available in injury endpoint
            injury_status=normalized_status,
            injury_type=type_data.get("name") if isinstance(type_data, dict) else None,
            injury_description=data.get("shortComment") or data.get("longComment"),
            game_status=None,
            severity=self.get_severity(normalized_status),
            confidence=60,  # Single source baseline
            sources=["ESPN"],
            date_reported=data.get("date"),
        )
    
    async def fetch_cbs_injuries(self, teams: List[str] = None) -> List[InjuryReport]:
        """Fetch injury reports from CBS Sports.
        
        Note: CBS doesn't have a public API, so this uses web scraping.
        This is a placeholder for future implementation.
        
        Args:
            teams: Optional list of team abbreviations
            
        Returns:
            List of InjuryReport objects
        """
        # CBS Sports scraping would require:
        # 1. Fetching https://www.cbssports.com/nfl/injuries/
        # 2. Parsing the HTML for injury tables
        # 3. Matching players to IDs
        #
        # For now, return empty - can be implemented later
        logger.debug("[InjuryAggregator] CBS source not yet implemented")
        return []
    
    async def fetch_all_injuries(
        self, 
        teams: List[str] = None,
        use_cache: bool = True,
        cache_ttl_hours: Optional[int] = None
    ) -> List[InjuryReport]:
        """Fetch and aggregate injuries from all sources.
        
        Args:
            teams: Optional list of team abbreviations
            use_cache: Whether to use database cache
            cache_ttl_hours: Cache TTL in hours (None for adaptive)
            
        Returns:
            List of aggregated InjuryReport objects
        """
        teams = teams or self.NFL_TEAMS
        
        # Check cache first if database available
        if use_cache and self._db:
            cached = self._get_cached_injuries(teams, cache_ttl_hours)
            if cached:
                logger.info(f"[InjuryAggregator] Using cached data: {len(cached)} injuries")
                return cached
        
        # Fetch from all sources concurrently
        espn_task = self.fetch_espn_injuries(teams)
        cbs_task = self.fetch_cbs_injuries(teams)
        
        espn_injuries, cbs_injuries = await asyncio.gather(espn_task, cbs_task)
        
        # Aggregate and dedupe
        aggregated = self._aggregate_injuries(espn_injuries, cbs_injuries)
        
        # Cache results
        if self._db and aggregated:
            self._cache_injuries(aggregated)
        
        return aggregated
    
    def _get_cached_injuries(
        self, 
        teams: List[str],
        cache_ttl_hours: Optional[int]
    ) -> List[InjuryReport]:
        """Get injuries from database cache.
        
        Args:
            teams: List of team abbreviations
            cache_ttl_hours: Cache TTL in hours
            
        Returns:
            List of InjuryReport objects or empty list if cache miss
        """
        if not self._db:
            return []
        
        all_cached = []
        for team in teams:
            team_injuries = self._db.get_team_injuries_from_cache(team, cache_ttl_hours)
            for inj in team_injuries:
                all_cached.append(InjuryReport(
                    player_id=inj["player_id"],
                    player_name=inj["player_name"],
                    team_id=inj["team_id"],
                    position=inj.get("position"),
                    injury_status=inj.get("injury_status", "Unknown"),
                    injury_type=inj.get("injury_type"),
                    injury_description=inj.get("injury_description"),
                    game_status=inj.get("game_status"),
                    severity=inj.get("severity"),
                    confidence=inj.get("confidence", 50),
                    sources=inj.get("sources", ["ESPN"]),
                    date_reported=inj.get("date_reported"),
                ))
        
        return all_cached
    
    def _cache_injuries(self, injuries: List[InjuryReport]) -> int:
        """Cache injuries to database.
        
        Args:
            injuries: List of InjuryReport objects
            
        Returns:
            Number of injuries cached
        """
        if not self._db:
            return 0
        
        injury_dicts = [inj.to_dict() for inj in injuries]
        return self._db.upsert_injuries(injury_dicts)
    
    def _aggregate_injuries(
        self,
        espn_injuries: List[InjuryReport],
        cbs_injuries: List[InjuryReport]
    ) -> List[InjuryReport]:
        """Aggregate injuries from multiple sources.
        
        Deduplicates by (player_id, team_id) and calculates confidence
        based on source agreement.
        
        Args:
            espn_injuries: Injuries from ESPN
            cbs_injuries: Injuries from CBS
            
        Returns:
            Aggregated list with confidence scores
        """
        # Index by (player_id, team_id)
        injury_map: Dict[Tuple[str, str], InjuryReport] = {}
        
        # Process ESPN injuries first (primary source)
        for inj in espn_injuries:
            key = (inj.player_id, inj.team_id)
            injury_map[key] = inj
        
        # Merge CBS injuries
        for inj in cbs_injuries:
            key = (inj.player_id, inj.team_id)
            
            if key in injury_map:
                existing = injury_map[key]
                # Check if statuses match
                statuses_match = existing.injury_status == inj.injury_status
                
                # Merge sources
                sources = list(set(existing.sources + inj.sources))
                
                # Calculate new confidence
                confidence = self.calculate_confidence(sources, statuses_match)
                
                # Update existing with merged data
                existing.sources = sources
                existing.confidence = confidence
                
                # If CBS has additional info, add it
                if inj.injury_description and not existing.injury_description:
                    existing.injury_description = inj.injury_description
                if inj.game_status and not existing.game_status:
                    existing.game_status = inj.game_status
            else:
                # New injury from CBS only
                inj.confidence = 40  # Lower confidence for single source
                injury_map[key] = inj
        
        return list(injury_map.values())
    
    async def get_team_injuries(
        self, 
        team: str,
        use_cache: bool = True,
        cache_ttl_hours: Optional[int] = None
    ) -> List[InjuryReport]:
        """Get injuries for a specific team.
        
        Args:
            team: Team abbreviation
            use_cache: Whether to use cache
            cache_ttl_hours: Cache TTL (None for adaptive)
            
        Returns:
            List of InjuryReport objects
        """
        return await self.fetch_all_injuries([team], use_cache, cache_ttl_hours)
    
    async def get_player_injury(
        self,
        player_id: str,
        team_id: str = None,
        use_cache: bool = True
    ) -> Optional[InjuryReport]:
        """Get injury status for a specific player.
        
        Args:
            player_id: Player identifier
            team_id: Optional team abbreviation (speeds up lookup)
            use_cache: Whether to use cache
            
        Returns:
            InjuryReport or None
        """
        # Check cache first
        if use_cache and self._db:
            cached = self._db.get_player_injury_from_cache(player_id)
            if cached:
                return InjuryReport(
                    player_id=cached["player_id"],
                    player_name=cached["player_name"],
                    team_id=cached["team_id"],
                    position=cached.get("position"),
                    injury_status=cached.get("injury_status", "Unknown"),
                    injury_type=cached.get("injury_type"),
                    injury_description=cached.get("injury_description"),
                    game_status=cached.get("game_status"),
                    severity=cached.get("severity"),
                    confidence=cached.get("confidence", 50),
                    sources=cached.get("sources", ["ESPN"]),
                    date_reported=cached.get("date_reported"),
                )
        
        # If we have team_id, fetch that team's injuries
        if team_id:
            injuries = await self.get_team_injuries(team_id, use_cache=False)
            for inj in injuries:
                if inj.player_id == player_id:
                    return inj
        
        return None


# Convenience function for tool usage
async def get_injury_reports(
    teams: List[str] = None,
    db=None,
    use_cache: bool = True
) -> List[Dict]:
    """Get injury reports for teams.
    
    Args:
        teams: List of team abbreviations (None for all)
        db: Optional NFLDatabase instance
        use_cache: Whether to use cache
        
    Returns:
        List of injury dicts
    """
    async with InjuryAggregator(db=db) as aggregator:
        injuries = await aggregator.fetch_all_injuries(teams, use_cache)
        return [inj.to_dict() for inj in injuries]


async def get_player_injury_report(
    player_id: str,
    team_id: str = None,
    db=None
) -> Optional[Dict]:
    """Get injury report for a specific player.
    
    Args:
        player_id: Player identifier
        team_id: Optional team abbreviation
        db: Optional NFLDatabase instance
        
    Returns:
        Injury dict or None
    """
    async with InjuryAggregator(db=db) as aggregator:
        injury = await aggregator.get_player_injury(player_id, team_id)
        return injury.to_dict() if injury else None
