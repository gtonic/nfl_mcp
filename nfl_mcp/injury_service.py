import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import IntEnum

from . import injury_status

logger = logging.getLogger(__name__)

# Configuration constants for performance tuning
MAX_CONCURRENT_TEAMS = 6  # Parallel team fetches
MAX_CONCURRENT_INJURIES = 15  # Parallel injury detail fetches per team
# Athlete-name cache cap. The league-wide injury list names ~1900 athletes; at
# 500 (with no eviction) most names were refetched on every crawl.
ATHLETE_CACHE_SIZE = 4000
UNKNOWN_NAME = "Unknown"  # placeholder when an athlete's name could not be fetched
REQUEST_TIMEOUT = 10.0  # Seconds per request
# An empty team list is only believed in small numbers. One team's last injured
# player recovering is routine; several teams losing every report in the same
# crawl is ESPN serving empty lists (an outage), and pruning on it would mark
# every injured player on those teams Active.
MAX_EMPTY_TEAMS_PER_CRAWL = 3
# ...and only for a team that had few open reports to lose. ESPN keeps IR and
# PUP placements listed all season, so a team dropping from a handful of open
# reports to none in one crawl is a feed problem, not a recovery.
EMPTY_TEAM_MAX_PRIOR_OPEN = 2


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
    position: str | None = None
    injury_status: str = "Unknown"
    injury_type: str | None = None
    injury_description: str | None = None
    game_status: str | None = None  # Active/Inactive/IR/PUP
    severity: int | None = None
    confidence: int = 50  # 0-100
    sources: list[str] = field(default_factory=lambda: ["ESPN"])
    date_reported: str | None = None

    def to_dict(self) -> dict:
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


# The status vocabulary lives in `injury_status`; these are views of it for
# the callers that rank stored strings (SQL CASE maps, trend deltas).
STATUS_SEVERITY: dict[str, int] = injury_status.severity_map()

# Severity of a status the tables do not know. Projections price an
# unrecognised designation as questionable (0.9), so severity matches that
# rather than outranking a real Questionable.
DEFAULT_SEVERITY = InjurySeverity.QUESTIONABLE


# ESPN Core API `$ref` links end at the athlete id followed by a query string
# (".../athletes/4684527?lang=en&region=us"), but nested refs can also continue
# with another path segment. Accept both, plus end-of-string.
_ATHLETE_ID_PATTERN = re.compile(r"/athletes/(\d+)(?:/|\?|$)")


def status_severity(status: str | None) -> int:
    """Severity rank for a status string, DEFAULT_SEVERITY for anything unrecognised."""
    return injury_status.severity(status)


def worst_status(*statuses: str | None) -> str | None:
    """The most severe of several status strings, or None if all are empty.

    Sources disagree, and routinely: Sleeper's player list lags ESPN's injury
    feed by hours around kickoff. Taking the milder reading means projecting a
    player at 90% who one source already has at doubtful, which is the error
    that puts him in a lineup. Taking the worse one errs toward the bench,
    which is the recoverable direction.
    """
    known = [s for s in statuses if s]
    if not known:
        return None
    return max(known, key=lambda s: (status_severity(s),
                                     _TIEBREAK.get(injury_status.normalize(s), 0)))


# Doubtful and Out share a rung on the 1-5 scale, and `max` then kept whichever
# source happened to be passed first: roster enrichment showed Jayden Daniels at
# ESPN's "Doubtful" while Sleeper already had him "Out". Within a rung, a
# designation that rules the player out outranks one that merely doubts him.
_TIEBREAK = {"Doubtful": 0, "Out": 1, "Inactive": 1}


def extract_athlete_id(athlete_url: str | None) -> str | None:
    """ESPN athlete id from a Core-API ``$ref`` URL, or None.

    Shared so the injury fetchers cannot drift apart: a copy of this that
    required a trailing slash silently dropped *every* record it saw.
    """
    if not athlete_url:
        return None
    match = _ATHLETE_ID_PATTERN.search(athlete_url)
    return match.group(1) if match else None


