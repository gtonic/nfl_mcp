"""
Sleeper API MCP tools for the NFL MCP Server.

This module contains MCP tools for comprehensive fantasy league management
through the Sleeper API, including league information, rosters, users,
matchups, transactions, and more.
"""

import asyncio
import json
import logging

import httpx

from .config import (
    DEFAULT_TIMEOUT,
    LIMITS,
    LONG_TIMEOUT,
    create_http_client,
    get_http_headers,
    validate_limit,
)
from .errors import (
    ErrorType,
    create_error_response,
    create_success_response,
    handle_http_errors,
    handle_validation_error,
)

logger = logging.getLogger(__name__)

# The enrichment/data-fetch layer lives in sleeper_enrichment.py. Re-import it
# here so both internal callers and external `sleeper_tools.<name>` references
# keep working unchanged after the split.
from .sleeper_enrichment import (  # noqa: F401
    ADVANCED_ENRICH_ENABLED,
    _calculate_usage_trend,
    _enrich_usage_and_opponent,
    _estimate_snap_pct,
    _fetch_all_team_schedules,
    _fetch_injuries,
    _fetch_practice_reports,
    _fetch_week_player_snaps,
    _fetch_week_schedule,
    _fetch_weekly_usage_stats,
)


# ---------------------------------------------------------------------------
# Player enrichment helpers
# ---------------------------------------------------------------------------
def _init_db():
    try:
        from .database import NFLDatabase
        return NFLDatabase()
    except Exception as e:
        logger.debug(f"NFLDatabase init failed (enrichment disabled): {e}")
        return None

def _enrich_single(nfl_db, pid, cache):
    if pid in cache:
        return cache[pid]
    athlete = {}
    if nfl_db:
        try:
            athlete = nfl_db.get_athlete_by_id(pid) or {}
        except Exception as e:
            logger.debug(f"Lookup failed for {pid}: {e}")
    data = {"player_id": pid, "full_name": athlete.get("full_name"), "position": athlete.get("position")}
    cache[pid] = data
    return data

def _enrich_id_list(nfl_db, ids):
    cache = {}
    return [_enrich_single(nfl_db, pid, cache) for pid in (ids or [])]


def _resolve_team(base_info: dict) -> str | None:
    """Best-effort team abbreviation for an athlete row.

    Prefers the populated ``team``/``team_id`` field, then falls back to the
    ``team`` key inside the stored raw Sleeper player JSON — the column can be
    blank even when the raw record carries a team. Returns ``None`` for genuine
    free agents (both sources empty).
    """
    team = base_info.get("team") or base_info.get("team_id")
    if team:
        return team
    raw = base_info.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = None
    if isinstance(raw, dict):
        return raw.get("team") or None
    return None


