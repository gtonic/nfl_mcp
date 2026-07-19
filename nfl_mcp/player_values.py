"""
Consensus player market values (real valuation layer).

This module fetches market-consensus player values from FantasyCalc — a free,
key-less API that already attaches Sleeper player IDs to every player, so the
values join directly onto Sleeper roster and draft data. Values are
format-aware (PPR level, superflex, league size, redraft vs. dynasty).

It replaces the previous "everyone starts at 50" heuristic used by the trade
analyzer and provides the ranking/tier backbone for the draft assistant.

Design:
- In-memory TTL cache per league format (values move slowly).
- SQLite persistence via NFLDatabase (survives restarts, serves as stale
  fallback when the API is unreachable).
- Lookups by Sleeper player_id (primary) or normalized name+position (fallback).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, UTC, timedelta
from typing import Any, Dict, List, Optional

import httpx

from .config import create_http_client, get_rate_limiter
from .errors import (
    create_success_response,
    create_error_response,
    ErrorType,
    handle_http_errors,
)

logger = logging.getLogger(__name__)

FANTASYCALC_URL = "https://api.fantasycalc.com/values/current"

# Market values move slowly; a couple of refreshes per day is plenty.
CACHE_TTL_HOURS = 12
# How stale DB data may be before we consider it unusable for a "fresh" answer.
DB_FRESH_HOURS = 24

# Name suffixes to strip when matching by name.
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def scoring_to_ppr(scoring: Optional[str]) -> float:
    """Map a scoring label to a PPR value (points per reception)."""
    if scoring is None:
        return 1.0
    s = str(scoring).strip().lower()
    if s in ("ppr", "full", "full-ppr", "1", "1.0"):
        return 1.0
    if s in ("half", "half-ppr", "halfppr", "0.5", ".5"):
        return 0.5
    if s in ("standard", "std", "non-ppr", "0", "0.0"):
        return 0.0
    # Allow passing a raw number
    try:
        return float(s)
    except ValueError:
        return 1.0


def build_format_key(ppr: float, num_qbs: int, num_teams: int, is_dynasty: bool) -> str:
    """Build a stable cache/DB key encoding the league format."""
    return f"ppr{ppr:g}:qb{int(num_qbs)}:tm{int(num_teams)}:{'dyn' if is_dynasty else 'redraft'}"


def normalize_name(name: Optional[str]) -> str:
    """Normalize a player name for fuzzy matching (lowercase, no punctuation/suffix)."""
    if not name:
        return ""
    cleaned = re.sub(r"[^a-z\s]", "", name.lower())
    parts = [p for p in cleaned.split() if p and p not in _NAME_SUFFIXES]
    return " ".join(parts)


def _normalize_fantasycalc_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a FantasyCalc value object into our normalized value dict."""
    player = entry.get("player") or {}
    sleeper_id = player.get("sleeperId")
    if not sleeper_id:
        # Without a Sleeper id we cannot join onto roster/draft data reliably.
        return None
    return {
        "player_id": str(sleeper_id),
        "name": player.get("name"),
        "position": player.get("position"),
        "team": player.get("maybeTeam"),
        "value": entry.get("value"),
        "redraft_value": entry.get("redraftValue", entry.get("value")),
        "overall_rank": entry.get("overallRank"),
        "position_rank": entry.get("positionRank"),
        "tier": entry.get("maybeTier"),
        "trend_30day": entry.get("trend30Day"),
        "source": "fantasycalc",
    }


