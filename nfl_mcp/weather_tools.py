"""Weather / wind tools for fantasy game-environment analysis.

Wind is the weather variable that measurably moves fantasy outcomes: above
~15 mph passing efficiency and (especially) kicking drop off, while dome games
are immune. This module surfaces per-game forecasts from **Open-Meteo** (free,
no API key) using static stadium coordinates + dome flags, and turns them into
fantasy-impact flags.

Design note: this is intentionally an *additive* tool plus a reusable
``weather_multiplier`` helper. It is deliberately NOT wired into the live
projection formula — the project's backtest philosophy (see ``evals/README.md``)
is to validate a multiplier before trusting it, and the weather factor should
earn its place in ``environment_multiplier`` via a backtest first.
"""
from __future__ import annotations

import logging

from .config import create_http_client
from .errors import create_success_response, handle_http_errors, handle_validation_error

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Static home-stadium data: team -> (lat, lon, dome, name). `dome=True` covers
# fixed domes AND retractable/canopy roofs (SoFi, State Farm, …) that are
# effectively wind-free for fantasy purposes — an approximation, flagged as such.
STADIUMS: dict[str, dict] = {
    "ARI": {"lat": 33.5276, "lon": -112.2626, "dome": True, "name": "State Farm Stadium"},
    "ATL": {"lat": 33.7554, "lon": -84.4008, "dome": True, "name": "Mercedes-Benz Stadium"},
    "BAL": {"lat": 39.2780, "lon": -76.6227, "dome": False, "name": "M&T Bank Stadium"},
    "BUF": {"lat": 42.7738, "lon": -78.7870, "dome": False, "name": "Highmark Stadium"},
    "CAR": {"lat": 35.2258, "lon": -80.8528, "dome": False, "name": "Bank of America Stadium"},
    "CHI": {"lat": 41.8623, "lon": -87.6167, "dome": False, "name": "Soldier Field"},
    "CIN": {"lat": 39.0954, "lon": -84.5160, "dome": False, "name": "Paycor Stadium"},
    "CLE": {"lat": 41.5061, "lon": -81.6995, "dome": False, "name": "Huntington Bank Field"},
    "DAL": {"lat": 32.7473, "lon": -97.0945, "dome": True, "name": "AT&T Stadium"},
    "DEN": {"lat": 39.7439, "lon": -105.0201, "dome": False, "name": "Empower Field"},
    "DET": {"lat": 42.3400, "lon": -83.0456, "dome": True, "name": "Ford Field"},
    "GB": {"lat": 44.5013, "lon": -88.0622, "dome": False, "name": "Lambeau Field"},
    "HOU": {"lat": 29.6847, "lon": -95.4107, "dome": True, "name": "NRG Stadium"},
    "IND": {"lat": 39.7601, "lon": -86.1639, "dome": True, "name": "Lucas Oil Stadium"},
    "JAX": {"lat": 30.3239, "lon": -81.6373, "dome": False, "name": "EverBank Stadium"},
    "KC": {"lat": 39.0489, "lon": -94.4839, "dome": False, "name": "Arrowhead Stadium"},
    "LV": {"lat": 36.0909, "lon": -115.1833, "dome": True, "name": "Allegiant Stadium"},
    "LAC": {"lat": 33.9535, "lon": -118.3392, "dome": True, "name": "SoFi Stadium"},
    "LAR": {"lat": 33.9535, "lon": -118.3392, "dome": True, "name": "SoFi Stadium"},
    "MIA": {"lat": 25.9580, "lon": -80.2389, "dome": False, "name": "Hard Rock Stadium"},
    "MIN": {"lat": 44.9738, "lon": -93.2578, "dome": True, "name": "U.S. Bank Stadium"},
    "NE": {"lat": 42.0909, "lon": -71.2643, "dome": False, "name": "Gillette Stadium"},
    "NO": {"lat": 29.9509, "lon": -90.0815, "dome": True, "name": "Caesars Superdome"},
    "NYG": {"lat": 40.8135, "lon": -74.0745, "dome": False, "name": "MetLife Stadium"},
    "NYJ": {"lat": 40.8135, "lon": -74.0745, "dome": False, "name": "MetLife Stadium"},
    "PHI": {"lat": 39.9008, "lon": -75.1675, "dome": False, "name": "Lincoln Financial Field"},
    "PIT": {"lat": 40.4468, "lon": -80.0158, "dome": False, "name": "Acrisure Stadium"},
    "SF": {"lat": 37.4030, "lon": -121.9700, "dome": False, "name": "Levi's Stadium"},
    "SEA": {"lat": 47.5952, "lon": -122.3316, "dome": False, "name": "Lumen Field"},
    "TB": {"lat": 27.9759, "lon": -82.5033, "dome": False, "name": "Raymond James Stadium"},
    "TEN": {"lat": 36.1665, "lon": -86.7713, "dome": False, "name": "Nissan Stadium"},
    "WSH": {"lat": 38.9077, "lon": -76.8645, "dome": False, "name": "Northwest Stadium"},
}