@handle_http_errors(
    default_data={"league": None},
    operation_name="fetching league information"
)
async def get_league(league_id: str) -> dict:
    """
    Get specific league information from Sleeper API.

    This tool fetches detailed information about a specific fantasy league
    including settings, roster positions, scoring, and other league metadata.

    Args:
        league_id: The unique identifier for the league

    Returns:
        A dictionary containing:
        - league: League information and settings
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_league")

    # Sleeper API endpoint for specific league
    url = f"https://api.sleeper.app/v1/league/{league_id}"

    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

        # Parse JSON response
        league_data = response.json()

        return create_success_response({
            "league": league_data
        })


async def get_rosters(league_id: str) -> dict:
    """
    Get all rosters in a fantasy league from Sleeper API.

    This tool fetches all team rosters including player IDs, starters,
    bench players, and other roster information for the specified league.

    Note: Some leagues may have roster privacy settings that restrict access.
    If you encounter access issues, the league owner needs to make rosters public
    or grant appropriate permissions in the league settings.

    Args:
        league_id: The unique identifier for the league

    Returns:
        A dictionary containing:
        - rosters: List of all rosters in the league
        - count: Number of rosters found
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
        - access_help: Guidance for resolving access issues (if applicable)
    """
    headers = get_http_headers("sleeper_rosters")
    url = f"https://api.sleeper.app/v1/league/{league_id}/rosters"
    retry_delays = [0.0, 0.4, 1.2]
    attempts = 0
    last_error = None
    from .database import NFLDatabase
    nfl_db = NFLDatabase()

    for delay in retry_delays:
        if delay:
            import asyncio as _asyncio
            await _asyncio.sleep(delay)
        attempts += 1
        try:
            async with create_http_client() as client:
                response = await client.get(url, headers=headers, follow_redirects=True, timeout=DEFAULT_TIMEOUT)
                if response.status_code in (401,403,404):
                    # Direct terminal errors (no retry beyond first)
                    if response.status_code == 404:
                        return create_error_response(
                            f"League with ID '{league_id}' not found or does not exist",
                            ErrorType.HTTP,
                            {"rosters": [], "count": 0, "retries_used": attempts-1, "access_help": "Please verify the league ID is correct and the league exists"}
                        )
                    err_type = ErrorType.ACCESS_DENIED
                    if response.status_code == 403:
                        msg = "Access denied: Roster information is private for this league"
                        help_text = "The league owner needs to enable public roster access in league settings or you need appropriate permissions to view rosters"
                    else:
                        msg = "Authentication required: This league requires login to view rosters"
                        help_text = "This is a private league requiring authentication. Contact the league owner for access"
                    return create_error_response(
                        msg,
                        err_type,
                        {"rosters": [], "count": 0, "retries_used": attempts-1, "access_help": help_text}
                    )
                if response.status_code == 429:
                    last_error = "rate_limited"
                    continue
                response.raise_for_status()
                rosters_data = response.json()
                # Empty roster anomaly: retry unless final attempt
                if isinstance(rosters_data, list) and len(rosters_data) == 0 and attempts < len(retry_delays):
                    last_error = "empty_rosters"
                    # Try league info to determine privacy (single attempt)
                    try:
                        league_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}", headers=headers)
                        if league_resp.status_code == 200:
                            league_data = league_resp.json() or {}
                            if league_data:  # treat as privacy scenario -> return immediately (success, warning)
                                return create_success_response({
                                    "rosters": [],
                                    "count": 0,
                                    "warning": "League found but no rosters returned - this may indicate roster privacy settings are enabled",
                                    "access_help": "Ask league owner to review roster privacy settings",
                                    "retries_used": attempts-1,
                                    "stale": False,
                                    "failure_reason": None,
                                    "snapshot_fetched_at": None,
                                    "snapshot_age_seconds": None
                                })
                    except Exception:
                        pass
                    continue

                # Enrichment (best-effort)
                try:
                    cache: dict[str, dict] = {}
                    # Lazy schedule & stats fetch flags
                    schedule_fetched: dict[tuple[int,int], bool] = {}
                    stats_fetched: dict[tuple[int,int], bool] = {}

                    async def fetch_schedule_if_needed(season: int, week_guess: int):
                        key = (season, week_guess)
                        if schedule_fetched.get(key):
                            return
                        try:
                            sched = await _fetch_week_schedule(season, week_guess)
                            if sched:
                                nfl_db.upsert_schedule_games(sched)
                        except Exception as e:
                            logger.debug(f"schedule fetch failed season={season} week={week_guess}: {e}")
                        schedule_fetched[key] = True

                    async def fetch_stats_if_needed(season: int, week_guess: int):
                        key = (season, week_guess)
                        if stats_fetched.get(key):
                            return
                        try:
                            stats = await _fetch_week_player_snaps(season, week_guess)
                            if stats:
                                nfl_db.upsert_player_week_stats(stats)
                        except Exception as e:
                            logger.debug(f"snap stats fetch failed season={season} week={week_guess}: {e}")
                        stats_fetched[key] = True

                    # Attempt to derive current season & week (best-effort)
                    season = None; current_week = None
                    try:
                        state = await get_nfl_state()
                        if state.get("success") and state.get("nfl_state"):
                            st = state["nfl_state"]
                            season = st.get("season") or st.get("league_season")
                            current_week = st.get("week") or st.get("display_week")
                    except Exception:
                        pass

                    def estimate_snap_pct_from_depth(position: str | None, depth_rank: int | None):
                        if depth_rank is None:
                            return None
                        if depth_rank == 1:
                            return 70.0
                        if depth_rank == 2:
                            return 45.0
                        return 15.0

                    async def enrich_players(player_ids):
                        enriched: list[dict] = []
                        for pid in player_ids or []:
                            if pid in cache:
                                enriched.append(cache[pid]); continue
                            athlete = nfl_db.get_athlete_by_id(pid) or {}
                            obj = {"player_id": pid, "full_name": athlete.get("full_name"), "position": athlete.get("position")}

                            # Use _enrich_usage_and_opponent for all enrichment
                            # This ensures injury_status and practice_status are always included
                            try:
                                athlete_for_enrichment = {
                                    "id": pid,
                                    "player_id": pid,
                                    "full_name": athlete.get("full_name"),
                                    "name": athlete.get("full_name"),
                                    "position": athlete.get("position"),
                                    "team": athlete.get("team"),
                                    "team_id": athlete.get("team_id"),
                                    "raw": athlete.get("raw")
                                }
                                extra = _enrich_usage_and_opponent(nfl_db, athlete_for_enrichment, season, current_week)
                                obj.update(extra)
                            except Exception as e:
                                logger.debug(f"Roster player enrichment failed for {pid}: {e}")

                            cache[pid] = obj; enriched.append(obj)
                        return enriched

                    if isinstance(rosters_data, list):
                        # Because we need async inside enrichment, gather sequentially
                        for roster in rosters_data:
                            if isinstance(roster, dict):
                                # "0" is Sleeper's empty-slot sentinel — filter it
                                # (and blanks) so we don't fabricate phantom players.
                                if isinstance(roster.get("players"), list):
                                    roster["players_enriched"] = await enrich_players(
                                        [p for p in roster["players"] if p and p != "0"])
                                if isinstance(roster.get("starters"), list):
                                    roster["starters_enriched"] = await enrich_players(
                                        [p for p in roster["starters"] if p and p != "0"])
                except Exception as enrich_error:
                    logger.debug(f"Roster enrichment (extended) skipped: {enrich_error}")

                # Save snapshot
                nfl_db.save_roster_snapshot(league_id, rosters_data)
                return create_success_response({
                    "rosters": rosters_data,
                    "count": len(rosters_data),
                    "retries_used": attempts-1,
                    "stale": False,
                    "failure_reason": None,
                    "snapshot_fetched_at": None,
                    "snapshot_age_seconds": None
                })
        except httpx.TimeoutException:
            last_error = "timeout"
            continue
        except httpx.HTTPStatusError as he:
            if he.response is not None and he.response.status_code == 429:
                return create_error_response(
                    "Rate limit exceeded for Sleeper API - please try again in a few minutes",
                    ErrorType.HTTP,
                    {"rosters": [], "count": 0, "retries_used": attempts-1, "access_help": "Sleeper API has rate limits. Wait a few minutes before trying again"}
                )
            last_error = f"http:{getattr(he.response,'status_code', '?')}"
            continue
        except httpx.NetworkError as ne:
            last_error = f"network:{ne}"
            continue
        except Exception as e:
            last_error = f"unexpected:{e}"
            continue

    # Fallback: snapshot
    snap = nfl_db.load_roster_snapshot(league_id)
    if snap:
        return create_error_response(
            "Roster fetch failed after retries (serving snapshot)",
            ErrorType.NETWORK if last_error and last_error.startswith("network") else ErrorType.UNEXPECTED,
            {
                "rosters": snap["rosters"],
                "count": len(snap["rosters"]),
                "retries_used": attempts,
                "stale": snap.get("stale", True),
                "failure_reason": last_error or "unknown",
                "snapshot_fetched_at": snap.get("fetched_at"),
                "snapshot_age_seconds": snap.get("age_seconds")
            }
        )
    return create_error_response(
        f"Roster fetch failed after retries: {last_error}",
        ErrorType.NETWORK if last_error and last_error.startswith("network") else ErrorType.UNEXPECTED,
    {"rosters": [], "count": 0, "retries_used": attempts, "stale": False, "failure_reason": last_error or "unknown", "snapshot_fetched_at": None, "snapshot_age_seconds": None}
    )


@handle_http_errors(
    default_data={"users": [], "count": 0},
    operation_name="fetching league users"
)
async def get_league_users(league_id: str) -> dict:
    """
    Get all users in a fantasy league from Sleeper API.

    This tool fetches all users/managers in the specified league including
    their display names, usernames, and other profile information.

    Args:
        league_id: The unique identifier for the league

    Returns:
        A dictionary containing:
        - users: List of all users in the league
        - count: Number of users found
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_users")

    # Sleeper API endpoint for league users
    url = f"https://api.sleeper.app/v1/league/{league_id}/users"

    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

        # Parse JSON response
        users_data = response.json()

        return create_success_response({
            "users": users_data,
            "count": len(users_data)
        })