class PlayerValuesService:
    """Fetches, caches and serves consensus player values."""

    def __init__(self, db=None):
        self.db = db
        if self.db is None:
            try:
                from .database import NFLDatabase
                self.db = NFLDatabase()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"PlayerValuesService DB init failed: {e}")
                self.db = None
        # format_key -> {"list": [...], "by_id": {...}, "by_name": {...}, "fetched_at": dt}
        self._mem: Dict[str, Dict[str, Any]] = {}

    # -- fetching -----------------------------------------------------------
    async def _fetch_from_fantasycalc(
        self, ppr: float, num_qbs: int, num_teams: int, is_dynasty: bool
    ) -> List[Dict[str, Any]]:
        params = {
            "isDynasty": "true" if is_dynasty else "false",
            "numQbs": int(num_qbs),
            "numTeams": int(num_teams),
            "ppr": ppr,
        }
        try:
            await get_rate_limiter("fantasycalc").acquire()
        except Exception:
            pass
        async with create_http_client() as client:
            resp = await client.get(FANTASYCALC_URL, params=params)
            resp.raise_for_status()
            raw = resp.json()
        values: List[Dict[str, Any]] = []
        for entry in raw or []:
            norm = _normalize_fantasycalc_entry(entry)
            if norm:
                values.append(norm)
        return values

    def _index(self, values: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_id: Dict[str, Dict] = {}
        by_name: Dict[str, Dict] = {}
        for v in values:
            if v.get("player_id"):
                by_id[str(v["player_id"])] = v
            nn = normalize_name(v.get("name"))
            if nn:
                # First (higher-ranked) entry wins on name collision.
                by_name.setdefault(nn, v)
        return {"list": values, "by_id": by_id, "by_name": by_name}

    async def get_values(
        self,
        ppr: float = 1.0,
        num_qbs: int = 1,
        num_teams: int = 12,
        is_dynasty: bool = False,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Return values for a format with caching and stale-fallback.

        Returns a dict: {format_key, list, by_id, by_name, source, stale,
        fetched_at, count}.
        """
        format_key = build_format_key(ppr, num_qbs, num_teams, is_dynasty)
        now = datetime.now(UTC)

        # 1) In-memory cache
        if not force_refresh:
            cached = self._mem.get(format_key)
            if cached and (now - cached["fetched_at"]) < timedelta(hours=CACHE_TTL_HOURS):
                return {**cached, "format_key": format_key, "source": "fantasycalc",
                        "stale": False, "count": len(cached["list"])}

        # 2) Fresh fetch from API
        try:
            values = await self._fetch_from_fantasycalc(ppr, num_qbs, num_teams, is_dynasty)
            if values:
                if self.db:
                    try:
                        self.db.upsert_player_values(values, format_key)
                    except Exception as e:  # pragma: no cover - defensive
                        logger.debug(f"upsert_player_values failed: {e}")
                idx = self._index(values)
                idx["fetched_at"] = now
                self._mem[format_key] = idx
                return {**idx, "format_key": format_key, "source": "fantasycalc",
                        "stale": False, "count": len(values)}
        except Exception as e:
            logger.warning(f"FantasyCalc fetch failed ({format_key}): {e}")

        # 3) Stale fallback from DB
        if self.db:
            try:
                rows = self.db.get_player_values(format_key)
                if rows:
                    idx = self._index(rows)
                    last = self.db.get_player_values_last_updated(format_key)
                    idx["fetched_at"] = now
                    self._mem[format_key] = idx
                    return {**idx, "format_key": format_key, "source": "db_cache",
                            "stale": True, "snapshot_updated_at": last,
                            "count": len(rows)}
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"DB fallback failed: {e}")

        return {"format_key": format_key, "list": [], "by_id": {}, "by_name": {},
                "source": "unavailable", "stale": True, "count": 0}

    def lookup(
        self,
        indexed: Dict[str, Any],
        player_id: Optional[str] = None,
        name: Optional[str] = None,
        position: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Look up a single player's value from an indexed values dict."""
        if player_id:
            hit = indexed.get("by_id", {}).get(str(player_id))
            if hit:
                return hit
        if name:
            hit = indexed.get("by_name", {}).get(normalize_name(name))
            if hit and (not position or (hit.get("position") or "").upper() == position.upper()):
                return hit
        return None


# Module-level singleton so all callers share the cache.
_service: Optional[PlayerValuesService] = None


def get_values_service(db=None) -> PlayerValuesService:
    """Get or create the shared PlayerValuesService."""
    global _service
    if _service is None:
        _service = PlayerValuesService(db=db)
    elif db is not None and _service.db is None:
        _service.db = db
    return _service


# ==========================================================================
# MCP Tool Functions
# ==========================================================================

@handle_http_errors(
    default_data={"values": [], "total": 0},
    operation_name="fetching player values",
)
async def get_player_values(
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    dynasty: bool = False,
    position: Optional[str] = None,
    limit: Optional[int] = 100,
    db=None,
) -> Dict[str, Any]:
    """Get consensus player market values (FantasyCalc), ordered best-first.

    Real market values you can trust for trades and draft ordering — not a
    heuristic. Values are format-aware.

    Args:
        scoring: "ppr", "half-ppr", or "standard".
        superflex: True for 2-QB / superflex leagues (boosts QB values).
        num_teams: League size (default 12).
        dynasty: True for dynasty values, False for redraft.
        position: Optional filter (QB, RB, WR, TE).
        limit: Max players to return (default 100).

    Returns: {values: [...], total, format, source, stale, updated_at}
    """
    ppr = scoring_to_ppr(scoring)
    num_qbs = 2 if superflex else 1
    service = get_values_service(db)
    data = await service.get_values(ppr, num_qbs, num_teams, dynasty)

    values = data.get("list", [])
    if position:
        pos = position.upper()
        values = [v for v in values if (v.get("position") or "").upper() == pos]
    if limit:
        values = values[: int(limit)]

    return create_success_response({
        "values": values,
        "total": len(values),
        "format": {
            "scoring": scoring, "ppr": ppr, "superflex": superflex,
            "num_teams": num_teams, "dynasty": dynasty,
        },
        "source": data.get("source"),
        "stale": data.get("stale", False),
        "updated_at": data.get("snapshot_updated_at"),
        "message": (
            f"{len(values)} player values ({data.get('source')})"
            + (" ⚠️ STALE cached data" if data.get("stale") else "")
        ),
    })


@handle_http_errors(
    default_data={"value": None},
    operation_name="looking up player value",
)
async def get_player_value(
    player_id: Optional[str] = None,
    name: Optional[str] = None,
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    dynasty: bool = False,
    db=None,
) -> Dict[str, Any]:
    """Get the consensus market value for a single player by Sleeper id or name.

    Args:
        player_id: Sleeper player id (preferred).
        name: Player name (fallback lookup).
        scoring / superflex / num_teams / dynasty: League format.

    Returns: {value: {...} | None, found, source, stale}
    """
    if not player_id and not name:
        return create_error_response(
            "Provide player_id or name",
            ErrorType.VALIDATION,
            {"value": None, "found": False},
        )
    ppr = scoring_to_ppr(scoring)
    num_qbs = 2 if superflex else 1
    service = get_values_service(db)
    data = await service.get_values(ppr, num_qbs, num_teams, dynasty)
    hit = service.lookup(data, player_id=player_id, name=name)

    return create_success_response({
        "value": hit,
        "found": hit is not None,
        "source": data.get("source"),
        "stale": data.get("stale", False),
        "message": (
            f"{hit['name']}: value {hit['value']} (#{hit['overall_rank']} overall)"
            if hit else "Player not found in value list (deep bench / K / DST?)"
        ),
    })
