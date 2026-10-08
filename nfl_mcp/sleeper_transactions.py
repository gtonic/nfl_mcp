"""Sleeper transactions tools (split out of sleeper_tools.py).

get_transactions (week-inferring, robust with snapshot fallback) and
get_traded_picks. Consumers of the core Sleeper primitives + the enrichment
layer; re-exported from sleeper_tools for backward compatibility.
"""
import asyncio
import logging

import httpx

from .config import (
    LIMITS,
    create_http_client,
    get_http_headers,
)
from .errors import (
    ErrorType,
    create_error_response,
    create_success_response,
    handle_http_errors,
    handle_validation_error,
)
from .sleeper_enrichment import _enrich_usage_and_opponent
from .sleeper_tools import (
    _enrich_single,
    _init_db,
    get_nfl_state,
    note_transactions,
    sleeper_get_fresh,
)

logger = logging.getLogger(__name__)


async def get_transactions(league_id: str, round: int | None = None, week: int | None = None,
                           include_previous_leg: bool | None = False) -> dict:
    """Get COMPLETED transactions for a week of a Sleeper league.

    IMPORTANT — pending waiver claims are not here. Sleeper exposes a claim only
    once it has been processed; a claim sitting in a manager's queue appears in
    no API response. An empty list therefore means "nothing has been processed",
    NOT "nobody has claims in". Reading it as the latter produced a confident
    "you have no claims pending" while two were waiting in the league app.

    There is no public endpoint for pending claims. The league app is the only
    place to see them.

    Legs: Sleeper files the Wednesday waiver run under the leg it closes, so on
    the Wednesday of week N (``nfl_state.week`` N) the claims processed that
    morning are in leg N-1 and leg N is still empty. ``include_previous_leg``
    True merges leg N-1 into the answer (deduped by transaction_id, each row
    keeping Sleeper's ``leg``); None does that only for the current week (or
    an inferred one); False (the default) reads exactly one leg, which callers
    that already walk several weeks rely on.

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
        from .param_validator import format_errors, validate_params
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

    if include_previous_leg is None:
        include_previous_leg = auto_inferred or week == await _current_week()
    if not include_previous_leg or week <= LIMITS["round_min"]:
        return await _fetch_week(league_id, week, auto_inferred)
    current, previous = await asyncio.gather(
        _fetch_week(league_id, week, auto_inferred),
        _fetch_week(league_id, week - 1, False),
    )
    return _merge_legs(current, previous, week)


async def _current_week() -> int | None:
    """Sleeper's current ``week`` (the leg new moves are filed under), or None."""
    try:
        state = await get_nfl_state()
        week = ((state or {}).get("nfl_state") or {}).get("week")
        return week if isinstance(week, int) else None
    except Exception as e:
        logger.debug(f"NFL state unavailable for the transaction leg: {e}")
        return None


def _merge_legs(current: dict, previous: dict, week: int) -> dict:
    """One answer for "this week's moves" from the current and previous leg.

    Sleeper files the waiver run under the leg it closes, not the one it opens:
    on Wednesday of week 5 (``nfl_state.week`` 5) that morning's claims are in
    leg 4, and leg 5 is still empty. Asking for week 5 alone read as "no claims
    processed" hours after they had been. Rows are deduped by transaction_id
    and keep Sleeper's own ``leg`` field, so callers can still tell them apart.
    """
    seen: set = set()
    merged: list = []
    for resp in (current, previous):
        for tx in (resp or {}).get("transactions") or []:
            tid = tx.get("transaction_id") if isinstance(tx, dict) else None
            if tid is not None and tid in seen:
                continue
            if tid is not None:
                seen.add(tid)
            merged.append(tx)
    merged.sort(key=lambda t: (t.get("status_updated") or t.get("created") or 0)
                if isinstance(t, dict) else 0, reverse=True)
    out = dict(current or {})
    out.update({
        "transactions": merged,
        "count": len(merged),
        "week": week,
        "legs": [week, week - 1],
        "previous_leg_count": len((previous or {}).get("transactions") or []),
        "leg_note": (f"Includes leg {week - 1}: Sleeper files the waiver run that opens "
                     f"week {week} under the previous leg."),
    })
    if previous and previous.get("success") is False:
        out["previous_leg_error"] = previous.get("error") or "unavailable"
    # One leg answering is an answer; the other's failure is reported above.
    if out.get("success") is False and previous and previous.get("success") is not False:
        out["success"] = True
        out["current_leg_error"] = out.pop("error", None) or "unavailable"
    return out


async def _fetch_week(league_id: str, week: int, auto_inferred: bool) -> dict:
    """One leg of transactions: retries, enrichment, snapshot fallback."""
    headers = get_http_headers("sleeper_transactions")
    url = f"https://api.sleeper.app/v1/league/{league_id}/transactions/{week}"
    retry_delays = [0.0, 0.4, 1.0]
    attempts = 0
    last_error = None
    from .database import get_shared_db
    nfl_db = get_shared_db()

    for delay in retry_delays:
        if delay:
            import asyncio as _asyncio
            await _asyncio.sleep(delay)
        attempts += 1
        try:
            async with create_http_client() as client:
                response, _ = await sleeper_get_fresh(client, url, headers)
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
                        # Always enrich with injury and practice status
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
                            logger.debug(f"Transaction player enrichment failed for {pid}: {e}")
                        cache[pid] = obj; return obj
                    if isinstance(tx_data, list):
                        for tx in tx_data:
                            if not isinstance(tx, dict):
                                continue
                            adds = tx.get("adds") or {}
                            drops = tx.get("drops") or {}
                            if isinstance(adds, dict):
                                tx["adds_enriched"] = [enrich_player(pid) for pid in adds]
                            if isinstance(drops, dict):
                                tx["drops_enriched"] = [enrich_player(pid) for pid in drops]
                except Exception as enrich_error:
                    logger.debug(f"Transaction enrichment skipped: {enrich_error}")

                # Save snapshot
                nfl_db.save_transaction_snapshot(league_id, week, tx_data)
                # A trade/add/drop newer than the shared roster copy: the
                # rosters changed, so the next roster read refetches.
                note_transactions(league_id, tx_data)
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