async def get_matchups(league_id: str, week: int) -> dict:
    """Get matchups for a week with robustness (retry + snapshot fallback)."""
    try:
        from .param_validator import format_errors, validate_params
        schema = {"week": {"type": int, "required": True, "min": LIMITS["week_min"], "max": LIMITS["week_max"]}}
        validated, errors = validate_params(schema, {"week": week})
        if errors:
            bounds_prefixes = ("'week' must be >=", "'week' must be <=")
            if all(any(e.startswith(p) for p in bounds_prefixes) for e in errors):
                return handle_validation_error(
                    f"Week must be between {LIMITS['week_min']} and {LIMITS['week_max']}",
                    {"matchups": [], "week": week, "count": 0}
                )
            return handle_validation_error(format_errors(errors), {"matchups": [], "week": week, "count": 0})
        week = validated["week"]
    except Exception:
        if week < LIMITS["week_min"] or week > LIMITS["week_max"]:
            return handle_validation_error(
                f"Week must be between {LIMITS['week_min']} and {LIMITS['week_max']}",
                {"matchups": [], "week": week, "count": 0}
            )

    headers = get_http_headers("sleeper_matchups")
    url = f"https://api.sleeper.app/v1/league/{league_id}/matchups/{week}"
    retry_delays = [0.0, 0.4, 1.0]
    attempts = 0
    last_error = None
    from .database import NFLDatabase
    nfl_db = NFLDatabase()

    for delay in retry_delays:
        if delay:
            import asyncio as _asyncio
            await _asyncio.sleep(delay)
        attempts += 1
        try:
            async with create_http_client() as client:
                response = await client.get(url, headers=headers, follow_redirects=True, timeout=DEFAULT_TIMEOUT)
                if response.status_code in (401,403,404):
                    if response.status_code == 404:
                        return create_error_response(
                            f"League or matchups not found for league '{league_id}' week {week}",
                            ErrorType.HTTP,
                            {"matchups": [], "week": week, "count": 0, "retries_used": attempts-1, "stale": False, "failure_reason": "not_found"}
                        )
                    err_type = ErrorType.ACCESS_DENIED
                    if response.status_code == 403:
                        msg = "Access denied: Matchups are private for this league"
                        help_text = "League owner must enable public matchup visibility"
                    else:
                        msg = "Authentication required to view matchups for this league"
                        help_text = "Contact league owner for access to private league matchups"
                    return create_error_response(
                        msg,
                        err_type,
                        {"matchups": [], "week": week, "count": 0, "retries_used": attempts-1, "stale": False, "failure_reason": "access_denied", "access_help": help_text}
                    )
                if response.status_code == 429:
                    last_error = "rate_limited"
                    continue
                response.raise_for_status()
                matchups_data = response.json()
                if isinstance(matchups_data, list) and len(matchups_data) == 0 and attempts < len(retry_delays):
                    last_error = "empty_matchups"
                    continue

                # Enrichment
                try:
                    cache: dict[str, dict] = {}
                    state = None
                    season = None
                    try:
                        state = await get_nfl_state()
                        if state.get("success") and state.get("nfl_state"):
                            st = state["nfl_state"]
                            season = st.get("season") or st.get("league_season")
                    except Exception:
                        pass

                    if isinstance(matchups_data, list):
                        for m in matchups_data:
                            if not isinstance(m, dict):
                                continue
                            enriched_players = []
                            enriched_starters = []
                            for key, target_list in [("players", enriched_players), ("starters", enriched_starters)]:
                                ids = m.get(key)
                                if not isinstance(ids, list):
                                    continue
                                for pid in ids:
                                    if pid in cache:
                                        target_list.append(cache[pid]); continue
                                    athlete = nfl_db.get_athlete_by_id(pid) or {}
                                    obj = {"player_id": pid, "full_name": athlete.get("full_name"), "position": athlete.get("position")}

                                    # Use _enrich_usage_and_opponent for all enrichment
                                    # This ensures injury_status and practice_status are always included
                                    try:
                                        athlete_for_enrichment = {
                                            "id": pid,
                                            "player_id": pid,
                                            "full_name": athlete.get("full_name"),
                                            "name": athlete.get("full_name"),
                                            "position": athlete.get("position"),
                                            "team": athlete.get("team"),
                                            "team_id": athlete.get("team_id"),
                                            "raw": athlete.get("raw")
                                        }
                                        extra = _enrich_usage_and_opponent(nfl_db, athlete_for_enrichment, season, week)
                                        obj.update(extra)
                                    except Exception as e:
                                        logger.debug(f"Matchup player enrichment failed for {pid}: {e}")

                                    cache[pid] = obj
                                    target_list.append(obj)
                            if enriched_players:
                                m["players_enriched"] = enriched_players
                            if enriched_starters:
                                m["starters_enriched"] = enriched_starters
                except Exception as e:
                    logger.debug(f"Matchup enrichment (extended) skipped: {e}")

                nfl_db.save_matchup_snapshot(league_id, week, matchups_data)
                return create_success_response({
                    "matchups": matchups_data,
                    "week": week,
                    "count": len(matchups_data),
                    "retries_used": attempts-1,
                    "stale": False,
                    "failure_reason": None,
                    "snapshot_fetched_at": None,
                    "snapshot_age_seconds": None
                })
        except httpx.TimeoutException:
            last_error = "timeout"
            continue
        except httpx.HTTPStatusError as he:
            if he.response is not None and he.response.status_code == 429:
                return create_error_response(
                    "Rate limit exceeded for Sleeper API - please try again later",
                    ErrorType.HTTP,
                    {"matchups": [], "week": week, "count": 0, "retries_used": attempts-1, "stale": False, "failure_reason": "rate_limited"}
                )
            last_error = f"http:{getattr(he.response,'status_code','?')}"
            continue
        except httpx.NetworkError as ne:
            last_error = f"network:{ne}"
            continue
        except Exception as e:
            last_error = f"unexpected:{e}"
            continue

    snap = nfl_db.load_matchup_snapshot(league_id, week)
    if snap:
        return create_error_response(
            "Matchup fetch failed after retries (serving snapshot)",
            ErrorType.NETWORK if last_error and last_error.startswith("network") else ErrorType.UNEXPECTED,
            {
                "matchups": snap["matchups"],
                "week": week,
                "count": len(snap["matchups"]),
                "retries_used": attempts,
                "stale": snap.get("stale", True),
                "failure_reason": last_error or "unknown",
                "snapshot_fetched_at": snap.get("fetched_at"),
                "snapshot_age_seconds": snap.get("age_seconds")
            }
        )
    return create_error_response(
        f"Matchup fetch failed after retries: {last_error}",
        ErrorType.NETWORK if last_error and last_error.startswith("network") else ErrorType.UNEXPECTED,
        {"matchups": [], "week": week, "count": 0, "retries_used": attempts, "stale": False, "failure_reason": last_error or "unknown", "snapshot_fetched_at": None, "snapshot_age_seconds": None}
    )


