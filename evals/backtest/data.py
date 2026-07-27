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

import httpx

logger = logging.getLogger(__name__)

NFLVERSE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/player_stats_{season}.csv"
)
# Per-game schedule with recorded wind / roof / temp (nflverse `nfldata`).
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
# nflverse abbreviations -> the abbreviations used across this codebase.
_TEAM_FIX = {"LA": "LAR", "WAS": "WSH", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_season(season: int, use_cache: bool = True) -> list[dict]:
    """Return regular-season weekly records for a season.

    Each record: player_id, player, position, team, opponent, season, week,
    ppr (fantasy_points_ppr), touches (targets + carries).
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(_CACHE_DIR, f"player_stats_{season}.csv")

    text: str | None = None
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

    records: list[dict] = []
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
        targets = _to_float(row.get("targets"))
        carries = _to_float(row.get("carries") or row.get("rushing_attempts"))
        records.append({
            "player_id": pid,
            "player": row.get("player_display_name") or row.get("player_name"),
            "position": pos,
            "team": _TEAM_FIX.get(team, team),
            "opponent": _TEAM_FIX.get(opp, opp),
            "season": int(season),
            "week": int(wk),
            "ppr": _to_float(row.get("fantasy_points_ppr")),
            "touches": targets + carries,
            # Opportunity components (for the opportunity-based projection).
            "targets": targets,
            "carries": carries,
            "attempts": _to_float(row.get("attempts")),
            "receptions": _to_float(row.get("receptions")),
            "receiving_yards": _to_float(row.get("receiving_yards")),
            "receiving_tds": _to_float(row.get("receiving_tds")),
            "rushing_yards": _to_float(row.get("rushing_yards")),
            "rushing_tds": _to_float(row.get("rushing_tds")),
            "passing_yards": _to_float(row.get("passing_yards")),
            "passing_tds": _to_float(row.get("passing_tds")),
            "interceptions": _to_float(row.get("interceptions")),
        })
    logger.info("Loaded %d REG records for %s", len(records), season)
    return records


def load_games(season: int, use_cache: bool = True) -> dict:
    """Return per-game weather keyed by both teams.

    ``{(season, week, team): {"wind": float, "roof": str}}`` for regular-season
    games, from the nflverse schedule (recorded wind mph + roof).
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(_CACHE_DIR, "games.csv")

    text: str | None = None
    if use_cache and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            text = f.read()
    if text is None:
        logger.info("Downloading %s", GAMES_URL)
        resp = httpx.get(GAMES_URL, follow_redirects=True, timeout=60)
        resp.raise_for_status()
        text = resp.text
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(text)

    out: dict = {}
    for row in csv.DictReader(StringIO(text)):
        if str(row.get("season")) != str(season):
            continue
        if (row.get("game_type") or "").upper() != "REG":
            continue
        wk = row.get("week")
        if not wk:
            continue
        wind_raw = (row.get("wind") or "").strip()
        wind = _to_float(wind_raw) if wind_raw else None
        roof = (row.get("roof") or "").strip().lower()
        info = {"wind": wind, "roof": roof}
        for side in ("home_team", "away_team"):
            team = _TEAM_FIX.get((row.get(side) or "").upper(), (row.get(side) or "").upper())
            if team:
                out[(int(season), int(wk), team)] = info
    return out