class InjuryAggregator:
    """Aggregates injury data from multiple sources with confidence scoring.

    Performance optimizations:
    - Concurrent team fetching (MAX_CONCURRENT_TEAMS parallel)
    - Batch injury detail fetching (MAX_CONCURRENT_INJURIES per team)
    - LRU cache for athlete names to avoid duplicate API calls
    - Delta updates: only fetch teams with stale cache
    - ETag/If-Modified-Since support for HTTP caching
    """

    # NFL team abbreviations
    NFL_TEAMS = [
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
        "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
        "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
        "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WSH"
    ]

    # In-memory caches (class-level for reuse across instances)
    _athlete_name_cache: dict[str, str] = {}  # player_id -> name
    _etag_cache: dict[str, str] = {}  # url -> etag
    _last_modified_cache: dict[str, str] = {}  # url -> last-modified
    # url -> (pageCount, injury $ref URLs) of the last 200 for that list page,
    # reused on a 304 (which has no body) so "not modified" != "no injuries".
    _page_refs_cache: dict[str, tuple[int, list[str]]] = {}

    def __init__(self, http_client=None, db=None, persist: bool = True):
        """Initialize the aggregator.

        Args:
            http_client: Optional httpx.AsyncClient to use
            db: Optional NFLDatabase instance for caching, stored-name fallback
                and position lookup
            persist: Write fetched reports to ``db``. False for a caller that
                owns persistence (the prefetch, which prunes as it writes) but
                still wants the lookups.
        """
        self._http_client = http_client
        self._db = db
        self._persist = persist
        self._own_client = False
        # Semaphores for concurrency control
        self._team_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TEAMS)
        self._injury_semaphore = asyncio.Semaphore(MAX_CONCURRENT_INJURIES)
        # Teams whose last ESPN crawl was complete: every list page read and
        # every listed report resolved. Only these may be pruned (see
        # NFLDatabase.upsert_injuries) -- a partial crawl is not a recovery.
        self.complete_teams: set[str] = set()
        # Complete teams whose list came back empty (see prunable_teams).
        self.empty_teams: set[str] = set()

    def _require_client(self) -> None:
        """Fail loudly when used outside ``async with``.

        Without a client every team fetch raises ``'NoneType' object has no
        attribute 'get'`` from ``self._http_client.get``, which the per-team
        handler logs at debug level as "ESPN page 1 failed for BUF". That reads
        like a broken upstream payload — it cost a real debugging session
        chasing an ESPN feed that was perfectly intact. 32 teams then return
        zero records and the caller sees an empty, successful-looking result.
        """
        if self._http_client is None:
            raise RuntimeError(
                "InjuryAggregator has no HTTP client. Use it as a context "
                "manager (`async with InjuryAggregator(db=db) as agg:`) or pass "
                "`http_client=`. Without one every fetch fails and returns an "
                "empty list that looks like 'no injuries'."
            )

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

    @classmethod
    def clear_caches(cls) -> dict[str, int]:
        """Clear all in-memory caches.

        Returns:
            Dict with counts of cleared items per cache
        """
        stats = {
            "athlete_names": len(cls._athlete_name_cache),
            "etags": len(cls._etag_cache),
            "last_modified": len(cls._last_modified_cache),
            "page_refs": len(cls._page_refs_cache),
        }
        cls._athlete_name_cache.clear()
        cls._etag_cache.clear()
        cls._last_modified_cache.clear()
        cls._page_refs_cache.clear()
        logger.info(f"[InjuryAggregator] Cleared caches: {stats}")
        return stats

    @classmethod
    def get_cache_stats(cls) -> dict[str, any]:
        """Get statistics about in-memory caches.

        Returns:
            Dict with cache sizes and sample data
        """
        return {
            "athlete_name_cache_size": len(cls._athlete_name_cache),
            "athlete_name_cache_max": ATHLETE_CACHE_SIZE,
            "etag_cache_size": len(cls._etag_cache),
            "last_modified_cache_size": len(cls._last_modified_cache),
            "sample_athletes": list(cls._athlete_name_cache.items())[:5],
        }

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

        return injury_status.normalize(status) or status.strip().title()

    @staticmethod
    def get_severity(status: str) -> int:
        """Get severity score from normalized status.

        Args:
            status: Normalized status string

        Returns:
            Severity score 1-5
        """
        return injury_status.severity(status) or int(DEFAULT_SEVERITY)

    @staticmethod
    def calculate_confidence(sources: list[str], statuses_match: bool) -> int:
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

    async def fetch_espn_injuries(self, teams: list[str] | None = None) -> list[InjuryReport]:
        """Fetch injury reports from ESPN Core API with concurrent team fetching.

        Uses semaphore-controlled parallelism for optimal performance.

        Args:
            teams: Optional list of team abbreviations. If None, fetches all teams.

        Returns:
            List of InjuryReport objects

        Raises:
            RuntimeError: when used outside ``async with`` (no HTTP client).
        """
        self._require_client()
        teams = teams or self.NFL_TEAMS

        try:
            from .config import get_http_headers
            headers = get_http_headers("nfl_teams")

            # Fetch all teams concurrently with semaphore control
            async def fetch_team_with_semaphore(team: str) -> list[InjuryReport]:
                async with self._team_semaphore:
                    try:
                        return await self._fetch_team_espn_injuries(team, headers)
                    except Exception as e:
                        logger.debug(f"[InjuryAggregator] ESPN fetch failed for {team}: {e}")
                        return []

            # Run all team fetches concurrently
            team_results = await asyncio.gather(
                *[fetch_team_with_semaphore(team) for team in teams],
                return_exceptions=True
            )

            # Flatten results, filtering out exceptions
            all_injuries = []
            successful_teams = 0
            for i, result in enumerate(team_results):
                if isinstance(result, list):
                    all_injuries.extend(result)
                    if result:
                        successful_teams += 1
                elif isinstance(result, Exception):
                    logger.debug(f"[InjuryAggregator] Team {teams[i]} failed: {result}")

            logger.info(f"[InjuryAggregator] ESPN: fetched {len(all_injuries)} injuries from {successful_teams}/{len(teams)} teams")
            return all_injuries

        except Exception as e:
            logger.error(f"[InjuryAggregator] ESPN fetch failed: {e}")
            return []

    async def _fetch_team_espn_injuries(self, team: str, headers: dict) -> list[InjuryReport]:
        """Fetch injuries for a single team from ESPN with batch detail fetching.

        Uses semaphore-controlled parallel fetching for injury details.

        Args:
            team: Team abbreviation
            headers: HTTP headers to use

        Returns:
            List of InjuryReport objects for the team. The team is added to
            ``complete_teams`` only when every page was listed and every
            report resolved.
        """
        self.complete_teams.discard(team)
        self.empty_teams.discard(team)
        all_injury_urls = []
        page = 1
        page_count = 1
        listed_all = False

        # First, collect all injury URLs from paginated list
        while page <= page_count:
            url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{team}/injuries?limit=50&page={page}"

            try:
                # Add conditional request headers for caching -- only when the
                # page's refs are cached too, since a 304 carries no body.
                request_headers = dict(headers)
                cached_page = self._page_refs_cache.get(url)
                if cached_page is not None:
                    if url in self._etag_cache:
                        request_headers["If-None-Match"] = self._etag_cache[url]
                    if url in self._last_modified_cache:
                        request_headers["If-Modified-Since"] = self._last_modified_cache[url]

                resp = await self._http_client.get(url, headers=request_headers, timeout=REQUEST_TIMEOUT)

                # Handle 304 Not Modified - data unchanged: reuse the cached
                # refs for this page and carry on with the next one.
                if resp.status_code == 304:
                    if cached_page is None:
                        break
                    logger.debug(f"[InjuryAggregator] {team} page {page}: not modified (cached)")
                    cached_count, cached_refs = cached_page
                    if page == 1:
                        page_count = cached_count
                    all_injury_urls.extend(cached_refs)
                    page += 1
                    continue

                if resp.status_code != 200:
                    break

                # Store caching headers for future requests
                if "ETag" in resp.headers:
                    self._etag_cache[url] = resp.headers["ETag"]
                if "Last-Modified" in resp.headers:
                    self._last_modified_cache[url] = resp.headers["Last-Modified"]

                data = resp.json()
                if page == 1:
                    page_count = data.get("pageCount", 1)

                # Collect injury URLs
                page_refs = [
                    injury_ref.get("$ref")
                    for injury_ref in data.get("items", [])
                    if injury_ref.get("$ref")
                ]
                all_injury_urls.extend(page_refs)
                self._page_refs_cache[url] = (data.get("pageCount", 1), page_refs)

                page += 1

            except TimeoutError:
                logger.debug(f"[InjuryAggregator] {team} page {page}: timeout")
                break
            except Exception as e:
                logger.debug(f"[InjuryAggregator] ESPN page {page} failed for {team}: {e}")
                break
        else:
            listed_all = True  # no page failed

        if not all_injury_urls:
            # A fully listed team with no reports is complete: its last
            # injured player has recovered and must be pruned -- unless the
            # crawl as a whole says otherwise (prunable_teams).
            if listed_all:
                self.complete_teams.add(team)
                self.empty_teams.add(team)
            return []

        # Batch fetch all injury details concurrently
        async def fetch_injury_with_semaphore(injury_url: str) -> InjuryReport | None:
            async with self._injury_semaphore:
                try:
                    return await self._fetch_espn_injury_detail(injury_url, headers)
                except Exception as e:
                    logger.debug(f"[InjuryAggregator] Failed to fetch injury detail: {e}")
                    return None

        # Fetch all injury details in parallel
        injury_results = await asyncio.gather(
            *[fetch_injury_with_semaphore(url) for url in all_injury_urls],
            return_exceptions=True
        )

        # Filter successful results and set team_id
        injuries = []
        for result in injury_results:
            if isinstance(result, InjuryReport):
                result.team_id = team
                injuries.append(result)

        if listed_all and len(injuries) == len(all_injury_urls):
            self.complete_teams.add(team)
        else:
            logger.info(
                f"[InjuryAggregator] {team}: partial crawl ({len(injuries)}/"
                f"{len(all_injury_urls)} reports, all pages listed={listed_all}); not pruned"
            )
        return injuries

    async def _fetch_espn_injury_detail(self, url: str, headers: dict) -> InjuryReport | None:
        """Fetch individual injury detail from ESPN with athlete name caching.

        Uses class-level LRU cache for athlete names to avoid duplicate API calls.

        Args:
            url: ESPN injury detail URL
            headers: HTTP headers

        Returns:
            InjuryReport or None
        """
        from .config import safe_espn_ref

        # Follow only ESPN links, over https: an off-ESPN ref resolves to no
        # report, so its team counts as partially crawled and is not pruned.
        url = safe_espn_ref(url)
        if not url:
            return None
        try:
            resp = await self._http_client.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                return None

            data = resp.json()

            # Extract athlete info
            athlete_ref = data.get("athlete", {})
            if not athlete_ref:
                return None

            # Extract player ID from athlete URL
            athlete_url = athlete_ref.get("$ref", "")
            player_id = extract_athlete_id(athlete_url)
            if not player_id:
                return None

            # Check athlete name cache first
            player_name = self._athlete_name_cache.get(player_id)
            position = None

            if not player_name:
                # Try inline displayName first
                player_name = athlete_ref.get("displayName")

                athlete_link = safe_espn_ref(athlete_url)
                if not player_name and athlete_link:
                    # Fetch athlete details (with timeout)
                    try:
                        athlete_resp = await self._http_client.get(
                            athlete_link, headers=headers, timeout=REQUEST_TIMEOUT
                        )
                        if athlete_resp.status_code == 200:
                            athlete_data = athlete_resp.json()
                            player_name = athlete_data.get("displayName")
                            pos = athlete_data.get("position")
                            position = (pos.get("abbreviation") if isinstance(pos, dict)
                                        else None)
                    except Exception:
                        player_name = None

                # Cache real names only: a cached "Unknown" from one failed
                # fetch stuck for the life of the process.
                if player_name and len(self._athlete_name_cache) < ATHLETE_CACHE_SIZE:
                    self._athlete_name_cache[player_id] = player_name
                # The store keeps its previously known name for an "Unknown"
                # (see NFLDatabase.upsert_injuries).
                player_name = player_name or UNKNOWN_NAME

            # Extract status and type
            status_data = data.get("status", {})
            type_data = data.get("type", {})

            raw_status = status_data if isinstance(status_data, str) else status_data.get("description", "Unknown")
            normalized_status = self.normalize_status(raw_status)

            # ESPN is a single source, but its status IS the confidence signal:
            # a definitive "Out"/IR/Doubtful is far more certain than
            # "Questionable" (which in turn beats a plain "Active" listing). A
            # flat 60 made get_high_confidence_injuries(min_confidence=70) always
            # empty, so grade confidence by status certainty instead.
            _s = (normalized_status or "").lower()
            if any(k in _s for k in ("out", "injured reserve", "reserve", "doubtful", "suspend")):
                _confidence = 90
            elif "questionable" in _s:
                _confidence = 65
            elif any(k in _s for k in ("probable", "active", "day", "limited")):
                _confidence = 55
            else:
                _confidence = 50

            details = data.get("details") or {}
            return InjuryReport(
                player_id=str(player_id),
                player_name=player_name,
                team_id="",  # Will be set by caller
                # Not in the injury endpoint; the athlete fetch has it when it
                # ran, fetch_all_injuries fills the rest from the athletes table.
                position=position,
                injury_status=normalized_status,
                # Prefer the body part; fall back to the human-readable status
                # description ("active") — never the raw enum ("INJURY_STATUS_ACTIVE").
                injury_type=(details.get("type")
                             or (type_data.get("description") if isinstance(type_data, dict) else None)),
                injury_description=data.get("shortComment") or data.get("longComment"),
                game_status=_game_status(details),
                severity=self.get_severity(normalized_status),
                confidence=_confidence,
                sources=["ESPN"],
                date_reported=data.get("date"),
            )
        except TimeoutError:
            logger.debug(f"[InjuryAggregator] Timeout fetching injury detail: {url}")
            return None
        except Exception as e:
            logger.debug(f"[InjuryAggregator] Error fetching injury detail: {e}")
            return None

    async def fetch_cbs_injuries(self, teams: list[str] | None = None) -> list[InjuryReport]:
        """CBS injury source: NOT IMPLEMENTED — always returns ``[]``.

        CBS has no public injury API and no scraper is wired in, so every
        report is ESPN-only (single source) and ``confidence`` does not vary
        with source agreement. Kept as the hook for a second source; do not
        describe injuries as multi-source or CBS-verified.

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
        teams: list[str] | None = None,
        use_cache: bool = True,
        cache_ttl_hours: int | None = None,
        force_refresh: bool = False
    ) -> list[InjuryReport]:
        """Fetch and aggregate injuries from all sources with delta updates.

        Uses incremental fetching: only fetches teams with stale cache,
        combines with fresh cached data for optimal performance.

        Args:
            teams: Optional list of team abbreviations
            use_cache: Whether to use database cache
            cache_ttl_hours: Cache TTL in hours (None for adaptive)
            force_refresh: If True, bypasses cache entirely

        Returns:
            List of aggregated InjuryReport objects
        """
        teams = teams or self.NFL_TEAMS

        # Force refresh bypasses all caching
        if force_refresh:
            use_cache = False

        # Check cache and identify stale teams
        fresh_cached: list[InjuryReport] = []
        stale_teams: list[str] = []

        if use_cache and self._db:
            for team in teams:
                team_injuries = self._get_cached_injuries([team], cache_ttl_hours)
                if team_injuries:
                    fresh_cached.extend(team_injuries)
                else:
                    stale_teams.append(team)

            # If all teams are fresh, return cached data
            if not stale_teams:
                logger.info(f"[InjuryAggregator] All {len(teams)} teams fresh in cache: {len(fresh_cached)} injuries")
                return fresh_cached

            logger.info(f"[InjuryAggregator] Delta update: {len(stale_teams)} stale teams, {len(teams) - len(stale_teams)} fresh")
        else:
            stale_teams = teams

        # Fetch from all sources concurrently (only stale teams)
        espn_task = self.fetch_espn_injuries(stale_teams)
        cbs_task = self.fetch_cbs_injuries(stale_teams)

        espn_injuries, cbs_injuries = await asyncio.gather(espn_task, cbs_task)

        # Aggregate newly fetched data
        newly_fetched = self._aggregate_injuries(espn_injuries, cbs_injuries)

        # A failed name fetch: reuse the name stored for that athlete.
        if self._db and hasattr(self._db, "get_player_injury_from_cache"):
            for inj in newly_fetched:
                if inj.player_name in ("", UNKNOWN_NAME):
                    try:
                        stored = self._db.get_player_injury_from_cache(
                            inj.player_id, max_age_hours=24 * 365)
                    except Exception:
                        stored = None
                    name = (stored or {}).get("player_name") if isinstance(stored, dict) else None
                    if name and name != UNKNOWN_NAME:
                        inj.player_name = name

        # ESPN's injury endpoint carries no position; without one the
        # position guard in injury_match never fires.
        if self._db:
            self._fill_positions(newly_fetched)

        # Cache newly fetched results
        if self._db and self._persist and newly_fetched:
            cached_count = self._cache_injuries(newly_fetched)
            logger.debug(f"[InjuryAggregator] Cached {cached_count} injuries from {len(stale_teams)} teams")

        # Combine fresh cached + newly fetched
        all_injuries = fresh_cached + newly_fetched

        return all_injuries

    def _fill_positions(self, reports: list[InjuryReport]) -> None:
        """Set a missing ``position`` from the Sleeper athletes table.

        By ESPN id first (Sleeper's payload names it: an exact join), else by
        normalized name on the same team when exactly one athlete has it.
        """
        missing = [r for r in reports if not r.position and r.player_id]
        if not missing:
            return
        from .opportunity_tools import norm_name  # deferred: heavy import
        from .teams import normalize_team

        by_espn: dict = {}
        if hasattr(self._db, "get_athletes_by_espn_ids"):
            try:
                by_espn = self._db.get_athletes_by_espn_ids(
                    [r.player_id for r in missing]) or {}
            except Exception as e:
                logger.debug(f"[InjuryAggregator] ESPN id lookup failed: {e}")
        rosters: dict[str, dict[str, dict | None]] = {}
        for report in missing:
            row = by_espn.get(str(report.player_id)) if isinstance(by_espn, dict) else None
            team = normalize_team(report.team_id)
            if row is None and team and hasattr(self._db, "get_athletes_by_team"):
                if team not in rosters:
                    names: dict[str, dict | None] = {}
                    try:
                        for a in self._db.get_athletes_by_team(team) or []:
                            key = norm_name(a.get("full_name"))
                            if key:
                                # Two athletes of one name: no guess.
                                names[key] = None if key in names else a
                    except Exception as e:
                        logger.debug(f"[InjuryAggregator] roster lookup failed for {team}: {e}")
                    rosters[team] = names
                row = rosters[team].get(norm_name(report.player_name))
            if isinstance(row, dict) and row.get("position"):
                report.position = row["position"]

    def _get_cached_injuries(
        self,
        teams: list[str],
        cache_ttl_hours: int | None
    ) -> list[InjuryReport]:
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

    def _cache_injuries(self, injuries: list[InjuryReport]) -> int:
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
        espn_injuries: list[InjuryReport],
        cbs_injuries: list[InjuryReport]
    ) -> list[InjuryReport]:
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
        injury_map: dict[tuple[str, str], InjuryReport] = {}

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
        cache_ttl_hours: int | None = None
    ) -> list[InjuryReport]:
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
        team_id: str | None = None,
        use_cache: bool = True
    ) -> InjuryReport | None:
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
    teams: list[str] | None = None,
    db=None,
    use_cache: bool = True
) -> list[dict]:
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


def _game_status(details: dict) -> str | None:
    """ESPN's roster designation for a report (``details.fantasyStatus``).

    Says what the status alone does not: ``Out`` with ``PUP-R`` is a reserve
    list, not a one-week absence. All-caps words ("QUESTIONABLE") are shown in
    the canonical spelling; list codes ("PUP-R") as sent.
    """
    fantasy = details.get("fantasyStatus") if isinstance(details, dict) else None
    value = (fantasy or {}).get("description") if isinstance(fantasy, dict) else None
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if value.isalpha() and value.isupper():
        return injury_status.normalize(value) or value.title()
    return value


def open_report_counts(db) -> dict[str, int]:
    """``{team: stored reports that are not healthy}`` -- what a prune would clear."""
    counts: dict[str, int] = {}
    if db is None or not hasattr(db, "get_all_current_injuries"):
        return counts
    try:
        rows = db.get_all_current_injuries() or []
    except Exception as e:
        logger.debug(f"[InjuryAggregator] stored reports unavailable: {e}")
        return counts
    for row in rows:
        team = row.get("team_id")
        if team and not injury_status.is_healthy(row.get("injury_status")):
            counts[team] = counts.get(team, 0) + 1
    return counts


def prunable_teams(complete: set[str], empty: set[str],
                   prior_open: dict[str, int] | None = None) -> set[str]:
    """The teams a crawl may prune, from those it covered completely.

    A complete team with an empty list used to count as "everyone recovered",
    so an ESPN outage serving empty lists marked every injured player Active.
    Now: more than ``MAX_EMPTY_TEAMS_PER_CRAWL`` empty teams in one crawl and
    nothing is pruned; otherwise an empty team is pruned only if it had at
    most ``EMPTY_TEAM_MAX_PRIOR_OPEN`` open reports (its last injured player
    recovering, while the rest of the league still reports).
    """
    complete = set(complete)
    empty = set(empty) & complete
    if len(empty) > MAX_EMPTY_TEAMS_PER_CRAWL:
        logger.warning(
            f"[InjuryAggregator] {len(empty)} teams returned no injury reports "
            f"({', '.join(sorted(empty))}); treating it as a feed outage, nothing pruned"
        )
        return set()
    prior_open = prior_open or {}
    held = {t for t in empty if prior_open.get(t, 0) > EMPTY_TEAM_MAX_PRIOR_OPEN}
    if held:
        logger.warning(
            "[InjuryAggregator] empty injury list for "
            + ", ".join(f"{t} ({prior_open[t]} open)" for t in sorted(held))
            + "; not pruned"
        )
    return complete - held


async def crawl_injury_reports(teams: list[str] | None = None,
                               db=None) -> tuple[list[dict], set[str]]:
    """A fresh (uncached) crawl plus the teams it may prune.

    For the prefetch, which prunes reports a crawl no longer lists: only the
    returned teams may be pruned, including those with no reports left (see
    ``prunable_teams``). ``db`` (default: the shared database) is read for the
    stored-name fallback, positions and the prior open reports -- never
    written: the caller persists.
    """
    if db is None:
        try:
            from .database import get_shared_db
            db = get_shared_db()
        except Exception as e:
            logger.debug(f"[InjuryAggregator] shared database unavailable: {e}")
            db = None
    async with InjuryAggregator(db=db, persist=False) as aggregator:
        injuries = await aggregator.fetch_all_injuries(teams, use_cache=False)
        complete = set(aggregator.complete_teams)
        empty = set(aggregator.empty_teams) & complete
        prior = open_report_counts(db) if empty else {}
        return [inj.to_dict() for inj in injuries], prunable_teams(complete, empty, prior)


async def get_player_injury_report(
    player_id: str,
    team_id: str | None = None,
    db=None
) -> dict | None:
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