@handle_http_errors(
    default_data={"playoff_bracket": None, "bracket_type": None},
    operation_name="fetching playoff bracket"
)
async def get_playoff_bracket(league_id: str, bracket_type: str = "winners") -> dict:
    """Get playoff bracket information (winners or losers) for a Sleeper league.

    Sleeper exposes two brackets: winners_bracket and losers_bracket. This function
    allows selecting which one to retrieve while keeping backward compatibility
    (defaulting to the winners bracket if not specified).

    Args:
        league_id: League identifier
        bracket_type: Which bracket to fetch ("winners" | "losers"), defaults to "winners"

    Returns:
        success response with keys:
        - playoff_bracket: list bracket structure
        - bracket_type: which bracket was fetched
    """
    try:
        from .param_validator import format_errors, validate_params
        schema = {"bracket_type": {"type": str, "required": True, "choices": ["winners", "losers"]}}
        normalized = bracket_type.lower().strip() if isinstance(bracket_type, str) else bracket_type
        validated, errors = validate_params(schema, {"bracket_type": normalized})
        if errors:
            if any("bracket_type" in e for e in errors):
                return handle_validation_error(
                    "bracket_type must be one of: winners, losers",
                    {"playoff_bracket": None, "bracket_type": bracket_type}
                )
            return handle_validation_error(format_errors(errors), {"playoff_bracket": None, "bracket_type": bracket_type})
        bracket_type_normalized = validated["bracket_type"].lower().strip()
    except Exception:
        bracket_type_normalized = bracket_type.lower().strip()
        if bracket_type_normalized not in {"winners", "losers"}:
            return handle_validation_error(
                "bracket_type must be one of: winners, losers",
                {"playoff_bracket": None, "bracket_type": bracket_type}
            )

    headers = get_http_headers("sleeper_playoffs")
    path = "winners_bracket" if bracket_type_normalized == "winners" else "losers_bracket"
    url = f"https://api.sleeper.app/v1/league/{league_id}/{path}"

    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        bracket_data = response.json()
        return create_success_response({
            "playoff_bracket": bracket_data,
            "bracket_type": bracket_type_normalized
        })