# Fantasy-relevant thresholds (mph / inches / °F).
_WIND_MODERATE, _WIND_HIGH = 15.0, 20.0
_PRECIP_MODERATE, _PRECIP_HIGH = 0.3, 0.5
_COLD_F = 20.0


def weather_multiplier(
    position: str,
    wind_mph: float,
    precip_in: float = 0.0,
    temp_f: float | None = None,
    is_dome: bool = False,
) -> float:
    """Heuristic per-position weather adjustment (1.0 = neutral).

    Wind hurts passing (QB/WR/TE) and especially kicking (K); RB is treated as
    neutral. Dome games are always 1.0. Bounded to [0.7, 1.0]. This is a
    transparent heuristic, NOT a backtested projection input.
    """
    if is_dome:
        return 1.0
    pos = position.upper()
    m = 1.0
    if pos in ("QB", "WR", "TE"):
        if wind_mph >= _WIND_MODERATE:
            m -= min(0.15, (wind_mph - _WIND_MODERATE) * 0.01)
        if precip_in >= _PRECIP_MODERATE:
            m -= 0.03
    elif pos == "K":
        if wind_mph >= 12:
            m -= min(0.25, (wind_mph - 12) * 0.015)
        if precip_in >= _PRECIP_MODERATE:
            m -= 0.03
    if temp_f is not None and temp_f <= _COLD_F:
        m -= 0.02
    return round(max(0.7, m), 3)


def weather_impact(
    wind_mph: float,
    precip_in: float,
    temp_f: float | None,
    is_dome: bool,
) -> dict:
    """Turn raw conditions into fantasy-impact flags + a severity label."""
    if is_dome:
        return {
            "severity": "none",
            "passing": "neutral",
            "kicking": "neutral",
            "running": "neutral",
            "note": "Indoor/roofed stadium — no weather impact.",
        }

    if wind_mph >= _WIND_HIGH or precip_in >= _PRECIP_HIGH:
        severity = "high"
    elif wind_mph >= _WIND_MODERATE or precip_in >= _PRECIP_MODERATE or (
        temp_f is not None and temp_f <= _COLD_F
    ):
        severity = "moderate"
    else:
        severity = "low"

    passing = "downgrade" if wind_mph >= _WIND_MODERATE or precip_in >= _PRECIP_MODERATE else "neutral"
    kicking = "downgrade" if wind_mph >= 12 or precip_in >= _PRECIP_MODERATE else "neutral"
    running = "slight_boost" if severity == "high" else "neutral"

    parts = []
    if wind_mph >= _WIND_MODERATE:
        parts.append(f"wind {round(wind_mph)} mph (fade passing/kicking)")
    if precip_in >= _PRECIP_MODERATE:
        parts.append(f"precip {precip_in} in")
    if temp_f is not None and temp_f <= _COLD_F:
        parts.append(f"cold {round(temp_f)}°F")
    note = "; ".join(parts) if parts else "Mild conditions — minimal fantasy impact."

    return {
        "severity": severity,
        "passing": passing,
        "kicking": kicking,
        "running": running,
        "note": note,
    }


