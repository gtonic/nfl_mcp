"""
Sleeper API MCP tools for the NFL MCP Server.

This module contains MCP tools for comprehensive fantasy league management 
through the Sleeper API, including league information, rosters, users, 
matchups, transactions, and more.
"""

import httpx
import logging
import os
from typing import Optional, Dict, List, Tuple
from datetime import datetime, UTC

from .config import get_http_headers, create_http_client, validate_limit, LIMITS, LONG_TIMEOUT, DEFAULT_TIMEOUT
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors, handle_validation_error
)

logger = logging.getLogger(__name__)

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
                    cache: Dict[str, Dict] = {}
                    # Lazy schedule & stats fetch flags
                    schedule_fetched: Dict[Tuple[int,int], bool] = {}
                    stats_fetched: Dict[Tuple[int,int], bool] = {}

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

                    def estimate_snap_pct_from_depth(position: Optional[str], depth_rank: Optional[int]):
                        if depth_rank is None:
                            return None
                        if depth_rank == 1:
                            return 70.0
                        if depth_rank == 2:
                            return 45.0
                        return 15.0

                    async def enrich_players(player_ids):
                        enriched: List[Dict] = []
                        for pid in player_ids or []:
                            if pid in cache:
                                enriched.append(cache[pid]); continue
                            athlete = nfl_db.get_athlete_by_id(pid) or {}
                            obj = {"player_id": pid, "full_name": athlete.get("full_name"), "position": athlete.get("position")}

                            # Snap pct enrichment
                            snap_source = None
                            if season and current_week and athlete.get("position") not in (None, "DEF"):
                                snap_row = nfl_db.get_player_snap_pct(pid, season, current_week)
                                if not snap_row:
                                    await fetch_stats_if_needed(season, current_week)
                                    snap_row = nfl_db.get_player_snap_pct(pid, season, current_week)
                                if snap_row and snap_row.get("snap_pct") is not None:
                                    obj["snap_pct"] = snap_row.get("snap_pct")
                                    snap_source = "cached"
                            if "snap_pct" not in obj:
                                depth_rank = athlete.get("raw") and athlete.get("raw").get("depth_chart_order") if isinstance(athlete.get("raw"), dict) else None
                                est = estimate_snap_pct_from_depth(athlete.get("position"), depth_rank)
                                if est is not None:
                                    obj["snap_pct"] = est
                                    snap_source = "estimated"
                            if snap_source:
                                obj["snap_pct_source"] = snap_source

                            # Opponent enrichment for DEF
                            if athlete.get("position") == "DEF" and season and current_week:
                                opponent = nfl_db.get_opponent(season, current_week, athlete.get("team_id")) if hasattr(nfl_db, 'get_opponent') else None
                                opponent_source = None
                                if not opponent:
                                    await fetch_schedule_if_needed(season, current_week)
                                    opponent = nfl_db.get_opponent(season, current_week, athlete.get("team_id")) if hasattr(nfl_db, 'get_opponent') else None
                                    if opponent:
                                        opponent_source = "fetched"
                                else:
                                    opponent_source = "cached"
                                if opponent:
                                    obj["opponent"] = opponent
                                    obj["opponent_source"] = opponent_source or "cached"

                            cache[pid] = obj; enriched.append(obj)
                        return enriched

                    if isinstance(rosters_data, list):
                        # Because we need async inside enrichment, gather sequentially
                        for roster in rosters_data:
                            if isinstance(roster, dict):
                                if isinstance(roster.get("players"), list):
                                    roster["players_enriched"] = await enrich_players(roster["players"])
                                if isinstance(roster.get("starters"), list):
                                    roster["starters_enriched"] = await enrich_players(roster["starters"])
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
            f"Roster fetch failed after retries (serving snapshot)",
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
        from .param_validator import validate_params, format_errors
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
                    cache: Dict[str, Dict] = {}
                    state = None
                    season = None
                    try:
                        state = await get_nfl_state()
                        if state.get("success") and state.get("nfl_state"):
                            st = state["nfl_state"]
                            season = st.get("season") or st.get("league_season")
                    except Exception:
                        pass

                    def estimate_snap(depth_rank: Optional[int]):
                        if depth_rank == 1: return 70.0
                        if depth_rank == 2: return 45.0
                        if depth_rank is not None: return 15.0
                        return None

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
                                    # Snap pct (only if season known)
                                    if season and athlete.get("position") not in (None, "DEF"):
                                        row = nfl_db.get_player_snap_pct(pid, season, week) if hasattr(nfl_db,'get_player_snap_pct') else None
                                        if row and row.get("snap_pct") is not None:
                                            obj["snap_pct"] = row.get("snap_pct")
                                            obj["snap_pct_source"] = "cached"
                                        else:
                                            est = estimate_snap(athlete.get("raw", {}).get("depth_chart_order") if isinstance(athlete.get("raw"), dict) else None)
                                            if est is not None:
                                                obj["snap_pct"] = est
                                                obj["snap_pct_source"] = "estimated"
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
        from .param_validator import validate_params, format_errors
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