@handle_http_errors(
    default_data={"nfl_state": None},
    operation_name="fetching NFL state"
)
async def get_nfl_state() -> dict:
    """
    Get current NFL state information from Sleeper API.

    This tool fetches the current state of the NFL including season type,
    current week, and other league-wide information.

    Returns:
        A dictionary containing:
        - nfl_state: Current NFL state information
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_nfl_state")

    # Sleeper API endpoint for NFL state
    url = "https://api.sleeper.app/v1/state/nfl"

    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

        # Parse JSON response
        nfl_state_data = response.json()

        return create_success_response({
            "nfl_state": nfl_state_data
        })


@handle_http_errors(
    default_data={"trending_players": [], "trend_type": None, "lookback_hours": None, "count": 0},
    operation_name="fetching trending players"
)
async def get_trending_players(nfl_db=None, trend_type: str = "add", lookback_hours: int | None = 24, limit: int | None = 25) -> dict:
    """
    Get trending players from Sleeper API.

    This tool fetches currently trending players based on adds/drops or other
    activity metrics from the Sleeper platform.

    Args:
        nfl_db: NFLDatabase instance to use for player lookups (if None, creates new instance)
        trend_type: Type of trend to fetch ("add" or "drop", defaults to "add")
        lookback_hours: Hours to look back for trends (1-168, defaults to 24)
        limit: Maximum number of players to return (1-100, defaults to 25)

    Returns:
        A dictionary containing:
        - trending_players: List of trending players with enriched data
        - trend_type: The trend type requested
        - lookback_hours: Hours looked back
        - count: Number of players returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Central validation via param_validator (preserve legacy messages)
    try:
        from .param_validator import format_errors, validate_params
        schema = {
            "trend_type": {"type": str, "required": True, "choices": ["add", "drop"]},
            "lookback_hours": {"type": (int, type(None)), "required": False, "min": LIMITS["trending_lookback_min"], "max": LIMITS["trending_lookback_max"], "nullable": True, "default": 24},
            "limit": {"type": (int, type(None)), "required": False, "min": LIMITS["trending_limit_min"], "max": LIMITS["trending_limit_max"], "nullable": True, "default": 25},
        }
        values = {"trend_type": trend_type, "lookback_hours": lookback_hours, "limit": limit}
        validated, errors = validate_params(schema, values)
        if errors:
            # Legacy message mapping
            if any("trend_type" in e for e in errors):
                return handle_validation_error(
                    "trend_type must be one of: add, drop",
                    {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0}
                )
            return handle_validation_error(format_errors(errors), {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0})
        trend_type = validated["trend_type"]
        lookback_hours = validated["lookback_hours"] or 24
        limit = validated["limit"] or 25
    except Exception:
        # Fallback to legacy validation
        valid_trend_types = ["add", "drop"]
        if trend_type not in valid_trend_types:
            return handle_validation_error(
                f"trend_type must be one of: {', '.join(valid_trend_types)}",
                {"trending_players": [], "trend_type": trend_type, "lookback_hours": lookback_hours, "count": 0}
            )
        if lookback_hours is not None:
            lookback_hours = validate_limit(
                lookback_hours,
                LIMITS["trending_lookback_min"],
                LIMITS["trending_lookback_max"],
                24
            )
        else:
            lookback_hours = 24
        if limit is not None:
            limit = validate_limit(
                limit,
                LIMITS["trending_limit_min"],
                LIMITS["trending_limit_max"],
                25
            )
        else:
            limit = 25

    headers = get_http_headers("sleeper_trending")

    # Sleeper API endpoint for trending players
    url = f"https://api.sleeper.app/v1/players/nfl/trending/{trend_type}?lookback_hours={lookback_hours}&limit={limit}"

    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        raw_items = response.json()  # May be list[dict] or list[str]

        if not raw_items:
            return create_success_response({
                "trending_players": [],
                "trend_type": trend_type,
                "lookback_hours": lookback_hours,
                "count": 0
            })

        if nfl_db is None:
            from .database import NFLDatabase
            nfl_db = NFLDatabase()

        try:
            sample_athletes = nfl_db.search_athletes_by_name("", limit=1)
            if not sample_athletes:
                from . import athlete_tools
                try:
                    logger.info("Database appears empty, attempting to fetch athletes for trending players lookup")
                    await athlete_tools.fetch_athletes(nfl_db)
                except Exception as fetch_error:
                    logger.warning(f"Failed to automatically fetch athletes: {fetch_error}")
        except Exception as db_error:
            logger.warning(f"Could not check database status: {db_error}")

        # Get current season and week for enrichment
        season, week = None, None
        try:
            from .nfl_tools import get_current_season_and_week
            season, week = await get_current_season_and_week()
            logger.debug(f"[Trending Players] Using season={season}, week={week} for enrichment")
        except Exception as e:
            logger.warning(f"[Trending Players] Could not get current season/week: {e}")

        enriched_players = []
        for item in raw_items:
            if isinstance(item, dict):
                player_id = item.get("player_id") or item.get("id")
                count = item.get("count")  # Sleeper trending provides count
                if not player_id:
                    continue
            else:
                player_id = item
                count = None

            base_info = nfl_db.get_athlete_by_id(player_id) or {
                "player_id": player_id,
                "full_name": None,
                "first_name": None,
                "last_name": None,
                "position": None,
                "team": None,
                "age": None,
                "jersey": None
            }

            # Add enrichment (injury, practice status, and advanced stats)
            # Always enrich to ensure injury and practice status are included
            try:
                athlete_for_enrichment = {
                    "id": player_id,
                    "player_id": player_id,
                    "full_name": base_info.get("full_name"),
                    "name": base_info.get("full_name"),
                    "position": base_info.get("position"),
                    "team": base_info.get("team"),
                    "team_id": base_info.get("team_id"),
                    "raw": base_info.get("raw")
                }
                extra = _enrich_usage_and_opponent(nfl_db, athlete_for_enrichment, season, week)
                base_info.update(extra)
                logger.debug(f"[Trending Players] Enriched {base_info.get('full_name')} with {len(extra)} fields")
            except Exception as e:
                logger.warning(f"[Trending Players] Failed to enrich player {player_id}: {e}")

            # Surface the key identity fields at the top level so consumers don't
            # have to reach into `enriched`; normalize team (the column can be
            # blank even when the raw record carries it). `enriched` is kept
            # intact for the full record (injury, usage, opponent, raw, ...).
            team = _resolve_team(base_info)
            base_info["team"] = team

            enriched_players.append({
                "player_id": player_id,
                "count": count,
                "full_name": base_info.get("full_name"),
                "position": base_info.get("position"),
                "team": team,
                "enriched": base_info,
            })

        return create_success_response({
            "trending_players": enriched_players,
            "trend_type": trend_type,
            "lookback_hours": lookback_hours,
            "count": len(enriched_players)
        })


