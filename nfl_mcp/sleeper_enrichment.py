"""Sleeper enrichment & data-fetch layer (split out of sleeper_tools.py).

Best-effort fetchers (schedule, snaps, injuries, practice reports, weekly usage)
plus the usage/opponent enrichment helpers. These are LEAF helpers — the public
Sleeper tools call into them, not the reverse — so extracting them is cycle-free.
Re-exported from ``sleeper_tools`` for backward compatibility.
"""
import json
import logging
import os
from datetime import UTC, datetime

from .config import (
    DEFAULT_TIMEOUT,
    create_http_client,
    get_http_headers,
)

logger = logging.getLogger(__name__)


ADVANCED_ENRICH_ENABLED = os.getenv("NFL_MCP_ADVANCED_ENRICH") == "1"

async def _fetch_week_player_snaps(season: int, week: int):
    """Fetch player snap stats (best-effort) from Sleeper weekly stats endpoint.

    Returns list of dicts for upsert_player_week_stats. If advanced enrichment disabled
    or network/API issues occur, returns empty list.

    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug("[Fetch Snaps] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info(f"[Fetch Snaps] Starting fetch for season={season}, week={week}")

    async def _fetch():
        headers = get_http_headers("sleeper_week_stats")
        url = f"https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}"

        async with create_http_client() as client:
            resp = await client.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"[Fetch Snaps] API returned status {resp.status_code}")
                return []
            data = resp.json() or {}
            if not isinstance(data, dict):
                logger.warning("[Fetch Snaps] Invalid data format (not dict)")
                return []

            # Validate response
            from .response_validation import validate_response_and_log, validate_snap_count_response
            if not validate_response_and_log(data, validate_snap_count_response, "Snaps", allow_partial=True):
                logger.error("[Fetch Snaps] Response validation failed, returning empty list")
                return []

            logger.debug(f"[Fetch Snaps] Received data for {len(data)} players")
            rows = []
            for pid, stats in list(data.items())[:5000]:  # cap for safety
                if not isinstance(stats, dict):
                    continue
                # Attempt to extract snaps & snap_pct fields (naming may vary)
                # Sleeper uses 'off_snp' (not 'off_snaps'), so check both variations
                snaps = stats.get("snaps") or stats.get("off_snp") or stats.get("off_snaps") or stats.get("offense_snaps")
                team_snaps = stats.get("team_snaps") or stats.get("tm_off_snp") or stats.get("off_team_snaps") or stats.get("team_snp")
                snap_pct = stats.get("snap_pct") or stats.get("off_snp_pct") or stats.get("off_snap_pct")
                rows.append({
                    "player_id": str(pid),
                    "season": season,
                    "week": week,
                    "snaps_offense": snaps,
                    "snaps_team_offense": team_snaps,
                    "snap_pct": snap_pct,
                    "raw": stats
                })

            logger.info(f"[Fetch Snaps] Successfully fetched {len(rows)} snap records (season={season}, week={week})")
            return rows

    try:
        from .retry_utils import CircuitBreakerError, retry_with_backoff
        # Use retry with circuit breaker for snap fetches
        return await retry_with_backoff(
            _fetch,
            circuit_breaker_name="sleeper_snaps"
        )
    except CircuitBreakerError as e:
        logger.warning(f"[Fetch Snaps] Circuit breaker open: {e}")
        return []
    except Exception as e:
        logger.error(f"[Fetch Snaps] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []

async def _fetch_week_schedule(season: int, week: int, force: bool = False):
    """Fetch weekly schedule from ESPN scoreboard API (best-effort).

    Returns list of bidirectional game rows for upsert_schedule_games.
    If advanced enrichment disabled or failure occurs, returns empty list.

    Args:
        season: NFL season year.
        week: Regular-season week (1-18).
        force: Fetch even when NFL_MCP_ADVANCED_ENRICH is off. Used by callers
            (e.g. strength-of-schedule) for which the schedule *is* the product,
            not opportunistic enrichment.

    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not ADVANCED_ENRICH_ENABLED and not force:
        logger.debug("[Fetch Schedule] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info(f"[Fetch Schedule] Starting fetch for season={season}, week={week}")

    async def _fetch():
        # Regular season scoreboard: seasontype=2
        url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&year={season}&seasontype=2"

        async with create_http_client() as client:
            resp = await client.get(url, timeout=DEFAULT_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"[Fetch Schedule] ESPN API returned status {resp.status_code}")
                return []
            data = resp.json() or {}
            events = data.get("events") or []

            logger.debug(f"[Fetch Schedule] Received {len(events)} events from ESPN")
            games = []
            for ev in events:
                comps = ev.get("competitions") or []
                kickoff = ev.get("date")
                for comp in comps:
                    competitors = comp.get("competitors") or []
                    if len(competitors) != 2:
                        continue
                    home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                    away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])
                    h_abbr = (home.get("team") or {}).get("abbreviation")
                    a_abbr = (away.get("team") or {}).get("abbreviation")
                    if not h_abbr or not a_abbr:
                        continue
                    games.append({"season": season, "week": week, "team": h_abbr, "opponent": a_abbr, "is_home": 1, "kickoff": kickoff, "raw": ev})
                    games.append({"season": season, "week": week, "team": a_abbr, "opponent": h_abbr, "is_home": 0, "kickoff": kickoff, "raw": ev})

            # Validate response
            from .response_validation import validate_response_and_log, validate_schedule_response
            if not validate_response_and_log(games, validate_schedule_response, "Schedule", allow_partial=True):
                logger.error("[Fetch Schedule] Response validation failed, returning empty list")
                return []

            logger.info(f"[Fetch Schedule] Successfully fetched {len(games)} game records ({len(events)} events, season={season}, week={week})")
            return games

    try:
        from .retry_utils import CircuitBreakerError, retry_with_backoff
        # Use retry with circuit breaker for schedule fetches
        return await retry_with_backoff(
            _fetch,
            circuit_breaker_name="espn_schedule"
        )
    except CircuitBreakerError as e:
        logger.warning(f"[Fetch Schedule] Circuit breaker open: {e}")
        return []
    except Exception as e:
        logger.error(f"[Fetch Schedule] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []

async def _fetch_all_team_schedules(season: int):
    """Fetch full season schedules for all 32 NFL teams from ESPN Team Schedule API.

    This prefetches complete schedules (all weeks) for every team to warm the cache.
    Useful for startup/initial cache population.

    Args:
        season: Season year (e.g., 2026)

    Returns:
        List of game dicts for upsert_schedule_games (bidirectional rows)
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug("[Fetch All Schedules] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    # All 32 NFL team abbreviations
    nfl_teams = [
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
        "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
        "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
        "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WSH"
    ]

    logger.info(f"[Fetch All Schedules] Starting fetch for {len(nfl_teams)} teams (season={season})")

    all_games = []
    successful_teams = 0
    failed_teams = []

    async with create_http_client() as client:
        for team_abbr in nfl_teams:
            try:
                url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_abbr}/schedule?season={season}"
                resp = await client.get(url, timeout=DEFAULT_TIMEOUT)

                if resp.status_code != 200:
                    logger.warning(f"[Fetch All Schedules] Team {team_abbr}: ESPN API returned status {resp.status_code}")
                    failed_teams.append(team_abbr)
                    continue

                data = resp.json() or {}
                events = data.get("events", [])

                team_games = []
                for event in events:
                    # Extract week and kickoff
                    week_info = event.get("week", {})
                    week = week_info.get("number") if week_info else None
                    kickoff = event.get("date")

                    # Extract competitions
                    competitions = event.get("competitions", [])
                    if not competitions:
                        continue

                    competition = competitions[0]
                    competitors = competition.get("competitors", [])

                    if len(competitors) != 2:
                        continue

                    # Find home and away teams
                    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
                    away = next((c for c in competitors if c.get("homeAway") == "away"), None)

                    if not home or not away:
                        continue

                    h_abbr = (home.get("team") or {}).get("abbreviation")
                    a_abbr = (away.get("team") or {}).get("abbreviation")

                    if not h_abbr or not a_abbr or not week:
                        continue

                    # Create bidirectional game records
                    team_games.append({
                        "season": season,
                        "week": week,
                        "team": h_abbr,
                        "opponent": a_abbr,
                        "is_home": 1,
                        "kickoff": kickoff,
                        "raw": event
                    })
                    team_games.append({
                        "season": season,
                        "week": week,
                        "team": a_abbr,
                        "opponent": h_abbr,
                        "is_home": 0,
                        "kickoff": kickoff,
                        "raw": event
                    })

                all_games.extend(team_games)
                successful_teams += 1
                logger.debug(f"[Fetch All Schedules] Team {team_abbr}: {len(team_games)} game records ({len(events)} events)")

            except Exception as e:
                logger.warning(f"[Fetch All Schedules] Team {team_abbr}: Failed - {e}")
                failed_teams.append(team_abbr)

    logger.info(
        f"[Fetch All Schedules] Completed: {successful_teams}/{len(nfl_teams)} teams successful, "
        f"{len(all_games)} total game records fetched"
    )

    if failed_teams:
        logger.warning(f"[Fetch All Schedules] Failed teams: {', '.join(failed_teams)}")

    return all_games


async def _fetch_injuries():
    """Fetch injury reports from ESPN for all NFL teams.

    Returns list of dicts with keys: player_id, player_name, team_id, position,
    injury_status, injury_type, injury_description, date_reported.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug("[Fetch Injuries] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info("[Fetch Injuries] Starting fetch for all teams")

    # NFL team abbreviations
    teams = [
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
        "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
        "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
        "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WSH"  # WSH (not WAS) for Washington
    ]

    all_injuries = []

    try:
        import re

        import httpx

        from .config import create_http_client, get_http_headers

        headers = get_http_headers("nfl_teams")

        async with create_http_client() as client:
            for team in teams:
                try:
                    page = 1
                    page_count = 1  # Will be updated from first response
                    team_injuries = []

                    # Fetch all pages for this team
                    # Note: ESPN Core API returns items as $ref URLs only
                    # Removed 10-injury limit - ESPN typically returns 15-25 max anyway
                    max_injuries_per_team = 50  # Reasonable limit while allowing full data
                    injuries_fetched = 0

                    while page <= page_count and injuries_fetched < max_injuries_per_team:
                        url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{team}/injuries?limit=50&page={page}"
                        resp = await client.get(url, headers=headers)

                        if resp.status_code != 200:
                            logger.debug(f"[Fetch Injuries] Team {team} page {page}: status {resp.status_code}")
                            break

                        data = resp.json()

                        # Update page count from first response
                        if page == 1:
                            page_count = data.get('pageCount', 1)

                            # DEBUG: Log first team's response to understand structure
                            if team == teams[0]:
                                logger.info(f"[DEBUG Injuries] {team} response keys: {list(data.keys())}")
                                logger.info(f"[DEBUG Injuries] {team} count: {data.get('count', 'N/A')}")
                                logger.info(f"[DEBUG Injuries] {team} pageCount: {page_count}")
                                logger.info(f"[DEBUG Injuries] {team} page 1 items length: {len(data.get('items', []))}")

                        injuries_data = data.get('items', [])

                        # ESPN Core API v2 returns items as $ref URLs only
                        # We need to fetch each injury detail separately
                        for injury_ref in injuries_data:
                            try:
                                # Each item is just {"$ref": "url"}
                                injury_url = injury_ref.get('$ref')
                                if not injury_url:
                                    continue

                                # Fetch the actual injury details
                                injury_resp = await client.get(injury_url, headers=headers)
                                if injury_resp.status_code != 200:
                                    continue

                                injury_item = injury_resp.json()

                                # Extract athlete info from the injury details
                                athlete_ref = injury_item.get('athlete', {})
                                if not athlete_ref:
                                    continue

                                # Athlete is also a $ref, so we need to extract from URL or fetch it
                                athlete_url = athlete_ref.get('$ref', '')
                                # Extract athlete ID from URL: .../athletes/4428633/...
                                athlete_id_match = re.search(r'/athletes/(\d+)/', athlete_url)
                                if not athlete_id_match:
                                    continue

                                player_id = athlete_id_match.group(1)

                                # Get player name - might need to fetch athlete details
                                player_name = athlete_ref.get('displayName')
                                if not player_name:
                                    # Try fetching athlete details
                                    try:
                                        athlete_detail_resp = await client.get(athlete_url, headers=headers)
                                        if athlete_detail_resp.status_code == 200:
                                            athlete_detail = athlete_detail_resp.json()
                                            player_name = athlete_detail.get('displayName', 'Unknown')
                                        else:
                                            player_name = 'Unknown'
                                    except (httpx.HTTPError, json.JSONDecodeError, KeyError, AttributeError):
                                        player_name = 'Unknown'

                                # Status and type are nested objects
                                status_data = injury_item.get('status', {})
                                type_data = injury_item.get('type', {})

                                # Normalize status and calculate severity
                                raw_status = status_data if isinstance(status_data, str) else status_data.get('description', 'Unknown')
                                from .injury_service import InjuryAggregator
                                normalized_status = InjuryAggregator.normalize_status(raw_status)
                                severity = InjuryAggregator.get_severity(normalized_status)

                                injury = {
                                    'player_id': str(player_id),
                                    'player_name': player_name,
                                    'team_id': team,
                                    'position': None,  # Not available in injury endpoint
                                    'injury_status': normalized_status,
                                    'injury_type': type_data.get('name') if isinstance(type_data, dict) else None,
                                    'injury_description': injury_item.get('shortComment') or injury_item.get('longComment'),
                                    'severity': severity,
                                    'confidence': 60,  # Single source (ESPN)
                                    'sources': ['ESPN'],
                                    'date_reported': injury_item.get('date')
                                }
                                team_injuries.append(injury)
                                injuries_fetched += 1

                                # Stop if we've reached the limit per team
                                if injuries_fetched >= max_injuries_per_team:
                                    break

                            except Exception as e:
                                logger.debug(f"[Fetch Injuries] Failed to fetch injury detail: {e}")
                                continue

                        # Move to next page
                        page += 1

                    # Add all injuries from this team
                    all_injuries.extend(team_injuries)

                except Exception as e:
                    logger.debug(f"[Fetch Injuries] Team {team} failed: {e}")
                    continue

        logger.info(f"[Fetch Injuries] Successfully fetched {len(all_injuries)} injury records across {len(teams)} teams")
        return all_injuries

    except Exception as e:
        logger.error(f"[Fetch Injuries] Failed: {e}", exc_info=True)
        return []

async def _fetch_practice_reports(season: int, week: int):
    """Fetch practice status reports (DNP/LP/FP) from ESPN injuries endpoint.

    Returns list of dicts with keys: player_id, date, status, source.

    Note: This uses the injuries endpoint which includes practice participation status.
    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug("[Fetch Practice] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info(f"[Fetch Practice] Starting fetch for season={season}, week={week}")

    async def _fetch():
        # Use injury reports as source for practice status
        # Practice status is often reflected in injury reports (DNP/Limited/Full)
        injuries = await _fetch_injuries()

        if not injuries:
            logger.warning("[Fetch Practice] No injury data available to extract practice status")
            return []

        # Convert injury status to practice status format
        practice_reports = []
        now = datetime.now(UTC).isoformat()

        for inj in injuries:
            status = inj.get('injury_status', '').upper()

            # Map injury status to practice participation
            practice_status = None
            if 'OUT' in status or 'RESERVE' in status or 'PUP' in status:
                practice_status = 'DNP'  # Did Not Participate
            elif 'DOUBTFUL' in status or 'LIMITED' in status:
                practice_status = 'LP'   # Limited Participation
            elif 'QUESTIONABLE' in status:
                practice_status = 'LP'   # Usually limited
            elif 'PROBABLE' in status or 'FULL' in status:
                practice_status = 'FP'   # Full Participation

            if practice_status:
                practice_reports.append({
                    'player_id': inj.get('player_id'),
                    'date': inj.get('date_reported', now[:10]),  # YYYY-MM-DD
                    'status': practice_status,
                    'source': 'espn_injuries'
                })

        # Validate response
        from .response_validation import (
            validate_practice_report_response,
            validate_response_and_log,
        )
        if not validate_response_and_log(practice_reports, validate_practice_report_response, "Practice", allow_partial=True):
            logger.error("[Fetch Practice] Response validation failed, returning empty list")
            return []

        logger.info(f"[Fetch Practice] Extracted {len(practice_reports)} practice status records from {len(injuries)} injuries")
        return practice_reports

    try:
        from .retry_utils import CircuitBreakerError, retry_with_backoff
        # Use retry with circuit breaker for practice fetches
        return await retry_with_backoff(
            _fetch,
            circuit_breaker_name="espn_practice"
        )
    except CircuitBreakerError as e:
        logger.warning(f"[Fetch Practice] Circuit breaker open: {e}")
        return []
    except Exception as e:
        logger.error(f"[Fetch Practice] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []

async def _fetch_weekly_usage_stats(season: int, week: int):
    """Fetch weekly usage statistics (targets, routes, RZ touches) from available sources.

    Returns list of dicts for upsert_usage_stats.
    Attempts Sleeper stats first, falls back to ESPN if needed.
    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug("[Fetch Usage] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info(f"[Fetch Usage] Starting fetch for season={season}, week={week}")

    async def _fetch():
        # Try Sleeper weekly stats endpoint first
        headers = get_http_headers("sleeper_week_stats")
        url = f"https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}"

        async with create_http_client() as client:
            resp = await client.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json() or {}
                if isinstance(data, dict):
                    logger.debug(f"[Fetch Usage] Received data for {len(data)} players")
                    stats = []
                    for pid, player_stats in list(data.items())[:3000]:  # cap
                        if not isinstance(player_stats, dict):
                            continue
                        # Extract usage fields (naming varies by API)
                        # Use explicit None checks to handle 0 values correctly
                        targets = player_stats.get("rec_tgt")
                        if targets is None:
                            targets = player_stats.get("targets")

                        # Routes should only be actual routes run, not snap count
                        # Try multiple possible field names for routes data
                        routes = player_stats.get("routes_run")
                        routes_field_used = None
                        if routes is not None:
                            routes_field_used = "routes_run"
                        elif (routes := player_stats.get("routes")) is not None:
                            routes_field_used = "routes"
                        elif (routes := player_stats.get("rec_routes")) is not None:
                            routes_field_used = "rec_routes"
                        elif (routes := player_stats.get("pass_routes")) is not None:
                            routes_field_used = "pass_routes"
                        elif (routes := player_stats.get("receiving_routes")) is not None:
                            routes_field_used = "receiving_routes"

                        # Log diagnostic info for routes field detection (sample first 5 players)
                        if len(stats) < 5:
                            if routes is not None:
                                logger.debug(f"[Fetch Usage] Player {pid}: routes={routes} from field '{routes_field_used}'")
                            else:
                                # Check what fields ARE available for this player
                                available_fields = list(player_stats.keys())[:10]  # Sample fields
                                logger.debug(f"[Fetch Usage] Player {pid}: routes=None, available fields: {available_fields}")

                        # Calculate RZ touches from multiple sources
                        # Try multiple field names for better API compatibility
                        # Use explicit None checks to preserve 0 values
                        rz_tgt = player_stats.get("rec_tgt_rz")
                        if rz_tgt is None:
                            rz_tgt = player_stats.get("rec_targets_rz")
                        if rz_tgt is None:
                            rz_tgt = player_stats.get("redzone_targets")
                        if rz_tgt is None:
                            rz_tgt = 0

                        rz_rush = player_stats.get("rush_att_rz")
                        if rz_rush is None:
                            rz_rush = player_stats.get("rush_attempts_rz")
                        if rz_rush is None:
                            rz_rush = player_stats.get("redzone_rushes")
                        if rz_rush is None:
                            rz_rush = player_stats.get("redzone_rush_attempts")
                        if rz_rush is None:
                            rz_rush = 0

                        rz_touches = rz_tgt + rz_rush

                        # If no explicit RZ data, estimate from TDs (TDs often happen in RZ)
                        if rz_touches == 0:
                            rec_td = player_stats.get("rec_td", 0)
                            rush_td = player_stats.get("rush_td", 0)
                            td_total = rec_td + rush_td

                            if td_total > 0:
                                rz_touches = td_total
                            else:
                                # Truly 0 or data missing
                                pass

                        # Calculate total touches
                        rush_att = player_stats.get("rush_att", 0)
                        receptions = player_stats.get("rec", 0)
                        touches = rush_att + receptions

                        # Air yards - preserve 0 values
                        air_yards = player_stats.get("rec_air_yds")
                        if air_yards is None:
                            air_yards = player_stats.get("air_yards")

                        # Get snap percentage - try multiple field names and calculation methods
                        # Use explicit None checks to preserve 0 values
                        snap_share = player_stats.get("snap_pct")
                        if snap_share is None:
                            snap_share = player_stats.get("off_snp_pct")
                        if snap_share is None:
                            snap_share = player_stats.get("snap_share")
                        if snap_share is None:
                            snap_share = player_stats.get("snap_percentage")
                        if snap_share is None:
                            snap_share = player_stats.get("snaps_pct")

                        # Calculate from absolute snaps if percentage not provided
                        if snap_share is None:
                            off_snp = player_stats.get("off_snp")
                            team_snp = player_stats.get("team_snp")
                            if team_snp is None:
                                team_snp = player_stats.get("tm_off_snp")

                            if off_snp is not None and team_snp is not None and team_snp > 0:
                                snap_share = round((off_snp / team_snp) * 100, 1)
                            else:
                                pass

                        # Only include if at least one usage metric present
                        if any([targets, routes, rz_touches, touches]):
                            stats.append({
                                "player_id": str(pid),
                                "season": season,
                                "week": week,
                                "targets": targets,
                                "routes": routes,
                                "rz_touches": rz_touches,
                                "touches": touches,
                                "air_yards": air_yards,
                                "snap_share": snap_share
                            })

                    if stats:
                        # Validate response
                        from .response_validation import (
                            validate_response_and_log,
                            validate_usage_stats_response,
                        )
                        if not validate_response_and_log(stats, validate_usage_stats_response, "Usage", allow_partial=True):
                            logger.error("[Fetch Usage] Response validation failed, returning empty list")
                            return []

                        # Log diagnostic summary about routes data availability
                        routes_available = sum(1 for s in stats if s.get("routes") is not None)
                        routes_zero = sum(1 for s in stats if s.get("routes") == 0)
                        routes_none = sum(1 for s in stats if s.get("routes") is None)
                        logger.info(
                            f"[Fetch Usage] Successfully fetched {len(stats)} usage records "
                            f"(season={season}, week={week}). "
                            f"Routes data: {routes_available} with data "
                            f"({routes_zero} with 0, {routes_none} with None)"
                        )
                        return stats
                    else:
                        logger.warning("[Fetch Usage] No valid usage stats found in response")
            else:
                logger.warning(f"[Fetch Usage] Sleeper API returned status {resp.status_code}")

        # Fallback: ESPN (limited coverage, best-effort)
        # Note: ESPN player stats API may require iterating by position or fetching league leaders
        # For simplicity, return empty list (can be extended later)
        logger.warning(f"[Fetch Usage] No usage stats available from any source for season={season}, week={week}")
        return []

    try:
        from .retry_utils import CircuitBreakerError, retry_with_backoff
        # Use retry with circuit breaker for usage fetches
        return await retry_with_backoff(
            _fetch,
            circuit_breaker_name="sleeper_usage"
        )
    except CircuitBreakerError as e:
        logger.warning(f"[Fetch Usage] Circuit breaker open: {e}")
        return []
    except Exception as e:
        logger.error(f"[Fetch Usage] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []

def _estimate_snap_pct(depth_rank: int | None, position: str | None = None) -> float | None:
    """Estimate snap percentage based on depth chart and position.

    Different positions have different snap count patterns:
    - QBs: Starters play 95%+, backups rarely play
    - RBs: Heavy rotation/committees, starters ~55%
    - WRs: Top receivers play 85%+, backups 50%
    - TEs: Varies by blocking role, starters ~65%

    Args:
        depth_rank: Depth chart position (1=starter, 2=backup, 3=third string, etc.)
        position: Player position (QB, RB, WR, TE, etc.)

    Returns:
        Estimated snap percentage or None if cannot estimate
    """
    if depth_rank is None:
        return None

    # Position-specific estimates for starters
    if depth_rank == 1:
        position_estimates = {
            "QB": 95.0,  # QBs rarely rotate unless blowout
            "RB": 55.0,  # RBs often in committees
            "WR": 85.0,  # #1 WRs play most snaps
            "TE": 65.0,  # TEs vary by blocking role
        }
        return position_estimates.get(position, 70.0)  # Default 70% for unknown positions

    # Backups (depth 2)
    elif depth_rank == 2:
        position_estimates = {
            "QB": 5.0,   # Backup QBs rarely see the field
            "RB": 35.0,  # Backup RBs get carries in rotation
            "WR": 50.0,  # #2 WRs get decent playing time
            "TE": 40.0,  # Backup TEs mostly situational
        }
        return position_estimates.get(position, 45.0)

    # Third string or lower
    else:
        return 15.0  # Limited snaps for depth pieces regardless of position

def _calculate_usage_trend(weekly_data: list[dict], metric: str) -> str | None:
    """Calculate trend direction for a usage metric over recent weeks.

    Args:
        weekly_data: List of week dicts ordered by week DESC (most recent first)
        metric: Name of the metric to analyze (targets, routes, rz_touches, snap_share)

    Returns:
        "up" if trending upward, "down" if trending downward, "flat" if stable, None if insufficient data
    """
    if not weekly_data or len(weekly_data) < 2:
        return None

    # Extract values for the metric (ignore None values)
    values = []
    for week_data in weekly_data:
        val = week_data.get(metric)
        if val is not None:
            values.append(float(val))

    if len(values) < 2:
        return None

    # Compare most recent week vs average of prior weeks
    most_recent = values[0]
    prior_avg = sum(values[1:]) / len(values[1:])

    # Calculate percentage change
    if prior_avg == 0:
        # If prior average is 0, any positive value is "up"
        return "up" if most_recent > 0 else "flat"

    pct_change = ((most_recent - prior_avg) / prior_avg) * 100

    # Threshold for significant change: 15%
    if pct_change > 15:
        return "up"
    elif pct_change < -15:
        return "down"
    else:
        return "flat"

def _enrich_usage_and_opponent(nfl_db, athlete: dict, season: int | None, week: int | None) -> dict:
    """Add snap_pct/opponent fields to a base enrichment object (mutates and returns)."""
    if not athlete:
        return {}

    enriched_additions: dict = {}
    position = athlete.get("position")
    player_id = athlete.get("id") or athlete.get("player_id")
    player_name = athlete.get("full_name") or athlete.get("name") or f"Player-{player_id}"

    logger.debug(f"[Enrichment] Processing {player_name} (id={player_id}, pos={position}, season={season}, week={week})")

    # Snap pct (non-DEF) - try current week, fallback to previous week
    if season and week and position not in (None, "DEF") and hasattr(nfl_db, 'get_player_snap_pct'):
        row = nfl_db.get_player_snap_pct(player_id, season, week)
        snap_week_used = week

        # If current week has no data, try previous week (games may not have been played yet)
        if (not row or row.get("snap_pct") is None) and week > 1:
            row = nfl_db.get_player_snap_pct(player_id, season, week - 1)
            snap_week_used = week - 1
            logger.debug(f"[Enrichment] {player_name}: Current week {week} has no snaps, trying week {week - 1}")

        if row and row.get("snap_pct") is not None:
            enriched_additions["snap_pct"] = row.get("snap_pct")
            enriched_additions["snap_pct_source"] = "cached"
            enriched_additions["snap_pct_week"] = snap_week_used  # Track which week was used
            logger.debug(f"[Enrichment] {player_name}: snap_pct={row.get('snap_pct')}% (cached from week {snap_week_used})")
        else:
            depth_rank = None
            raw_field = athlete.get("raw")
            if isinstance(raw_field, dict):
                depth_rank = raw_field.get("depth_chart_order")
            est = _estimate_snap_pct(depth_rank, position)  # Pass position for better estimates
            if est is not None:
                enriched_additions["snap_pct"] = est
                enriched_additions["snap_pct_source"] = "estimated"
                logger.debug(f"[Enrichment] {player_name}: snap_pct={est}% (estimated from depth={depth_rank}, pos={position})")

    # Opponent for ALL positions (all positions use team_id)
    if season and week and hasattr(nfl_db, 'get_opponent'):
        # All positions use team_id (database only stores team_id, not team)
        team_key = athlete.get("team_id")

        if team_key:
            opponent = nfl_db.get_opponent(season, week, team_key)
            if opponent:
                enriched_additions["opponent"] = opponent
                enriched_additions["opponent_source"] = "cached"
                logger.debug(f"[Enrichment] {player_name} ({position}): opponent={opponent} (cached)")

    # Injury status - all positions
    if player_id and hasattr(nfl_db, 'get_player_injury_from_cache'):
        injury = nfl_db.get_player_injury_from_cache(player_id, max_age_hours=None)  # Adaptive TTL
        if injury:
            age_hours = (datetime.now(UTC) - datetime.fromisoformat(injury["updated_at"])).total_seconds() / 3600
            enriched_additions["injury_status"] = injury["injury_status"]
            enriched_additions["injury_type"] = injury.get("injury_type")
            enriched_additions["injury_description"] = injury.get("injury_description")
            enriched_additions["injury_date"] = injury.get("date_reported")
            enriched_additions["injury_age_hours"] = round(age_hours, 1)
            enriched_additions["injury_stale"] = age_hours > 12
            # New fields from injury service
            enriched_additions["injury_severity"] = injury.get("severity")
            enriched_additions["injury_confidence"] = injury.get("confidence", 50)
            enriched_additions["injury_sources"] = injury.get("sources", ["ESPN"])
            enriched_additions["injury_game_status"] = injury.get("game_status")
            logger.debug(f"[Enrichment] {player_name}: injury_status={injury['injury_status']} severity={injury.get('severity')} confidence={injury.get('confidence')} (age={round(age_hours, 1)}h)")

    # Practice status (DNP/LP/FP) - all positions
    # Always try to provide a practice_status value
    practice_status_set = False

    if player_id and hasattr(nfl_db, 'get_latest_practice_status'):
        practice = nfl_db.get_latest_practice_status(player_id, max_age_hours=72)
        if practice:
            age_hours = (datetime.now(UTC) - datetime.fromisoformat(practice["updated_at"])).total_seconds() / 3600
            enriched_additions["practice_status"] = practice["status"]
            enriched_additions["practice_status_date"] = practice["date"]
            enriched_additions["practice_status_age_hours"] = round(age_hours, 1)
            enriched_additions["practice_status_stale"] = age_hours > 72
            enriched_additions["practice_status_source"] = "cached"
            logger.debug(f"[Enrichment] {player_name}: practice_status={practice['status']} (age={round(age_hours, 1)}h)")
            practice_status_set = True

    # If no cached practice status, derive from injury or default to FP
    if not practice_status_set:
        injury_status = enriched_additions.get("injury_status", "").upper()
        if injury_status:
            # Derive practice status from injury status
            if 'OUT' in injury_status or 'RESERVE' in injury_status or 'PUP' in injury_status:
                derived_status = 'DNP'  # Did Not Participate
            elif 'DOUBTFUL' in injury_status or 'LIMITED' in injury_status:
                derived_status = 'LP'   # Limited Participation
            elif 'QUESTIONABLE' in injury_status:
                derived_status = 'LP'   # Usually limited
            elif 'PROBABLE' in injury_status or 'FULL' in injury_status:
                derived_status = 'FP'   # Full Participation
            else:
                derived_status = 'FP'   # Default to full if injury status is unclear

            enriched_additions["practice_status"] = derived_status
            enriched_additions["practice_status_source"] = "derived_from_injury"
            logger.debug(f"[Enrichment] {player_name}: practice_status={derived_status} (derived from injury_status={injury_status})")
        else:
            # No injury, no practice status -> assume healthy and fully practicing
            enriched_additions["practice_status"] = "FP"
            enriched_additions["practice_status_source"] = "default_healthy"
            logger.debug(f"[Enrichment] {player_name}: practice_status=FP (default - no injury)")

    # Usage stats (targets, routes, RZ touches) - offensive skill positions
    if season and week and position in ("WR", "RB", "TE") and hasattr(nfl_db, 'get_usage_last_n_weeks'):
        usage = nfl_db.get_usage_last_n_weeks(player_id, season, week, n=3)
        if usage:
            enriched_additions["usage_last_3_weeks"] = {
                "targets_avg": round(usage["targets_avg"], 1) if usage["targets_avg"] is not None else None,
                "routes_avg": round(usage["routes_avg"], 1) if usage["routes_avg"] is not None else None,
                "rz_touches_avg": round(usage["rz_touches_avg"], 1) if usage["rz_touches_avg"] is not None else None,
                "snap_share_avg": round(usage["snap_share_avg"], 1) if usage["snap_share_avg"] is not None else None,
                "weeks_sample": usage["weeks_sample"]
            }
            enriched_additions["usage_source"] = "sleeper"
            logger.debug(
                f"[Enrichment] {player_name}: usage_last_3wks="
                f"tgt={usage['targets_avg'] or 0:.1f}, routes={usage['routes_avg'] or 0:.1f}, "
                f"rz={usage['rz_touches_avg'] or 0:.1f} (n={usage['weeks_sample']})"
            )

            # Add trend calculation if we have weekly breakdown
            if hasattr(nfl_db, 'get_usage_weekly_breakdown'):
                weekly_breakdown = nfl_db.get_usage_weekly_breakdown(player_id, season, week, n=3)
                if weekly_breakdown and len(weekly_breakdown) >= 2:
                    # Calculate trends for key metrics
                    targets_trend = _calculate_usage_trend(weekly_breakdown, "targets")
                    routes_trend = _calculate_usage_trend(weekly_breakdown, "routes")
                    snap_trend = _calculate_usage_trend(weekly_breakdown, "snap_share")

                    # Add trend to enrichment if at least one metric has a trend
                    if targets_trend or routes_trend or snap_trend:
                        enriched_additions["usage_trend"] = {
                            "targets": targets_trend,
                            "routes": routes_trend,
                            "snap_share": snap_trend
                        }
                        # Overall trend (prioritize targets for skill positions)
                        overall_trend = targets_trend or snap_trend or routes_trend
                        if overall_trend:
                            enriched_additions["usage_trend_overall"] = overall_trend
                            logger.debug(f"[Enrichment] {player_name}: usage_trend={overall_trend}")

    # Matchup difficulty analysis - QB, RB, WR, TE only
    opponent = enriched_additions.get("opponent")
    if opponent and position in ("QB", "RB", "WR", "TE"):
        try:
            from .matchup_tools import get_defense_analyzer
            analyzer = get_defense_analyzer()

            # Get matchup difficulty (synchronous - uses cached rankings)
            matchup = analyzer.get_matchup_difficulty(position, opponent)

            if matchup and not matchup.get("is_fallback", True):
                enriched_additions["matchup_rank"] = matchup.get("rank")
                enriched_additions["matchup_tier"] = matchup.get("matchup_tier")
                enriched_additions["matchup_indicator"] = matchup.get("tier_indicator")
                enriched_additions["matchup_recommendation"] = matchup.get("recommendation")
                enriched_additions["defense_pts_allowed_avg"] = matchup.get("points_allowed_avg")
                logger.debug(
                    f"[Enrichment] {player_name}: matchup vs {opponent} = "
                    f"{matchup.get('matchup_tier')} (#{matchup.get('rank')})"
                )
            else:
                # Use fallback data but still add basic matchup info
                enriched_additions["matchup_rank"] = matchup.get("rank", 16)
                enriched_additions["matchup_tier"] = matchup.get("matchup_tier", "neutral")
                enriched_additions["matchup_indicator"] = matchup.get("tier_indicator", "🟡")
                enriched_additions["matchup_source"] = "fallback"
                logger.debug(f"[Enrichment] {player_name}: matchup vs {opponent} = neutral (fallback)")
        except Exception as e:
            logger.debug(f"[Enrichment] {player_name}: matchup analysis failed: {e}")

    # Vegas lines game environment analysis - QB, RB, WR, TE only
    team = athlete.get("team")
    if team and position in ("QB", "RB", "WR", "TE"):
        try:
            from .vegas_tools import get_vegas_analyzer
            vegas = get_vegas_analyzer()

            # Get game lines for the team (synchronous - uses cached lines)
            game = vegas.get_game_lines(team)

            if game and not game.get("is_fallback", True):
                # Determine if home or away
                team_norm = vegas._normalize_team(team)
                is_home = game.get("home_team") == team_norm

                # Get team-specific implied total
                implied_total = game.get("home_implied_total") if is_home else game.get("away_implied_total")
                spread = game.get("home_spread") if is_home else game.get("away_spread", 0)

                # Add Vegas data
                enriched_additions["game_total"] = game.get("total")
                enriched_additions["implied_team_total"] = implied_total
                enriched_additions["spread"] = spread

                # Game environment
                env = game.get("game_environment", {})
                enriched_additions["game_environment"] = env.get("tier", "average")
                enriched_additions["game_environment_indicator"] = env.get("indicator", "➡️")

                # Position-specific boost indicator
                if position == "QB":
                    enriched_additions["vegas_boost"] = env.get("qb_boost", "0%")
                elif position in ("WR", "TE"):
                    enriched_additions["vegas_boost"] = env.get("pass_catchers_boost", "0%")
                elif position == "RB":
                    enriched_additions["vegas_boost"] = env.get("rb_boost", "0%")

                logger.debug(
                    f"[Enrichment] {player_name}: Vegas O/U={game.get('total')}, "
                    f"implied={implied_total}, env={env.get('tier')}"
                )
            else:
                # Fallback - still provide basic neutral data
                enriched_additions["game_total"] = 45.0
                enriched_additions["implied_team_total"] = 22.5
                enriched_additions["game_environment"] = "average"
                enriched_additions["game_environment_indicator"] = "➡️"
                enriched_additions["vegas_source"] = "fallback"
                logger.debug(f"[Enrichment] {player_name}: Vegas data unavailable (fallback)")
        except Exception as e:
            logger.debug(f"[Enrichment] {player_name}: Vegas analysis failed: {e}")

    if enriched_additions:
        logger.info(f"[Enrichment] {player_name}: Added {len(enriched_additions)} enrichment fields")

    return enriched_additions