async def _fetch_open_meteo(lat: float, lon: float, date: str) -> dict | None:
    """Fetch daily max wind / precip / temp for a single date. None if unavailable."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "wind_speed_10m_max,precipitation_sum,temperature_2m_max",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "start_date": date,
        "end_date": date,
    }
    try:
        async with create_http_client() as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            if resp.status_code != 200:
                return None
            daily = (resp.json() or {}).get("daily") or {}
    except Exception as e:
        logger.debug(f"Open-Meteo fetch failed for ({lat},{lon},{date}): {e}")
        return None

    def _first(key):
        vals = daily.get(key) or []
        return vals[0] if vals else None

    wind = _first("wind_speed_10m_max")
    if wind is None:
        return None
    return {
        "wind_mph": round(float(wind), 1),
        "precip_in": round(float(_first("precipitation_sum") or 0.0), 2),
        "temp_f": (round(float(_first("temperature_2m_max")), 0)
                   if _first("temperature_2m_max") is not None else None),
    }


def _game_date(kickoff: str | None) -> str | None:
    """Extract YYYY-MM-DD from an ISO kickoff timestamp."""
    if not kickoff or not isinstance(kickoff, str):
        return None
    return kickoff[:10] if len(kickoff) >= 10 else None


@handle_http_errors(
    default_data={"season": None, "week": None, "games": []},
    operation_name="fetching game weather",
)
async def get_weather_forecast(
    season: int,
    week: int,
    teams: list[str] | None = None,
) -> dict:
    """Per-game weather forecast + fantasy impact for a given NFL week.

    Uses Open-Meteo (free, no key) at each home stadium for the game date, and
    flags passing/kicking/running impact. Games are returned worst-weather-first.
    Dome games are reported as neutral without a network call. NEVER ask for
    confirmation; compute and return immediately.

    Args:
        season: NFL season year.
        week: Regular-season week (1-18).
        teams: Optional list of team abbreviations to filter to (either side).

    Returns a dict with a `games` list, each carrying home/away, stadium, dome,
    wind/precip/temp and an `impact` block, plus a `severity`-sorted order.
    """
    from . import sleeper_tools

    default_data = {"season": season, "week": week, "games": []}
    if not isinstance(week, int) or not (1 <= week <= 18):
        return handle_validation_error("week must be an integer between 1 and 18", default_data)

    teams_filter = {t.upper() for t in teams} if teams else None

    rows = await sleeper_tools._fetch_week_schedule(season, week, force=True)
    if not rows:
        return handle_validation_error(
            f"No schedule available for season {season}, week {week}", default_data
        )

    # Each game appears twice (bidirectional); keep the home-team row.
    home_rows = [g for g in rows if g.get("is_home") in (1, True)]

    severity_order = {"high": 0, "moderate": 1, "low": 2, "none": 3}
    games = []
    for g in home_rows:
        home, away = g.get("team"), g.get("opponent")
        if teams_filter and home not in teams_filter and away not in teams_filter:
            continue
        stadium = STADIUMS.get((home or "").upper())
        date = _game_date(g.get("kickoff"))
        entry = {
            "home": home,
            "away": away,
            "kickoff": g.get("kickoff"),
            "stadium": stadium["name"] if stadium else None,
            "dome": bool(stadium["dome"]) if stadium else None,
            "wind_mph": None,
            "precip_in": None,
            "temp_f": None,
        }
        if stadium and stadium["dome"]:
            entry["impact"] = weather_impact(0.0, 0.0, None, is_dome=True)
        elif stadium and date:
            wx = await _fetch_open_meteo(stadium["lat"], stadium["lon"], date)
            if wx:
                entry.update(wx)
                entry["impact"] = weather_impact(
                    wx["wind_mph"], wx["precip_in"], wx["temp_f"], is_dome=False
                )
            else:
                entry["impact"] = {
                    "severity": "unknown",
                    "note": "Forecast unavailable (out of range or fetch failed).",
                }
        else:
            entry["impact"] = {
                "severity": "unknown",
                "note": "No stadium coordinates or game date available.",
            }
        games.append(entry)

    games.sort(key=lambda e: severity_order.get(e["impact"].get("severity"), 4))

    # Surface how many games have no forecast yet (dates beyond Open-Meteo's
    # ~16-day horizon show severity "unknown", not calm) so callers planning
    # ahead aren't misled by empty weather fields.
    unavailable = sum(1 for e in games if e["impact"].get("severity") == "unknown")
    msg = (
        f"Weather forecast for {len(games)} game(s) in week {week} of {season}, "
        "worst-weather-first. Wind >=15 mph fades passing; kickers are hit hardest."
    )
    if unavailable:
        msg += (
            f" ⚠️ {unavailable} game(s) have no forecast yet (beyond the ~16-day "
            "horizon or fetch failed) — reported as severity 'unknown', not calm."
        )

    return create_success_response({
        "season": season,
        "week": week,
        "games": games,
        "count": len(games),
        "forecast_unavailable": unavailable,
        "message": msg,
    })