@handle_http_errors(
    default_data={"picks": [], "count": 0},
    operation_name="fetching draft picks"
)
async def get_draft_picks(draft_id: str) -> dict:
    """Fetch all picks in a draft, with additive enrichment.

    Returns picks with an additive `player_enriched` field for each pick that
    carries a player_id, when the athlete is known locally.
    """
    headers = get_http_headers("sleeper_draft_picks")
    url = f"https://api.sleeper.app/v1/draft/{draft_id}/picks"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        picks = response.json()
        try:
            from .database import NFLDatabase
            nfl_db = NFLDatabase()
            for p in picks:
                if isinstance(p, dict) and p.get("player_id"):
                    athlete = nfl_db.get_athlete_by_id(p["player_id"]) or {}
                    p["player_enriched"] = {
                        "player_id": p["player_id"],
                        "full_name": athlete.get("full_name"),
                        "position": athlete.get("position")
                    }
        except Exception as enrich_error:
            logger.debug(f"Draft pick enrichment skipped: {enrich_error}")
        return create_success_response({
            "picks": picks,
            "count": len(picks)
        })




# -------------------------------------------------------------
# NEW: Additional Sleeper endpoints (Users, Drafts, Players)
# -------------------------------------------------------------

@handle_http_errors(
    default_data={"user": None},
    operation_name="fetching user"
)
async def get_user(user_id_or_username: str) -> dict:
    """Fetch a Sleeper user by user_id or username."""
    headers = get_http_headers("sleeper_users")
    url = f"https://api.sleeper.app/v1/user/{user_id_or_username}"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        return create_success_response({"user": response.json()})


