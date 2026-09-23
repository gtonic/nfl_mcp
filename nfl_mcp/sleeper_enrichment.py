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
from .game_clock import progress_of, week_games
from .injury_match import sleeper_injury_status
from .injury_service import worst_status
from .teams import normalize_team

logger = logging.getLogger(__name__)


ADVANCED_ENRICH_ENABLED = os.getenv("NFL_MCP_ADVANCED_ENRICH") == "1"


def advanced_enrich_enabled() -> bool:
    """Whether the advanced-enrichment fetchers may run.

    Read lazily rather than trusting the import-time constant: ``server.py``
    imports the tool registry (and therefore this module) *before* it loads
    ``.env``, so a flag set only in ``.env`` would otherwise be missed for the
    whole process. The module attribute still wins when set, which keeps
    ``monkeypatch.setattr(..., ADVANCED_ENRICH_ENABLED, True)`` working.
    """
    return ADVANCED_ENRICH_ENABLED or os.getenv("NFL_MCP_ADVANCED_ENRICH") == "1"


def _first_present(stats: dict, *keys: str):
    """First key present with a non-None value, else None.

    Field naming varies across the upstream feeds, so callers probe several
    spellings. Plain ``or`` chaining would discard a legitimate 0.
    """
    for key in keys:
        value = stats.get(key)
        if value is not None:
            return value
    return None