async def get_transactions(league_id: str, round: Optional[int] = None, week: Optional[int] = None) -> dict:
    """Get transactions for a specific (or inferred) week of a Sleeper league with robustness.

    Robustness features:
    - Week auto-inference (existing behavior)
    - Retry/backoff on transient failures & empty anomaly
    - Snapshot persistence & fallback (per league/week)
    - Additive metadata: retries_used, stale, failure_reason
    - Preserves legacy validation & success contract
    """
    auto_inferred = False
    # Central param schema validation (except league_id which is positional)
    try:
        from .param_validator import validate_params, format_errors
        schema = {
            "round": {"type": (int, type(None)), "required": False, "min": LIMITS["round_min"], "max": LIMITS["round_max"], "nullable": True},
            "week": {"type": (int, type(None)), "required": False, "min": LIMITS["round_min"], "max": LIMITS["round_max"], "nullable": True},
        }
        validated, errors = validate_params(schema, {"round": round, "week": week})
        if errors:
            # If the only errors are min/max for round/week, convert to legacy message for tests
            legacy_bounds = {"'round' must be >=", "'round' must be <=", "'week' must be >=", "'week' must be <="}
            if all(any(e.startswith(prefix) for prefix in legacy_bounds) for e in errors):
                return handle_validation_error(
                    f"Week must be between {LIMITS['round_min']} and {LIMITS['round_max']}",
                    {"transactions": [], "week": week, "count": 0}
                )
            return handle_validation_error(format_errors(errors), {"transactions": [], "week": week, "count": 0})
        round = validated.get("round")
        week = validated.get("week")
    except Exception as e:
        logger.debug(f"Param validator fallback (non-fatal) for transactions: {e}")

    # Normalize week/round
    if week is None and round is not None:
        week = round
    elif week is not None and round is not None and week != round:
        return handle_validation_error(
            "Conflicting values provided for week and round; they must match",
            {"transactions": [], "week": week, "count": 0}
        )

    # Infer week if absent
    if week is None:
        try:
            nfl_state_resp = await get_nfl_state()
            if nfl_state_resp.get("success") and nfl_state_resp.get("nfl_state"):
                inferred = nfl_state_resp["nfl_state"].get("week") or nfl_state_resp["nfl_state"].get("display_week")
                if isinstance(inferred, int):
                    week = inferred
                    auto_inferred = True
        except Exception as e:
            logger.debug(f"Week auto-inference failed: {e}")
        if week is None:
            return handle_validation_error(
                "Unable to infer current week from NFL state",
                {"transactions": [], "week": None, "count": 0}
            )

    # Range validation
    if week < LIMITS["round_min"] or week > LIMITS["round_max"]:
        return handle_validation_error(
            f"Week must be between {LIMITS['round_min']} and {LIMITS['round_max']}",
            {"transactions": [], "week": week, "count": 0}
        )

    headers = get_http_headers("sleeper_transactions")
    url = f"https://api.sleeper.app/v1/league/{league_id}/transactions/{week}"
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
                    # Direct terminal errors (no further retry)
                    if response.status_code == 404:
                        return create_error_response(
                            f"League or transactions endpoint not found for league '{league_id}' week {week}",
                            ErrorType.HTTP,
                            {"transactions": [], "week": week, "count": 0, "retries_used": attempts-1, "stale": False, "failure_reason": "not_found"}
                        )
                    err_type = ErrorType.ACCESS_DENIED
                    if response.status_code == 403:
                        msg = "Access denied: Transactions are private for this league"
                        help_text = "The league owner must adjust privacy settings to allow transaction viewing"
                    else:
                        msg = "Authentication required to view transactions for this private league"
                        help_text = "This league requires authentication. Contact the league owner for access"
                    return create_error_response(
                        msg,
                        err_type,
                        {"transactions": [], "week": week, "count": 0, "retries_used": attempts-1, "stale": False, "failure_reason": "access_denied", "access_help": help_text}
                    )
                if response.status_code == 429:
                    last_error = "rate_limited"
                    continue
                response.raise_for_status()
                tx_data = response.json()
                # Empty anomaly (treat like rosters) -> retry unless last attempt
                if isinstance(tx_data, list) and len(tx_data) == 0 and attempts < len(retry_delays):
                    last_error = "empty_transactions"
                    continue

                # Enrichment
                try:
                    cache = {}
                    # Determine season for usage/opponent enrichment
                    season = None
                    if ADVANCED_ENRICH_ENABLED:
                        try:
                            state = await get_nfl_state()
                            if state.get("success") and state.get("nfl_state"):
                                st = state["nfl_state"]
                                season = st.get("season") or st.get("league_season")
                        except Exception:
                            pass

                    def enrich_player(pid):
                        if pid in cache:
                            return cache[pid]
                        athlete = nfl_db.get_athlete_by_id(pid) or {}
                        obj = {"player_id": pid, "full_name": athlete.get("full_name"), "position": athlete.get("position")}
                        if ADVANCED_ENRICH_ENABLED and season:
                            try:
                                extra = _enrich_usage_and_opponent(nfl_db, athlete, season, week)
                                obj.update(extra)
                            except Exception:
                                pass
                        cache[pid] = obj; return obj
                    if isinstance(tx_data, list):
                        for tx in tx_data:
                            if not isinstance(tx, dict):
                                continue
                            adds = tx.get("adds") or {}
                            drops = tx.get("drops") or {}
                            if isinstance(adds, dict):
                                tx["adds_enriched"] = [enrich_player(pid) for pid in adds.keys()]
                            if isinstance(drops, dict):
                                tx["drops_enriched"] = [enrich_player(pid) for pid in drops.keys()]
                except Exception as enrich_error:
                    logger.debug(f"Transaction enrichment skipped: {enrich_error}")

                # Save snapshot
                nfl_db.save_transaction_snapshot(league_id, week, tx_data)
                return create_success_response({
                    "transactions": tx_data,
                    "week": week,
                    "auto_week_inferred": auto_inferred,
                    "count": len(tx_data),
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
                    {"transactions": [], "week": week, "count": 0, "retries_used": attempts-1, "stale": False, "failure_reason": "rate_limited"}
                )
            last_error = f"http:{getattr(he.response,'status_code','?')}"
            continue
        except httpx.NetworkError as ne:
            last_error = f"network:{ne}"
            continue
        except Exception as e:
            last_error = f"unexpected:{e}"
            continue

    # Fallback: transaction snapshot (specific week preferred)
    snap = nfl_db.load_transaction_snapshot(league_id, week)
    if not snap and auto_inferred:
        # If week was inferred and no snapshot, attempt last any-week snapshot
        snap = nfl_db.load_transaction_snapshot(league_id, None)
    if snap:
        return create_error_response(
            "Transaction fetch failed after retries (serving snapshot)",
            ErrorType.NETWORK if last_error and last_error.startswith("network") else ErrorType.UNEXPECTED,
            {
                "transactions": snap["transactions"],
                "week": snap.get("week", week),
                "auto_week_inferred": auto_inferred,
                "count": len(snap["transactions"]),
                "retries_used": attempts,
                "stale": snap.get("stale", True),
        "failure_reason": last_error or "unknown",
        "snapshot_fetched_at": snap.get("fetched_at"),
        "snapshot_age_seconds": snap.get("age_seconds")
            }
        )
    return create_error_response(
        f"Transaction fetch failed after retries: {last_error}",
        ErrorType.NETWORK if last_error and last_error.startswith("network") else ErrorType.UNEXPECTED,
    {"transactions": [], "week": week, "auto_week_inferred": auto_inferred, "count": 0, "retries_used": attempts, "stale": False, "failure_reason": last_error or "unknown", "snapshot_fetched_at": None, "snapshot_age_seconds": None}
    )


@handle_http_errors(
    default_data={"traded_picks": [], "count": 0},
    operation_name="fetching traded picks"
)
async def get_traded_picks(league_id: str) -> dict:
    """
    Get traded draft picks for a fantasy league from Sleeper API.
    
    This tool fetches information about draft picks that have been traded
    within the specified league.
    
    Args:
        league_id: The unique identifier for the league
        
    Returns:
        A dictionary containing:
        - traded_picks: List of traded draft picks
        - count: Number of traded picks found
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    headers = get_http_headers("sleeper_traded_picks")
    
    # Sleeper API endpoint for league traded picks
    url = f"https://api.sleeper.app/v1/league/{league_id}/traded_picks"
    
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        # Parse JSON response
        traded_picks_data = response.json()
        
        try:
            nfl_db = _init_db()
            cache = {}
            if isinstance(traded_picks_data, list):
                for tp in traded_picks_data:
                    if isinstance(tp, dict) and tp.get("player_id"):
                        tp["player_enriched"] = _enrich_single(nfl_db, tp["player_id"], cache)
        except Exception as e:
            logger.debug(f"Traded pick enrichment skipped: {e}")
        return create_success_response({
            "traded_picks": traded_picks_data,
            "count": len(traded_picks_data)
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
async def get_trending_players(nfl_db=None, trend_type: str = "add", lookback_hours: Optional[int] = 24, limit: Optional[int] = 25) -> dict:
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
        from .param_validator import validate_params, format_errors
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
            enriched_players.append({
                "player_id": player_id,
                "count": count,
                "enriched": base_info
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
async def get_draft_picks(draft_id: str) -> dict:  # type: ignore[override]
    """Override earlier definition to add enrichment (keeps same name).

    Returns picks with additive `player_enriched` for each pick containing
    player_id if available.
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


# Strategic Planning Functions for Forward-Looking Analysis

@handle_http_errors(
    default_data={"strategic_preview": {}, "weeks_analyzed": 0, "league_id": None},
    operation_name="generating strategic matchup preview"
)
async def get_strategic_matchup_preview(league_id: str, current_week: int, weeks_ahead: int = 4) -> dict:
    """
    Generate a strategic preview of upcoming matchups for multiple weeks ahead.
    
    This strategic tool combines Sleeper league matchup data with NFL schedule insights
    to provide early warning about upcoming challenges, bye weeks, and opportunities.
    Helps fantasy managers plan trades, waiver claims, and lineup strategies weeks in advance.
    
    Args:
        league_id: The unique identifier for the league
        current_week: The current NFL week (1-22)
        weeks_ahead: Number of weeks to analyze ahead (1-8, defaults to 4)
        
    Returns:
        A dictionary containing:
        - strategic_preview: Multi-week analysis with recommendations
        - weeks_analyzed: Number of weeks analyzed
        - league_id: The league identifier
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate parameters via param_validator
    try:
        from .param_validator import validate_params, format_errors
        schema = {
            "current_week": {"type": int, "required": True, "min": LIMITS["week_min"], "max": LIMITS["week_max"]},
            "weeks_ahead": {"type": int, "required": False, "min": 1, "max": 8, "default": 4},
        }
        validated, errors = validate_params(schema, {"current_week": current_week, "weeks_ahead": weeks_ahead})
        if errors:
            # Legacy phrasing preservation
            msgs = []
            for e in errors:
                if e.startswith("'current_week' must be >=") or e.startswith("'current_week' must be <="):
                    return handle_validation_error(
                        f"Current week must be between {LIMITS['week_min']} and {LIMITS['week_max']}",
                        {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
                    )
                if e.startswith("'weeks_ahead' must be >=") or e.startswith("'weeks_ahead' must be <="):
                    return handle_validation_error(
                        "Weeks ahead must be between 1 and 8",
                        {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
                    )
                msgs.append(e)
            if msgs:
                return handle_validation_error(
                    format_errors(msgs),
                    {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
                )
        current_week = validated["current_week"]
        weeks_ahead = validated.get("weeks_ahead", 4)
    except Exception:
        if current_week < LIMITS["week_min"] or current_week > LIMITS["week_max"]:
            return handle_validation_error(
                f"Current week must be between {LIMITS['week_min']} and {LIMITS['week_max']}",
                {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
            )
        if weeks_ahead < 1 or weeks_ahead > 8:
            return handle_validation_error(
                "Weeks ahead must be between 1 and 8",
                {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
            )
    
    strategic_data = {
        "analysis_period": f"Week {current_week} through Week {current_week + weeks_ahead - 1}",
        "weeks": {},
        "summary": {
            "critical_bye_weeks": [],
            "high_opportunity_weeks": [],
            "challenging_weeks": [],
            "trade_recommendations": []
        }
    }
    
    # Import NFL tools here to avoid circular imports
    from . import nfl_tools
    
    # Get league information for context
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            {
                "strategic_preview": {},
                "weeks_analyzed": 0,
                "league_id": league_id
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )
    
    # Analyze each upcoming week
    weeks_analyzed = 0
    for week_offset in range(weeks_ahead):
        target_week = current_week + week_offset
        if target_week > LIMITS["week_max"]:
            break
            
        # Get matchups for this week
        matchups = await get_matchups(league_id, target_week)
        if not matchups.get("success", True):
            continue
            
        week_analysis = {
            "week_number": target_week,
            "matchup_count": matchups.get("count", 0),
            "strategic_insights": [],
            "bye_week_teams": [],
            "recommended_actions": []
        }
        
        # Analyze NFL bye weeks for this week (sample analysis - in real implementation 
        # you'd analyze all 32 teams to find which have byes this week)
        sample_teams = ["KC", "BUF", "SF", "DAL", "LAR", "PHI", "MIA", "CIN"]
        
        for team in sample_teams:
            try:
                team_schedule = await nfl_tools.get_team_schedule(team, 2025)
                if team_schedule.get("success", False):
                    schedule = team_schedule.get("schedule", [])
                    for game in schedule:
                        if (game.get("week") == target_week and 
                            "BYE WEEK" in game.get("fantasy_implications", [])):
                            week_analysis["bye_week_teams"].append({
                                "team": team,
                                "impact": "High - Consider backup options or trades"
                            })
                            strategic_data["summary"]["critical_bye_weeks"].append({
                                "week": target_week,
                                "team": team
                            })
            except Exception:
                # Skip team if schedule unavailable
                continue
        
        # Add strategic insights based on week timing
        if target_week == current_week:
            week_analysis["strategic_insights"].append("Current week - Focus on injury reports and last-minute changes")
        elif target_week == current_week + 1:
            week_analysis["strategic_insights"].append("Next week - Prime time for waiver claims and trades")
        elif target_week <= 13:
            week_analysis["strategic_insights"].append("Regular season - Build for playoffs")
        else:
            week_analysis["strategic_insights"].append("Playoff push - Prioritize high-floor players")
        
        # Add recommendations based on bye weeks
        if week_analysis["bye_week_teams"]:
            week_analysis["recommended_actions"].append(
                f"Plan for {len(week_analysis['bye_week_teams'])} bye week teams - "
                "consider trades or waiver pickups 1-2 weeks early"
            )
            strategic_data["summary"]["challenging_weeks"].append(target_week)
        else:
            week_analysis["recommended_actions"].append("No major bye weeks - good week to be aggressive")
            strategic_data["summary"]["high_opportunity_weeks"].append(target_week)
        
        strategic_data["weeks"][f"week_{target_week}"] = week_analysis
        weeks_analyzed += 1
    
    # Generate overall strategic recommendations
    if strategic_data["summary"]["critical_bye_weeks"]:
        strategic_data["summary"]["trade_recommendations"].append(
            "Consider trading for depth before major bye weeks hit your key players"
        )
    
    if len(strategic_data["summary"]["challenging_weeks"]) >= 2:
        strategic_data["summary"]["trade_recommendations"].append(
            "Multiple challenging weeks ahead - prioritize roster flexibility"
        )
    
    return create_success_response({
        "strategic_preview": strategic_data,
        "weeks_analyzed": weeks_analyzed,
        "league_id": league_id
    })


@handle_http_errors(
    default_data={"coordination_plan": {}, "season": None, "league_id": None},
    operation_name="coordinating season bye weeks"
)
async def get_season_bye_week_coordination(league_id: str, season: int = 2025) -> dict:
    """
    Coordinate your fantasy league schedule with NFL bye weeks for season-long planning.
    
    This strategic tool analyzes the entire NFL season's bye week schedule and correlates
    it with your fantasy league's playoff schedule to identify optimal trading periods,
    waiver claim timing, and roster construction strategies.
    
    Args:
        league_id: The unique identifier for the league
        season: Season year (defaults to 2025)
        
    Returns:
        A dictionary containing:
        - coordination_plan: Season-long strategic plan
        - season: Season year
        - league_id: League identifier
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    from . import nfl_tools
    
    # Get league information for playoff schedule context
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            {
                "coordination_plan": {},
                "season": season,
                "league_id": league_id
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )
    
    league_data = league_info.get("league", {})
    playoff_start = league_data.get("settings", {}).get("playoff_week_start", 14)
    regular_season_weeks = playoff_start - 1
    
    coordination_plan = {
        "season_overview": {
            "regular_season_weeks": regular_season_weeks,
            "playoff_start_week": playoff_start,
            "trade_deadline": league_data.get("settings", {}).get("trade_deadline", 13)
        },
        "bye_week_calendar": {},
        "strategic_periods": {
            "early_season": {"weeks": [1, 2, 3], "focus": "Observe and collect data"},
            "bye_week_preparation": {"weeks": [], "focus": "Build roster depth"},
            "trade_deadline_push": {"weeks": [], "focus": "Final roster optimization"}, 
            "playoff_preparation": {"weeks": [], "focus": "Secure playoff position"}
        },
        "recommendations": []
    }
    
    # Analyze bye weeks for all NFL teams (sample of major fantasy teams)
    major_fantasy_teams = [
        "KC", "BUF", "SF", "DAL", "LAR", "PHI", "MIA", "CIN", 
        "BAL", "GB", "MIN", "NYJ", "DEN", "LV", "ATL", "TB"
    ]
    
    bye_weeks_by_week = {}
    
    for team in major_fantasy_teams:
        try:
            team_schedule = await nfl_tools.get_team_schedule(team, season)
            if team_schedule.get("success", False):
                schedule = team_schedule.get("schedule", [])
                for game in schedule:
                    if "BYE WEEK" in game.get("fantasy_implications", []):
                        week_num = game.get("week")
                        if week_num:
                            if week_num not in bye_weeks_by_week:
                                bye_weeks_by_week[week_num] = []
                            bye_weeks_by_week[week_num].append(team)
        except Exception:
            continue
    
    # Organize bye weeks in calendar format
    for week, teams in bye_weeks_by_week.items():
        coordination_plan["bye_week_calendar"][f"week_{week}"] = {
            "week": week,
            "teams_on_bye": teams,
            "team_count": len(teams),
            "strategic_impact": "High" if len(teams) >= 4 else "Medium" if len(teams) >= 2 else "Low",
            "recommended_prep_week": max(1, week - 2)
        }
    
    # Identify strategic periods
    heavy_bye_weeks = [week for week, teams in bye_weeks_by_week.items() if len(teams) >= 4]
    if heavy_bye_weeks:
        prep_weeks = [max(1, week - 2) for week in heavy_bye_weeks]
        coordination_plan["strategic_periods"]["bye_week_preparation"]["weeks"] = prep_weeks
    
    # Trade deadline preparation
    trade_deadline = coordination_plan["season_overview"]["trade_deadline"]
    coordination_plan["strategic_periods"]["trade_deadline_push"]["weeks"] = [
        trade_deadline - 2, trade_deadline - 1, trade_deadline
    ]
    
    # Playoff preparation
    coordination_plan["strategic_periods"]["playoff_preparation"]["weeks"] = [
        playoff_start - 3, playoff_start - 2, playoff_start - 1
    ]
    
    # Generate strategic recommendations
    if heavy_bye_weeks:
        coordination_plan["recommendations"].append({
            "priority": "High",
            "action": f"Weeks {', '.join(map(str, heavy_bye_weeks))} have heavy bye weeks",
            "timing": f"Start preparing 2-3 weeks early (weeks {', '.join(map(str, prep_weeks))})",
            "strategy": "Build roster depth through trades or strategic waiver claims"
        })
    
    coordination_plan["recommendations"].append({
        "priority": "Medium",
        "action": "Trade deadline preparation",
        "timing": f"Weeks {trade_deadline - 2} through {trade_deadline}",
        "strategy": "Evaluate roster needs and make final strategic moves"
    })
    
    coordination_plan["recommendations"].append({
        "priority": "High", 
        "action": "Playoff preparation",
        "timing": f"Weeks {playoff_start - 3} through {playoff_start - 1}",
        "strategy": "Optimize lineup for playoff schedule and prioritize high-floor players"
    })
    
    return create_success_response({
        "coordination_plan": coordination_plan,
        "season": season,
        "league_id": league_id
    })


@handle_http_errors(
    default_data={"trade_analysis": {}, "league_id": None, "current_week": None},
    operation_name="analyzing trade deadline strategy"
)
async def get_trade_deadline_analysis(league_id: str, current_week: int) -> dict:
    """
    Analyze optimal trade timing based on league schedule and NFL events.
    
    This strategic tool evaluates when to make trades by analyzing upcoming bye weeks,
    playoff schedules, and league transaction patterns to maximize competitive advantage.
    
    Args:
        league_id: The unique identifier for the league
        current_week: Current NFL week for timing analysis
        
    Returns:
        A dictionary containing:
        - trade_analysis: Strategic trade timing analysis
        - league_id: League identifier
        - current_week: Current week reference
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Get league information
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            {
                "trade_analysis": {},
                "league_id": league_id,
                "current_week": current_week
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )
    
    league_data = league_info.get("league", {})
    settings = league_data.get("settings", {})
    trade_deadline = settings.get("trade_deadline", 13)
    playoff_start = settings.get("playoff_week_start", 14)
    
    trade_analysis = {
        "timing_analysis": {
            "current_week": current_week,
            "trade_deadline": trade_deadline,
            "weeks_until_deadline": max(0, trade_deadline - current_week),
            "playoff_start": playoff_start,
            "weeks_until_playoffs": max(0, playoff_start - current_week)
        },
        "strategic_windows": {},
        "recommendations": [],
        "urgency_factors": []
    }
    
    # Determine strategic windows based on timing
    weeks_to_deadline = trade_deadline - current_week
    
    if weeks_to_deadline > 4:
        trade_analysis["strategic_windows"]["current_phase"] = "Early Season"
        trade_analysis["strategic_windows"]["strategy"] = "Observe and identify undervalued assets"
        trade_analysis["strategic_windows"]["urgency"] = "Low"
        trade_analysis["recommendations"].append({
            "action": "Monitor performance trends",
            "reasoning": "Plenty of time to evaluate players and identify trade targets",
            "priority": "Low"
        })
    elif weeks_to_deadline > 2:
        trade_analysis["strategic_windows"]["current_phase"] = "Prime Trading Window"
        trade_analysis["strategic_windows"]["strategy"] = "Actively pursue beneficial trades"
        trade_analysis["strategic_windows"]["urgency"] = "Medium"
        trade_analysis["recommendations"].append({
            "action": "Execute strategic trades now",
            "reasoning": f"Only {weeks_to_deadline} weeks until deadline - optimal time to trade",
            "priority": "High"
        })
    elif weeks_to_deadline > 0:
        trade_analysis["strategic_windows"]["current_phase"] = "Trade Deadline Crunch"
        trade_analysis["strategic_windows"]["strategy"] = "Make final critical moves"
        trade_analysis["strategic_windows"]["urgency"] = "High"
        trade_analysis["recommendations"].append({
            "action": "Complete all pending trades immediately",
            "reasoning": f"Only {weeks_to_deadline} week(s) left - last chance for trades",
            "priority": "Critical"
        })
        trade_analysis["urgency_factors"].append("Trade deadline imminent")
    else:
        trade_analysis["strategic_windows"]["current_phase"] = "Post-Deadline"
        trade_analysis["strategic_windows"]["strategy"] = "Focus on waiver wire and lineup optimization"
        trade_analysis["strategic_windows"]["urgency"] = "N/A"
        trade_analysis["recommendations"].append({
            "action": "Switch to waiver-based strategy",
            "reasoning": "Trade deadline has passed - only waivers and free agents available",
            "priority": "Medium"
        })
    
    # Analyze upcoming bye weeks to inform trade urgency
    upcoming_preview = await get_strategic_matchup_preview(league_id, current_week, 4)
    if upcoming_preview.get("success", False):
        preview_data = upcoming_preview.get("strategic_preview", {})
        critical_byes = preview_data.get("summary", {}).get("critical_bye_weeks", [])
        
        if critical_byes and weeks_to_deadline > 0:
            bye_weeks = [bye["week"] for bye in critical_byes]
            trade_analysis["urgency_factors"].append(
                f"Critical bye weeks coming in weeks {', '.join(map(str, bye_weeks))}"
            )
            trade_analysis["recommendations"].append({
                "action": "Trade for bye week coverage",
                "reasoning": f"Address bye weeks in weeks {', '.join(map(str, bye_weeks))} before deadline",
                "priority": "High"
            })
    
    # Add playoff preparation considerations
    if current_week >= playoff_start - 4:
        trade_analysis["urgency_factors"].append("Playoff preparation phase")
        trade_analysis["recommendations"].append({
            "action": "Prioritize playoff schedule strength",
            "reasoning": "Focus on players with favorable playoff matchups",
            "priority": "High"
        })
    
    return create_success_response({
        "trade_analysis": trade_analysis,
        "league_id": league_id,
        "current_week": current_week
    })


@handle_http_errors(
    default_data={"playoff_plan": {}, "league_id": None, "readiness_score": 0},
    operation_name="generating playoff preparation plan"
)
async def get_playoff_preparation_plan(league_id: str, current_week: int) -> dict:
    """
    Generate comprehensive playoff preparation plan combining league and NFL data.
    
    This strategic tool analyzes your league's playoff structure, upcoming NFL schedules,
    and provides a detailed preparation plan to maximize playoff success including
    roster optimization, matchup analysis, and strategic timing recommendations.
    
    Args:
        league_id: The unique identifier for the league
        current_week: Current NFL week for timeline analysis
        
    Returns:
        A dictionary containing:
        - playoff_plan: Comprehensive playoff preparation strategy
        - league_id: League identifier  
        - readiness_score: Playoff readiness assessment (0-100)
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    from . import nfl_tools
    
    # Get league information for playoff structure
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            {
                "playoff_plan": {},
                "league_id": league_id,
                "readiness_score": 0
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )
    
    league_data = league_info.get("league", {})
    settings = league_data.get("settings", {})
    
    playoff_start = settings.get("playoff_week_start", 14)
    playoff_weeks = settings.get("playoff_week_start", 14)
    total_teams = league_data.get("total_rosters", 12)
    playoff_teams = settings.get("playoff_teams", 6)
    
    playoff_plan = {
        "timeline": {
            "current_week": current_week,
            "playoff_start": playoff_start,
            "weeks_to_playoffs": max(0, playoff_start - current_week),
            "playoff_duration": 4,  # Typical playoff duration
            "championship_week": playoff_start + 3
        },
        "preparation_phases": {},
        "strategic_priorities": [],
        "nfl_schedule_analysis": {},
        "recommendations": [],
        "readiness_assessment": {
            "roster_depth": "TBD",
            "schedule_strength": "TBD", 
            "bye_week_planning": "TBD",
            "overall_score": 0
        }
    }
    
    weeks_to_playoffs = playoff_start - current_week
    
    # Define preparation phases based on timeline
    if weeks_to_playoffs > 4:
        current_phase = "Early Preparation"
        phase_strategy = "Build depth and monitor targets"
        phase_urgency = "Low"
        playoff_plan["strategic_priorities"] = [
            "Monitor playoff-bound teams for strong schedules",
            "Identify undervalued players on strong teams",
            "Build roster depth for injury protection",
            "Track trending players and waiver targets"
        ]
    elif weeks_to_playoffs > 2:
        current_phase = "Active Preparation" 
        phase_strategy = "Execute strategic moves"
        phase_urgency = "Medium"
        playoff_plan["strategic_priorities"] = [
            "Make trades for playoff-schedule advantages",
            "Secure handcuffs for star players",
            "Target players on motivated teams",
            "Avoid players on teams likely to rest starters"
        ]
    elif weeks_to_playoffs > 0:
        current_phase = "Final Preparation"
        phase_strategy = "Lock in playoff roster"
        phase_urgency = "High"
        playoff_plan["strategic_priorities"] = [
            "Finalize optimal lineup combinations", 
            "Secure must-have waiver pickups",
            "Plan for potential star player rest",
            "Optimize for high floor over high ceiling"
        ]
    else:
        current_phase = "Playoff Execution"
        phase_strategy = "Win now mode"
        phase_urgency = "Critical"
        playoff_plan["strategic_priorities"] = [
            "Start highest floor players",
            "Monitor injury reports closely",
            "Consider game flow and scripts",
            "Avoid risky boom-or-bust plays"
        ]
    
    playoff_plan["preparation_phases"]["current_phase"] = {
        "name": current_phase,
        "strategy": phase_strategy,
        "urgency": phase_urgency,
        "weeks_remaining": max(0, weeks_to_playoffs)
    }
    
    # Analyze NFL playoff schedule implications (sample key teams)
    key_teams = ["KC", "BUF", "SF", "DAL", "PHI", "MIA", "BAL", "CIN"]
    
    for team in key_teams:
        try:
            team_schedule = await nfl_tools.get_team_schedule(team, 2025)
            if team_schedule.get("success", False):
                schedule = team_schedule.get("schedule", [])
                
                # Analyze playoff weeks (weeks 14-17 typically)
                playoff_games = [g for g in schedule if g.get("week", 0) >= playoff_start and g.get("week", 0) <= playoff_start + 3]
                
                if playoff_games:
                    fantasy_implications = []
                    for game in playoff_games:
                        implications = game.get("fantasy_implications", [])
                        fantasy_implications.extend(implications[:2])  # Limit to key insights
                    
                    playoff_plan["nfl_schedule_analysis"][team] = {
                        "playoff_games_count": len(playoff_games),
                        "key_insights": fantasy_implications[:3],  # Top 3 insights
                        "recommendation": "Target" if len(playoff_games) >= 3 else "Monitor"
                    }
        except Exception:
            continue
    
    # Generate specific recommendations based on timeline
    if weeks_to_playoffs > 0:
        playoff_plan["recommendations"].append({
            "category": "Roster Management",
            "action": f"Optimize roster in next {weeks_to_playoffs} weeks",
            "priority": "High" if weeks_to_playoffs <= 2 else "Medium",
            "deadline": f"Week {playoff_start - 1}"
        })
    
    if current_week <= 13: # Before typical trade deadline
        playoff_plan["recommendations"].append({
            "category": "Trading Strategy", 
            "action": "Target players with strong playoff schedules",
            "priority": "High",
            "deadline": "Trade deadline"
        })
    playoff_plan["recommendations"].append({
        "category": "Waiver Wire",
        "action": "Prioritize high-floor players over boom-bust options",
        "priority": "Medium",
        "deadline": "Ongoing"
    })
    
    # Calculate readiness score (simplified scoring system)
    readiness_score = 50  # Base score
    
    if weeks_to_playoffs > 2:
        readiness_score += 20  # Bonus for having time to prepare
    elif weeks_to_playoffs == 0:
        readiness_score += 30  # Bonus for being in playoffs
    
    if len(playoff_plan["nfl_schedule_analysis"]) >= 4:
        readiness_score += 15  # Bonus for good schedule analysis
    
    if current_phase in ["Active Preparation", "Final Preparation"]:
        readiness_score += 10  # Bonus for being in optimal prep phase
    
    playoff_plan["readiness_assessment"]["overall_score"] = min(100, readiness_score)
    
    # Set readiness levels based on score
    if readiness_score >= 80:
        playoff_plan["readiness_assessment"]["roster_depth"] = "Excellent"
        playoff_plan["readiness_assessment"]["schedule_strength"] = "Strong"
        playoff_plan["readiness_assessment"]["bye_week_planning"] = "Well Prepared"
    elif readiness_score >= 60:
        playoff_plan["readiness_assessment"]["roster_depth"] = "Good"
        playoff_plan["readiness_assessment"]["schedule_strength"] = "Adequate"
        playoff_plan["readiness_assessment"]["bye_week_planning"] = "Prepared"
    else:
        playoff_plan["readiness_assessment"]["roster_depth"] = "Needs Work"
        playoff_plan["readiness_assessment"]["schedule_strength"] = "Challenging"
        playoff_plan["readiness_assessment"]["bye_week_planning"] = "Behind Schedule"
    
    return create_success_response({
        "playoff_plan": playoff_plan,
        "league_id": league_id,
        "readiness_score": readiness_score
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
    default_data={"picks": [], "count": 0},
    operation_name="fetching draft picks"
)
async def get_draft_picks(draft_id: str) -> dict:
    """Fetch all picks in a draft."""
    headers = get_http_headers("sleeper_league")
    url = f"https://api.sleeper.app/v1/draft/{draft_id}/picks"
    async with create_http_client() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        return create_success_response({"picks": data, "count": len(data)})


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
async def get_fantasy_context(league_id: str, week: Optional[int] = None, include: Optional[str] = None) -> dict:
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

    # Parallelizable fetches executed sequentially here (simplicity, avoid new deps)
    if "rosters" in wanted:
        rosters_resp = await get_rosters(league_id)
        if rosters_resp.get("success"):
            context["rosters"] = rosters_resp.get("rosters")
    if "users" in wanted:
        users_resp = await get_league_users(league_id)
        if users_resp.get("success"):
            context["users"] = users_resp.get("users")

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

    # Fetch week bound data
    if "matchups" in wanted and effective_week is not None:
        matchups_resp = await get_matchups(league_id, effective_week)
        if matchups_resp.get("success"):
            context["matchups"] = matchups_resp.get("matchups")
    if "transactions" in wanted:
        tx_resp = await get_transactions(league_id, week=effective_week)
        if tx_resp.get("success"):
            context["transactions"] = tx_resp.get("transactions")

    return create_success_response({
        "context": context,
        "league_id": league_id,
        "week": effective_week,
        "auto_week_inferred": auto_inferred
    })


ADVANCED_ENRICH_ENABLED = os.getenv("NFL_MCP_ADVANCED_ENRICH") == "1"

async def _fetch_week_player_snaps(season: int, week: int):
    """Fetch player snap stats (best-effort) from Sleeper weekly stats endpoint.

    Returns list of dicts for upsert_player_week_stats. If advanced enrichment disabled
    or network/API issues occur, returns empty list.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug(f"[Fetch Snaps] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []
    
    logger.info(f"[Fetch Snaps] Starting fetch for season={season}, week={week}")
    headers = get_http_headers("sleeper_week_stats")
    # Sleeper weekly stats endpoint pattern (regular season)
    # Using documented style: /v1/stats/nfl/regular/{season}/{week}
    url = f"https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}"
    try:
        async with create_http_client() as client:
            resp = await client.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"[Fetch Snaps] API returned status {resp.status_code}")
                return []
            data = resp.json() or {}
            # Data format: mapping of player_id -> stat dict (varies by Sleeper)
            if not isinstance(data, dict):
                logger.warning(f"[Fetch Snaps] Invalid data format (not dict)")
                return []
            
            logger.debug(f"[Fetch Snaps] Received data for {len(data)} players")
            rows = []
            for pid, stats in list(data.items())[:5000]:  # cap for safety
                if not isinstance(stats, dict):
                    continue
                # Attempt to extract snaps & snap_pct fields (naming may vary)
                snaps = stats.get("snaps") or stats.get("off_snaps") or stats.get("offense_snaps")
                team_snaps = stats.get("team_snaps") or stats.get("off_team_snaps")
                snap_pct = stats.get("snap_pct") or stats.get("off_snap_pct")
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
    except Exception as e:
        logger.error(f"[Fetch Snaps] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []

async def _fetch_week_schedule(season: int, week: int):
    """Fetch weekly schedule from ESPN scoreboard API (best-effort).

    Returns list of bidirectional game rows for upsert_schedule_games.
    If advanced enrichment disabled or failure occurs, returns empty list.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug(f"[Fetch Schedule] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []
    
    logger.info(f"[Fetch Schedule] Starting fetch for season={season}, week={week}")
    # Regular season scoreboard: seasontype=2
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&year={season}&seasontype=2"
    try:
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
            
            logger.info(f"[Fetch Schedule] Successfully fetched {len(games)} game records ({len(events)} events, season={season}, week={week})")
            return games
    except Exception as e:
        logger.error(f"[Fetch Schedule] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []

async def _fetch_all_team_schedules(season: int):
    """Fetch full season schedules for all 32 NFL teams from ESPN Team Schedule API.
    
    This prefetches complete schedules (all weeks) for every team to warm the cache.
    Useful for startup/initial cache population.
    
    Args:
        season: Season year (e.g., 2025)
        
    Returns:
        List of game dicts for upsert_schedule_games (bidirectional rows)
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug(f"[Fetch All Schedules] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
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
        logger.debug(f"[Fetch Injuries] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []
    
    logger.info(f"[Fetch Injuries] Starting fetch for all teams")
    
    # NFL team abbreviations
    teams = [
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
        "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
        "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
        "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WAS"
    ]
    
    all_injuries = []
    
    try:
        import httpx
        from .config import get_http_headers, create_http_client
        
        headers = get_http_headers("nfl_teams")
        
        async with create_http_client() as client:
            for team in teams:
                try:
                    url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{team}/injuries?limit=50"
                    resp = await client.get(url, headers=headers)
                    
                    if resp.status_code != 200:
                        logger.debug(f"[Fetch Injuries] Team {team}: status {resp.status_code}")
                        continue
                    
                    data = resp.json()
                    injuries_data = data.get('items', [])
                    
                    for injury_item in injuries_data:
                        # Extract athlete info
                        athlete_ref = injury_item.get('athlete', {})
                        if not athlete_ref:
                            continue
                        
                        injury = {
                            'player_id': str(athlete_ref.get('id', '')),
                            'player_name': athlete_ref.get('displayName', 'Unknown'),
                            'team_id': team,
                            'position': athlete_ref.get('position', {}).get('abbreviation'),
                            'injury_status': injury_item.get('status', {}).get('name', 'Unknown'),
                            'injury_type': injury_item.get('type', {}).get('name'),
                            'injury_description': injury_item.get('description'),
                            'date_reported': injury_item.get('date')
                        }
                        all_injuries.append(injury)
                
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
    
    Note: This now uses the injuries endpoint which includes practice participation status.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug(f"[Fetch Practice] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []
    
    logger.info(f"[Fetch Practice] Starting fetch for season={season}, week={week}")
    
    # Use injury reports as source for practice status
    # Practice status is often reflected in injury reports (DNP/Limited/Full)
    injuries = await _fetch_injuries()
    
    if not injuries:
        logger.warning(f"[Fetch Practice] No injury data available to extract practice status")
        return []
    
    # Convert injury status to practice status format
    practice_reports = []
    now = datetime.now(UTC).isoformat()
    
    for inj in injuries:
        status = inj.get('injury_status', '').upper()
        
        # Map injury status to practice participation
        practice_status = None
        if 'OUT' in status or 'IR' in status or 'PUP' in status:
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
    
    logger.info(f"[Fetch Practice] Extracted {len(practice_reports)} practice status records from {len(injuries)} injuries")
    return practice_reports

async def _fetch_weekly_usage_stats(season: int, week: int):
    """Fetch weekly usage statistics (targets, routes, RZ touches) from available sources.

    Returns list of dicts for upsert_usage_stats.
    Attempts Sleeper stats first, falls back to ESPN if needed.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug(f"[Fetch Usage] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []
    
    logger.info(f"[Fetch Usage] Starting fetch for season={season}, week={week}")
    
    # Try Sleeper weekly stats endpoint first
    headers = get_http_headers("sleeper_week_stats")
    url = f"https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}"
    try:
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
                        targets = player_stats.get("rec_tgt") or player_stats.get("targets")
                        routes = player_stats.get("routes_run") or player_stats.get("routes")
                        rz_touches = player_stats.get("rz_touches") or player_stats.get("redzone_touches")
                        touches = player_stats.get("touches")
                        air_yards = player_stats.get("rec_air_yds") or player_stats.get("air_yards")
                        snap_share = player_stats.get("snap_pct")
                        
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
                        logger.info(f"[Fetch Usage] Successfully fetched {len(stats)} usage records (season={season}, week={week})")
                        return stats
                    else:
                        logger.warning(f"[Fetch Usage] No valid usage stats found in response")
            else:
                logger.warning(f"[Fetch Usage] Sleeper API returned status {resp.status_code}")
    except Exception as e:
        logger.error(f"[Fetch Usage] Sleeper API failed: {e}", exc_info=True)
    
    # Fallback: ESPN (limited coverage, best-effort)
    # Note: ESPN player stats API may require iterating by position or fetching league leaders
    # For simplicity, skip ESPN fallback here (can be extended later)
    logger.warning(f"[Fetch Usage] No usage stats available from any source for season={season}, week={week}")
    return []

def _estimate_snap_pct(depth_rank: Optional[int]) -> Optional[float]:
    if depth_rank is None:
        return None
    if depth_rank == 1:
        return 70.0
    if depth_rank == 2:
        return 45.0
    return 15.0

def _enrich_usage_and_opponent(nfl_db, athlete: Dict, season: Optional[int], week: Optional[int]) -> Dict:
    """Add snap_pct/opponent fields to a base enrichment object (mutates and returns)."""
    if not athlete:
        return {}
    
    enriched_additions: Dict = {}
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
            est = _estimate_snap_pct(depth_rank)
            if est is not None:
                enriched_additions["snap_pct"] = est
                enriched_additions["snap_pct_source"] = "estimated"
                logger.debug(f"[Enrichment] {player_name}: snap_pct={est}% (estimated from depth={depth_rank})")
    
    # Opponent for DEF
    if season and week and position == "DEF" and hasattr(nfl_db, 'get_opponent'):
        opponent = nfl_db.get_opponent(season, week, athlete.get("team_id"))
        if opponent:
            enriched_additions["opponent"] = opponent
            enriched_additions["opponent_source"] = "cached"
            logger.debug(f"[Enrichment] {player_name} (DEF): opponent={opponent} (cached)")
    
    # Injury status - all positions
    if player_id and hasattr(nfl_db, 'get_player_injury_from_cache'):
        injury = nfl_db.get_player_injury_from_cache(player_id, max_age_hours=12)
        if injury:
            age_hours = (datetime.now(UTC) - datetime.fromisoformat(injury["updated_at"])).total_seconds() / 3600
            enriched_additions["injury_status"] = injury["injury_status"]
            enriched_additions["injury_type"] = injury.get("injury_type")
            enriched_additions["injury_description"] = injury.get("injury_description")
            enriched_additions["injury_date"] = injury.get("date_reported")
            enriched_additions["injury_age_hours"] = round(age_hours, 1)
            enriched_additions["injury_stale"] = age_hours > 12
            logger.debug(f"[Enrichment] {player_name}: injury_status={injury['injury_status']} (age={round(age_hours, 1)}h)")
    
    # Practice status (DNP/LP/FP) - all positions
    if player_id and hasattr(nfl_db, 'get_latest_practice_status'):
        practice = nfl_db.get_latest_practice_status(player_id, max_age_hours=72)
        if practice:
            age_hours = (datetime.now(UTC) - datetime.fromisoformat(practice["updated_at"])).total_seconds() / 3600
            enriched_additions["practice_status"] = practice["status"]
            enriched_additions["practice_status_date"] = practice["date"]
            enriched_additions["practice_status_age_hours"] = round(age_hours, 1)
            enriched_additions["practice_status_stale"] = age_hours > 72
            logger.debug(f"[Enrichment] {player_name}: practice_status={practice['status']} (age={round(age_hours, 1)}h)")
    
    # Usage stats (targets, routes, RZ touches) - offensive skill positions
    if season and week and position in ("WR", "RB", "TE") and hasattr(nfl_db, 'get_usage_last_n_weeks'):
        usage = nfl_db.get_usage_last_n_weeks(player_id, season, week, n=3)
        if usage:
            enriched_additions["usage_last_3_weeks"] = {
                "targets_avg": round(usage["targets_avg"], 1) if usage["targets_avg"] else None,
                "routes_avg": round(usage["routes_avg"], 1) if usage["routes_avg"] else None,
                "rz_touches_avg": round(usage["rz_touches_avg"], 1) if usage["rz_touches_avg"] else None,
                "snap_share_avg": round(usage["snap_share_avg"], 1) if usage["snap_share_avg"] else None,
                "weeks_sample": usage["weeks_sample"]
            }
            enriched_additions["usage_source"] = "sleeper"
            logger.debug(
                f"[Enrichment] {player_name}: usage_last_3wks="
                f"tgt={usage['targets_avg']:.1f}, routes={usage['routes_avg']:.1f}, "
                f"rz={usage['rz_touches_avg']:.1f} (n={usage['weeks_sample']})"
            )
    
    if enriched_additions:
        logger.info(f"[Enrichment] {player_name}: Added {len(enriched_additions)} enrichment fields")
    
    return enriched_additions