@handle_http_errors(
    default_data={"leagues": [], "count": 0, "season": None},
    operation_name="fetching user leagues"
)
async def get_user_leagues(user_id: str, season: int) -> dict:
    """Fetch all leagues for a user for a season."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/user/{user_id}/leagues/nfl/{season}"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        return create_success_response({"leagues": data, "count": len(data), "season": season})


@handle_http_errors(
    default_data={"drafts": [], "count": 0},
    operation_name="fetching league drafts"
)
async def get_league_drafts(league_id: str) -> dict:
    """Fetch all drafts for a league."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/league/{league_id}/drafts"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        return create_success_response({"drafts": data, "count": len(data)})


@handle_http_errors(
    default_data={"draft": None},
    operation_name="fetching draft"
)
async def get_draft(draft_id: str) -> dict:
    """Fetch a specific draft."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/draft/{draft_id}"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        return create_success_response({"draft": response.json()})


@handle_http_errors(
    default_data={"traded_picks": [], "count": 0},
    operation_name="fetching draft traded picks"
)
async def get_draft_traded_picks(draft_id: str) -> dict:
    """Fetch traded picks for a draft."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/draft/{draft_id}/traded_picks"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        try:
            nfl_db = _init_db()
            cache = {}
            if isinstance(data, list):
                for tp in data:
                    if isinstance(tp, dict) and tp.get("player_id"):
                        tp["player_enriched"] = _enrich_single(nfl_db, tp["player_id"], cache)
        except Exception as e:
            logger.debug(f"Draft traded pick enrichment skipped: {e}")
        return create_success_response({"traded_picks": data, "count": len(data)})


# Player dump caching (large ~5MB) - cache in memory to reduce calls.
_PLAYERS_CACHE = {"data": None, "fetched_at": 0}
_PLAYERS_CACHE_TTL = 60 * 60 * 12  # 12 hours

@handle_http_errors(
    default_data={"players": {}, "cached": False},
    operation_name="fetching all players"
)
async def fetch_all_players(force_refresh: bool = False) -> dict:
    """Fetch the full players map from Sleeper (cached; heavy endpoint).

    Args:
        force_refresh: Ignore cache and refetch.
    """
    import time as _time
    now = _time.time()
    if (
        not force_refresh and _PLAYERS_CACHE["data"] is not None and
        now - _PLAYERS_CACHE["fetched_at"] < _PLAYERS_CACHE_TTL
    ):
        return create_success_response({
            "players": {},  # not returning the large blob again intentionally
            "cached": True,
            "ttl_remaining": int(_PLAYERS_CACHE_TTL - (now - _PLAYERS_CACHE["fetched_at"]))
        })

    headers = get_http_headers("sleeper_league")
    url = "https://api.sleeper.app/v1/players/nfl"
    async with create_http_client(timeout=LONG_TIMEOUT) as client:  # longer timeout
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        _PLAYERS_CACHE["data"] = data
        _PLAYERS_CACHE["fetched_at"] = now
        return create_success_response({
            "players": {},  # avoid huge payload downstream; signal success
            "cached": False,
            "player_count": len(data)
        })


