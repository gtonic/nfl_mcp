"""Load real historical weekly player data from nflverse (the ground truth).

nflverse publishes free per-season CSVs of weekly player stats, including each
player's fantasy points and the defense they faced. We cache the download so the
backtest is fast and offline after the first run.

This is the *same source* the live server uses for defense-vs-position rankings,
so the backtest and production agree on reality.
"""

from __future__ import annotations

import csv
import logging
import os
from io import StringIO
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

NFLVERSE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/player_stats_{season}.csv"
)
_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
# nflverse abbreviations -> the abbreviations used across this codebase.
_TEAM_FIX = {"LA": "LAR", "WAS": "WSH", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_season(season: int, use_cache: bool = True) -> List[Dict]:
    """Return regular-season weekly records for a season.

    Each record: player_id, player, position, team, opponent, season, week,
    ppr (fantasy_points_ppr), touches (targets + carries).
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(_CACHE_DIR, f"player_stats_{season}.csv")

    text: Optional[str] = None
    if use_cache and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            text = f.read()
    if text is None:
        url = NFLVERSE_URL.format(season=season)
        logger.info("Downloading %s", url)
        resp = httpx.get(url, follow_redirects=True, timeout=60)
        resp.raise_for_status()
        text = resp.text
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(text)

    records: List[Dict] = []
    for row in csv.DictReader(StringIO(text)):
        if (row.get("season_type") or "").upper() != "REG":
            continue
        pos = (row.get("position") or row.get("position_group") or "").upper()
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        opp = (row.get("opponent_team") or "").upper()
        team = (row.get("recent_team") or row.get("team") or "").upper()
        wk = row.get("week")
        pid = row.get("player_id")
        if not (opp and wk and pid):
            continue
        touches = (
            _to_float(row.get("targets"))
            + _to_float(row.get("carries") or row.get("rushing_attempts"))
        )
        records.append({
            "player_id": pid,
            "player": row.get("player_display_name") or row.get("player_name"),
            "position": pos,
            "team": _TEAM_FIX.get(team, team),
            "opponent": _TEAM_FIX.get(opp, opp),
            "season": int(season),
            "week": int(wk),
            "ppr": _to_float(row.get("fantasy_points_ppr")),
            "touches": touches,
        })
    logger.info("Loaded %d REG records for %s", len(records), season)
    return records