async def _fetch_week_player_snaps(season: int, week: int):
    """Fetch player snap stats (best-effort) from Sleeper weekly stats endpoint.

    Returns list of dicts for upsert_player_week_stats. If advanced enrichment disabled
    or network/API issues occur, returns empty list.

    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not advanced_enrich_enabled():
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
                # Attempt to extract snaps & snap_pct fields (naming may vary).
                # Sleeper uses 'off_snp' / 'tm_off_snp'. Explicit None checks so a
                # legitimate 0 (dressed but never on the field) survives.
                snaps = _first_present(stats, "snaps", "off_snp", "off_snaps", "offense_snaps")
                team_snaps = _first_present(
                    stats, "team_snaps", "tm_off_snp", "off_team_snaps", "team_snp"
                )
                # Sleeper publishes no snap percentage, so derive it from the two
                # counts — the same calculation the usage fetcher already does.
                snap_pct = _first_present(stats, "snap_pct", "off_snp_pct", "off_snap_pct")
                if snap_pct is None and snaps is not None and team_snaps:
                    snap_pct = round(snaps / team_snaps * 100, 1)
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
    if not advanced_enrich_enabled() and not force:
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
    if not advanced_enrich_enabled():
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
    """Fetch injury reports for all NFL teams.

    Returns list of dicts with keys: player_id, player_name, team_id, position,
    injury_status, injury_type, injury_description, game_status, severity,
    confidence, sources, date_reported.

    Delegates to ``injury_service``, which fetches teams and injury details
    concurrently, caches athlete names and merges ESPN with CBS. This module
    used to carry its own sequential copy of that crawl; against the live API it
    took 589s for 1600 single-source records where the service needs 45s for
    1906 multi-source ones. Keeping two implementations is also what let one of
    them silently break — the copy extracted athlete ids with a pattern that
    never matched and returned nothing at all for months.

    ``db`` is deliberately not passed: the callers own persistence, so letting
    the service cache as well would write every record twice.
    """
    if not advanced_enrich_enabled():
        logger.debug("[Fetch Injuries] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info("[Fetch Injuries] Starting fetch for all teams")

    from .injury_service import get_injury_reports

    try:
        injuries = await get_injury_reports(teams=None, db=None, use_cache=False)
        logger.info(f"[Fetch Injuries] Successfully fetched {len(injuries)} injury records")
        return injuries
    except Exception as e:
        logger.error(f"[Fetch Injuries] Failed: {e}", exc_info=True)
        return []


async def _fetch_practice_reports(season: int, week: int, db=None):
    """Fetch this week's *real* practice reports (DNP/LP/FP) per report day.

    Returns dicts for ``upsert_practice_status``: player_name, team, date,
    status, source ("nfl.com" official report, "espn_news" blurbs), plus the
    report text and game designation. See ``practice_reports``.

    This used to translate injury designations into practice lines
    (Questionable -> LP, Out -> DNP) and store them as if reported. A player
    with no report now simply has no row.

    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not advanced_enrich_enabled():
        logger.debug("[Fetch Practice] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info(f"[Fetch Practice] Starting fetch for season={season}, week={week}")

    async def _fetch():
        from .practice_reports import fetch_practice_reports
        practice_reports = await fetch_practice_reports(season, week, db=db)

        # Validate response
        from .response_validation import (
            validate_practice_report_response,
            validate_response_and_log,
        )
        if not validate_response_and_log(practice_reports, validate_practice_report_response, "Practice", allow_partial=True):
            logger.error("[Fetch Practice] Response validation failed, returning empty list")
            return []

        logger.info(f"[Fetch Practice] {len(practice_reports)} real practice rows")
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
    if not advanced_enrich_enabled():
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
                        # `rec_rz_tgt` / `rush_rz_att` are the names Sleeper's
                        # weekly stats actually ship; the older spellings below
                        # never matched, so every RZ count was a TD estimate.
                        rz_tgt = player_stats.get("rec_rz_tgt")
                        if rz_tgt is None:
                            rz_tgt = player_stats.get("rec_tgt_rz")
                        if rz_tgt is None:
                            rz_tgt = player_stats.get("rec_targets_rz")
                        if rz_tgt is None:
                            rz_tgt = player_stats.get("redzone_targets")
                        if rz_tgt is None:
                            rz_tgt = 0

                        rz_rush = player_stats.get("rush_rz_att")
                        if rz_rush is None:
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

                        # Air yards - preserve 0 values. Sleeper ships `rec_air_yd`
                        # (singular); the plural spellings never matched anything.
                        air_yards = player_stats.get("rec_air_yd")
                        if air_yards is None:
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

def _cached_defense_rankings(analyzer, nfl_db, season: int | None) -> dict | None:
    """Defense-vs-position rankings without a network call, or None.

    Enrichment is synchronous, so it cannot fetch. The analyzer's in-memory
    cache holds whatever a matchup tool fetched this session; the database
    holds what any earlier session persisted. Season-long averages only move
    once a week, so a week-old row is still the current one.
    """
    if not season:
        return None
    cached = getattr(analyzer, "_rankings_cache", {}).get(f"defense_rankings_{season}")
    if cached and isinstance(cached.get("data"), dict) and cached["data"]:
        # A placeholder table in memory must not shadow real rows on disk.
        placeholder = any(
            entry.get("is_fallback")
            for rows in cached["data"].values()
            for entry in (rows or [])
        )
        if not placeholder:
            return cached["data"]
    if hasattr(nfl_db, "get_defense_rankings"):
        rankings = nfl_db.get_defense_rankings(int(season), max_age_hours=24 * 7)
        if isinstance(rankings, dict) and rankings:
            return rankings
    return None


def _team_game_final(nfl_db, season: int, week: int, team: str | None) -> bool:
    """Whether ``team``'s game in ``week`` is over, from the cached kickoff
    and — where the stored event has it — ESPN's own game state.

    Unknown (no schedule, no kickoff, bye) counts as not final: reading the
    previous completed week is the recoverable error, a half-played game
    reported as a full snap share is not.
    """
    if not team or not hasattr(nfl_db, "get_week_kickoffs"):
        return False
    try:
        games = week_games(nfl_db, int(season), int(week))
    except Exception:
        return False
    return progress_of(games.get(normalize_team(team) or team)) >= 1.0


def _enrich_usage_and_opponent(nfl_db, athlete: dict, season: int | None, week: int | None) -> dict:
    """Add snap_pct/opponent fields to a base enrichment object (mutates and returns)."""
    if not athlete:
        return {}

    enriched_additions: dict = {}
    position = athlete.get("position")
    player_id = athlete.get("id") or athlete.get("player_id")
    player_name = athlete.get("full_name") or athlete.get("name") or f"Player-{player_id}"

    logger.debug(f"[Enrichment] Processing {player_name} (id={player_id}, pos={position}, season={season}, week={week})")

    # Snap pct (non-DEF) - the current week only once his game is final,
    # otherwise the previous week. A Thursday game in progress or an early
    # injury exit is not a snap share.
    if season and week and position not in (None, "DEF") and hasattr(nfl_db, 'get_player_snap_pct'):
        row = None
        snap_week_used = week
        if _team_game_final(nfl_db, season, week, athlete.get("team_id") or athlete.get("team")):
            row = nfl_db.get_player_snap_pct(player_id, season, week)

        # Current week unfinished or not ingested yet: use the previous week
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
            if isinstance(raw_field, str):
                # Stored as JSON text; the dict check alone never matched, so
                # the depth-chart estimate never ran.
                try:
                    raw_field = json.loads(raw_field)
                except (ValueError, TypeError):
                    raw_field = None
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

    # Injury status - all positions. Matched by name and team: the report
    # table's ids are ESPN's and `player_id` here is Sleeper's (see
    # `injury_match`).
    injury = None
    if hasattr(nfl_db, 'find_player_injury'):
        injury = nfl_db.find_player_injury(
            athlete.get("full_name"), athlete.get("team_id"), max_age_hours=None  # Adaptive TTL
        )
        if injury:
            # A report row with a null status or timestamp must not raise: the
            # caller's broad except would drop every other field for him too.
            try:
                age_hours = (
                    datetime.now(UTC) - datetime.fromisoformat(injury.get("updated_at"))
                ).total_seconds() / 3600
            except (TypeError, ValueError):
                age_hours = None
            enriched_additions["injury_status"] = injury.get("injury_status") or None
            enriched_additions["injury_type"] = injury.get("injury_type")
            enriched_additions["injury_description"] = injury.get("injury_description")
            enriched_additions["injury_date"] = injury.get("date_reported")
            enriched_additions["injury_age_hours"] = None if age_hours is None else round(age_hours, 1)
            # Unknown age is not fresh.
            enriched_additions["injury_stale"] = age_hours is None or age_hours > 12
            # New fields from injury service
            enriched_additions["injury_severity"] = injury.get("severity")
            enriched_additions["injury_confidence"] = injury.get("confidence", 50)
            enriched_additions["injury_sources"] = injury.get("sources") or ["ESPN"]
            enriched_additions["injury_game_status"] = injury.get("game_status")
            logger.debug(f"[Enrichment] {player_name}: injury_status={injury.get('injury_status')} severity={injury.get('severity')} confidence={injury.get('confidence')} (age={enriched_additions['injury_age_hours']}h)")

    # Sleeper's own designation, worst case wins. The report can be missing
    # (cache expired, name spelled differently) or lag Sleeper; either way a
    # player Sleeper lists as Out must not come back without an injury.
    report_status = enriched_additions.get("injury_status")
    sleeper_status = sleeper_injury_status(athlete)
    status = worst_status(
        None if report_status == "Active" else report_status, sleeper_status
    )
    if status and status != report_status:
        enriched_additions["injury_status"] = status
        enriched_additions["injury_sources"] = sorted(
            set(enriched_additions.get("injury_sources") or []) | {"Sleeper"}
        )

    # Practice status (DNP/LP/FP) - all positions. Only what a report said:
    # the official NFL.com report or a dated news note, matched by name and
    # team for this week. No report means None ("unreported") — this used to
    # be derived from the injury designation, or defaulted to a full practice
    # for anyone without one.
    from .practice_reports import lookup_practice
    practice = lookup_practice(
        nfl_db, athlete.get("full_name"), athlete.get("team_id") or athlete.get("team"),
        season=season, week=week,
    )
    if practice:
        enriched_additions["practice_status"] = practice["latest"]
        enriched_additions["practice_status_date"] = practice["latest_date"]
        enriched_additions["practice_pattern"] = practice["pattern"]
        enriched_additions["practice_trend"] = practice["trend"]
        enriched_additions["practice_days"] = practice["days"]
        if practice.get("game_status"):
            enriched_additions["practice_report_game_status"] = practice["game_status"]
        enriched_additions["practice_status_source"] = practice["source"]
        enriched_additions["practice_source"] = practice["source"]
        if practice.get("updated_at"):
            try:
                age_hours = (datetime.now(UTC) - datetime.fromisoformat(practice["updated_at"])).total_seconds() / 3600
                enriched_additions["practice_status_age_hours"] = round(age_hours, 1)
                enriched_additions["practice_status_stale"] = age_hours > 72
            except (TypeError, ValueError):
                pass
        logger.debug(f"[Enrichment] {player_name}: practice={practice['pattern']} ({practice['source']})")
    else:
        enriched_additions["practice_status"] = None
        enriched_additions["practice_status_source"] = "unreported"
        enriched_additions["practice_source"] = "unreported"

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

            # Without `rankings` the analyzer always answers from its neutral
            # placeholder table, so every player came back rank 16 / unknown
            # while real rankings sat in the database.
            rankings = _cached_defense_rankings(analyzer, nfl_db, season)
            matchup = analyzer.get_matchup_difficulty(position, opponent, rankings)

            if matchup and not matchup.get("is_fallback", True):
                enriched_additions["matchup_rank"] = matchup.get("rank")
                enriched_additions["matchup_tier"] = matchup.get("matchup_tier")
                enriched_additions["matchup_indicator"] = matchup.get("tier_indicator")
                enriched_additions["matchup_recommendation"] = matchup.get("recommendation")
                enriched_additions["defense_pts_allowed_avg"] = matchup.get("points_allowed_avg")
                enriched_additions["matchup_source"] = "defense_rankings"
                enriched_additions["matchup_is_fallback"] = False
                enriched_additions["matchup_is_provisional"] = bool(matchup.get("is_provisional"))
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
                enriched_additions["matchup_is_fallback"] = True
                logger.debug(f"[Enrichment] {player_name}: matchup vs {opponent} = neutral (fallback)")
        except Exception as e:
            logger.debug(f"[Enrichment] {player_name}: matchup analysis failed: {e}")

    # Vegas lines game environment analysis - QB, RB, WR, TE only. Athlete rows
    # carry `team_id`; reading only `team` skipped this block for everyone.
    team = athlete.get("team_id") or athlete.get("team")
    if team and position in ("QB", "RB", "WR", "TE"):
        try:
            from .vegas_tools import get_vegas_analyzer
            vegas = get_vegas_analyzer()

            # Get game lines for the team (synchronous - uses cached lines)
            game = vegas.get_game_lines(team, opponent=enriched_additions.get("opponent"))

            # Real lines carry no `is_fallback` key at all — only the neutral
            # placeholder sets it — so defaulting the lookup to True filed every
            # real line as a fallback.
            if game and not game.get("is_fallback"):
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
                enriched_additions["vegas_source"] = "lines"
                if game.get("total_is_fallback"):
                    # Spread posted, total not: the spread is real, the
                    # total and implied total are unknown (None), not 45.
                    enriched_additions["vegas_total_is_fallback"] = True

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
                # No numbers: a placeholder 45.0 / 22.5 is indistinguishable
                # from a real line once it leaves this function. Enrichment is
                # synchronous and cannot fetch, so this is the normal state
                # until a Vegas-aware tool has loaded lines this session.
                enriched_additions["vegas_source"] = "unavailable"
                logger.debug(f"[Enrichment] {player_name}: Vegas data unavailable (fallback)")
        except Exception as e:
            logger.debug(f"[Enrichment] {player_name}: Vegas analysis failed: {e}")

    if enriched_additions:
        logger.info(f"[Enrichment] {player_name}: Added {len(enriched_additions)} enrichment fields")

    return enriched_additions