@handle_http_errors(
    default_data={"context": {}, "league_id": None, "week": None},
    operation_name="fetching consolidated fantasy context"
)
async def get_fantasy_context(league_id: str, week: int | None = None, include: str | None = None) -> dict:
    """Aggregate core fantasy data (league, rosters, users, matchups, transactions) in one call.

    Parameters:
        league_id (str): Sleeper league id.
        week (int, optional): Week to fetch matchups & transactions. If omitted will be auto-inferred.
        include (str, optional): Comma-separated subset filters (e.g. "league,rosters,matchups,transactions,users").

    Returns success with:
        context: {
            league, rosters, users, matchups, transactions
        }
        week: effective week used
        auto_week_inferred: bool if week was inferred

    IMPORTANT FOR LLM AGENTS: When using this data to generate briefings or reports, always provide
    complete analysis immediately. Never ask for user confirmations or additional input - render the
    full report directly with all insights and recommendations.
    """
    wanted = {s.strip() for s in (include.split(",") if include else []) if s.strip()}
    if not wanted:
        wanted = {"league", "rosters", "users", "matchups", "transactions"}

    context: dict = {}
    # Always fetch league first for structural context
    league_resp = await get_league(league_id) if "league" in wanted else {"success": True}
    if not league_resp.get("success"):
        return create_error_response(
            league_resp.get("error", "Failed to fetch league"),
            error_type=league_resp.get("error_type"),
            data={"context": {}, "league_id": league_id}
        )
    if "league" in wanted:
        context["league"] = league_resp.get("league")

    # Parallel fetches for rosters and users (no week dependency)
    parallel_tasks = []
    task_keys = []

    if "rosters" in wanted:
        parallel_tasks.append(get_rosters(league_id))
        task_keys.append("rosters")
    if "users" in wanted:
        parallel_tasks.append(get_league_users(league_id))
        task_keys.append("users")

    # Execute parallel tasks
    if parallel_tasks:
        results = await asyncio.gather(*parallel_tasks, return_exceptions=True)
        for key, result in zip(task_keys, results, strict=False):
            if isinstance(result, Exception):
                logger.warning(f"[Fantasy Context] Failed to fetch {key}: {result}")
            elif result.get("success"):
                context[key] = result.get(key)

    # Determine effective week (auto inference if needed)
    auto_inferred = False
    effective_week = week
    if ("matchups" in wanted or "transactions" in wanted) and effective_week is None:
        try:
            nfl_state = await get_nfl_state()
            if nfl_state.get("success") and nfl_state.get("nfl_state"):
                inferred = nfl_state["nfl_state"].get("week") or nfl_state["nfl_state"].get("display_week")
                if isinstance(inferred, int):
                    effective_week = inferred
                    auto_inferred = True
        except Exception as e:
            logger.debug(f"Context week inference failed: {e}")

    # Parallel fetches for week-dependent data
    week_tasks = []
    week_keys = []

    if "matchups" in wanted and effective_week is not None:
        week_tasks.append(get_matchups(league_id, effective_week))
        week_keys.append("matchups")
    if "transactions" in wanted:
        week_tasks.append(get_transactions(league_id, week=effective_week))
        week_keys.append("transactions")

    # Execute week-dependent parallel tasks
    if week_tasks:
        results = await asyncio.gather(*week_tasks, return_exceptions=True)
        for key, result in zip(week_keys, results, strict=False):
            if isinstance(result, Exception):
                logger.warning(f"[Fantasy Context] Failed to fetch {key}: {result}")
            elif result.get("success"):
                context[key] = result.get(key)

    return create_success_response({
        "context": context,
        "league_id": league_id,
        "week": effective_week,
        "auto_week_inferred": auto_inferred
    })




# ---------------------------------------------------------------------------
# Strategic-planning tools live in sleeper_strategy.py. They consume the core
# tools above, so they're re-exported here at the END of the module (after
# get_league/get_matchups are defined) to keep the import acyclic.
# ---------------------------------------------------------------------------
from .sleeper_strategy import (  # noqa: F401
    get_playoff_preparation_plan,
    get_season_bye_week_coordination,
    get_strategic_matchup_preview,
    get_trade_deadline_analysis,
)

# Transactions tools live in sleeper_transactions.py (re-exported here; they
# consume core primitives incl. get_nfl_state, so they load after it).
from .sleeper_transactions import get_traded_picks, get_transactions  # noqa: F401
