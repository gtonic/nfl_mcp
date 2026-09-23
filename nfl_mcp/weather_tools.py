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

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import create_http_client
from .errors import create_success_response, handle_http_errors, handle_validation_error
from .teams import normalize_team

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Static home-stadium data: team -> (lat, lon, dome, name, tz). `dome=True` covers
# fixed domes AND retractable/canopy roofs (SoFi, State Farm, …) that are
# effectively wind-free for fantasy purposes — an approximation, flagged as such.
STADIUMS: dict[str, dict] = {
    "ARI": {"lat": 33.5276, "lon": -112.2626, "dome": True, "name": "State Farm Stadium",
            "tz": "America/Phoenix"},
    "ATL": {"lat": 33.7554, "lon": -84.4008, "dome": True, "name": "Mercedes-Benz Stadium",
            "tz": "America/New_York"},
    "BAL": {"lat": 39.2780, "lon": -76.6227, "dome": False, "name": "M&T Bank Stadium",
            "tz": "America/New_York"},
    "BUF": {"lat": 42.7738, "lon": -78.7870, "dome": False, "name": "Highmark Stadium",
            "tz": "America/New_York"},
    "CAR": {"lat": 35.2258, "lon": -80.8528, "dome": False, "name": "Bank of America Stadium",
            "tz": "America/New_York"},
    "CHI": {"lat": 41.8623, "lon": -87.6167, "dome": False, "name": "Soldier Field",
            "tz": "America/Chicago"},
    "CIN": {"lat": 39.0954, "lon": -84.5160, "dome": False, "name": "Paycor Stadium",
            "tz": "America/New_York"},
    "CLE": {"lat": 41.5061, "lon": -81.6995, "dome": False, "name": "Huntington Bank Field",
            "tz": "America/New_York"},
    "DAL": {"lat": 32.7473, "lon": -97.0945, "dome": True, "name": "AT&T Stadium",
            "tz": "America/Chicago"},
    "DEN": {"lat": 39.7439, "lon": -105.0201, "dome": False, "name": "Empower Field",
            "tz": "America/Denver"},
    "DET": {"lat": 42.3400, "lon": -83.0456, "dome": True, "name": "Ford Field",
            "tz": "America/Detroit"},
    "GB": {"lat": 44.5013, "lon": -88.0622, "dome": False, "name": "Lambeau Field",
            "tz": "America/Chicago"},
    "HOU": {"lat": 29.6847, "lon": -95.4107, "dome": True, "name": "NRG Stadium",
            "tz": "America/Chicago"},
    "IND": {"lat": 39.7601, "lon": -86.1639, "dome": True, "name": "Lucas Oil Stadium",
            "tz": "America/Indiana/Indianapolis"},
    "JAX": {"lat": 30.3239, "lon": -81.6373, "dome": False, "name": "EverBank Stadium",
            "tz": "America/New_York"},
    "KC": {"lat": 39.0489, "lon": -94.4839, "dome": False, "name": "Arrowhead Stadium",
            "tz": "America/Chicago"},
    "LV": {"lat": 36.0909, "lon": -115.1833, "dome": True, "name": "Allegiant Stadium",
            "tz": "America/Los_Angeles"},
    "LAC": {"lat": 33.9535, "lon": -118.3392, "dome": True, "name": "SoFi Stadium",
            "tz": "America/Los_Angeles"},
    "LAR": {"lat": 33.9535, "lon": -118.3392, "dome": True, "name": "SoFi Stadium",
            "tz": "America/Los_Angeles"},
    "MIA": {"lat": 25.9580, "lon": -80.2389, "dome": False, "name": "Hard Rock Stadium",
            "tz": "America/New_York"},
    "MIN": {"lat": 44.9738, "lon": -93.2578, "dome": True, "name": "U.S. Bank Stadium",
            "tz": "America/Chicago"},
    "NE": {"lat": 42.0909, "lon": -71.2643, "dome": False, "name": "Gillette Stadium",
            "tz": "America/New_York"},
    "NO": {"lat": 29.9509, "lon": -90.0815, "dome": True, "name": "Caesars Superdome",
            "tz": "America/Chicago"},
    "NYG": {"lat": 40.8135, "lon": -74.0745, "dome": False, "name": "MetLife Stadium",
            "tz": "America/New_York"},
    "NYJ": {"lat": 40.8135, "lon": -74.0745, "dome": False, "name": "MetLife Stadium",
            "tz": "America/New_York"},
    "PHI": {"lat": 39.9008, "lon": -75.1675, "dome": False, "name": "Lincoln Financial Field",
            "tz": "America/New_York"},
    "PIT": {"lat": 40.4468, "lon": -80.0158, "dome": False, "name": "Acrisure Stadium",
            "tz": "America/New_York"},
    "SF": {"lat": 37.4030, "lon": -121.9700, "dome": False, "name": "Levi's Stadium",
            "tz": "America/Los_Angeles"},
    "SEA": {"lat": 47.5952, "lon": -122.3316, "dome": False, "name": "Lumen Field",
            "tz": "America/Los_Angeles"},
    "TB": {"lat": 27.9759, "lon": -82.5033, "dome": False, "name": "Raymond James Stadium",
            "tz": "America/New_York"},
    "TEN": {"lat": 36.1665, "lon": -86.7713, "dome": False, "name": "Nissan Stadium",
            "tz": "America/Chicago"},
    "WSH": {"lat": 38.9077, "lon": -76.8645, "dome": False, "name": "Northwest Stadium",
            "tz": "America/New_York"},
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


# Neutral-site venues the league plays at (international series and the
# like), keyed by ESPN venue id with the name as a fallback key. ESPN's event
# carries the venue's name, country and an `indoor` flag but no coordinates,
# and forecasting a London game at the home team's US stadium is the bug this
# table exists to prevent. A neutral site missing here is reported as
# `venue_uncertain` rather than guessed.
VENUES: dict[str, dict] = {
    "9119": {"lat": -37.8200, "lon": 144.9834, "dome": False, "name": "Melbourne Cricket Ground",
             "tz": "Australia/Melbourne"},
    "11931": {"lat": -22.9121, "lon": -43.2302, "dome": False, "name": "Maracanã Stadium",
              "tz": "America/Sao_Paulo"},
    "5534": {"lat": 51.6043, "lon": -0.0664, "dome": False, "name": "Tottenham Hotspur Stadium",
             "tz": "Europe/London"},
    "2455": {"lat": 51.5560, "lon": -0.2795, "dome": False, "name": "Wembley Stadium",
             "tz": "Europe/London"},
    "1781": {"lat": 48.9245, "lon": 2.3602, "dome": False, "name": "Stade de France",
             "tz": "Europe/Paris"},
    "1353": {"lat": 40.4531, "lon": -3.6883, "dome": True, "name": "Santiago Bernabéu",
             "tz": "Europe/Madrid"},
    "11930": {"lat": 48.2188, "lon": 11.6247, "dome": False, "name": "FC Bayern Munich Stadium",
              "tz": "Europe/Berlin"},
    "8219": {"lat": 19.3029, "lon": -99.1505, "dome": False, "name": "Estadio Banorte",
             "tz": "America/Mexico_City"},
    "arena corinthians": {"lat": -23.5453, "lon": -46.4742, "dome": False,
                          "name": "Arena Corinthians", "tz": "America/Sao_Paulo"},
    "croke park": {"lat": 53.3607, "lon": -6.2512, "dome": False, "name": "Croke Park",
                   "tz": "Europe/Dublin"},
    "deutsche bank park": {"lat": 50.0686, "lon": 8.6455, "dome": False,
                           "name": "Deutsche Bank Park", "tz": "Europe/Berlin"},
    "olympiastadion": {"lat": 52.5147, "lon": 13.2395, "dome": False,
                       "name": "Olympiastadion Berlin", "tz": "Europe/Berlin"},
}
_VENUES_BY_NAME = {v["name"].lower(): v for v in VENUES.values()}
_VENUES_BY_NAME.update({k: v for k, v in VENUES.items() if not k.isdigit()})
# ESPN's older names for venues it has since renamed.
_VENUES_BY_NAME["estadio azteca"] = VENUES["8219"]
_VENUES_BY_NAME["allianz arena"] = VENUES["11930"]
_VENUES_BY_NAME["neo química arena"] = VENUES["arena corinthians"]

# Hours either side of kickoff whose forecast describes the game.
_WINDOW_HOURS = 2


def _event_competition(row: dict) -> dict:
    """The ESPN competition stored with a schedule row, or ``{}``."""
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    comps = raw.get("competitions") or []
    return comps[0] if comps and isinstance(comps[0], dict) else {}


def resolve_venue(row: dict) -> dict | None:
    """Where a game is actually played: ``{lat, lon, dome, name, tz, source,
    venue_uncertain}``, or None when even the home team is unknown.

    A regular game is at the home team's stadium. A neutral-site game (the
    event says so) is at the venue ESPN names, looked up in :data:`VENUES`; one
    that is not in the table comes back with ``venue_uncertain`` and no
    coordinates — a forecast for the wrong continent is worse than none.
    Without a stored event there is nothing to say it is neutral, so the home
    stadium is assumed and labelled as such.
    """
    home = STADIUMS.get(normalize_team(row.get("team")) or "")
    comp = _event_competition(row)
    if comp.get("neutralSite"):
        venue = comp.get("venue") or {}
        known = VENUES.get(str(venue.get("id") or "")) or _VENUES_BY_NAME.get(
            (venue.get("fullName") or "").strip().lower()
        )
        if known:
            dome = venue.get("indoor") if isinstance(venue.get("indoor"), bool) else known["dome"]
            return {**known, "dome": bool(dome) or known["dome"], "source": "neutral_site",
                    "venue_uncertain": False}
        address = venue.get("address") or {}
        where = ", ".join(p for p in (address.get("city"), address.get("country")) if p)
        return {
            "lat": None, "lon": None, "dome": venue.get("indoor") is True,
            "name": venue.get("fullName") or where or None, "tz": None,
            "source": "neutral_site", "venue_uncertain": True,
        }
    if not home:
        return None
    return {**home, "source": "home_stadium" if comp else "home_team_assumed",
            "venue_uncertain": False}


def _local_kickoff(kickoff: str | None, tz: str | None) -> datetime | None:
    """Kickoff as a naive local wall-clock time at the venue, or None."""
    if not kickoff or not isinstance(kickoff, str):
        return None
    try:
        when = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if tz:
        try:
            when = when.astimezone(ZoneInfo(tz))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return when.replace(tzinfo=None)


def _game_date(kickoff: str | None, tz: str | None = None) -> str | None:
    """YYYY-MM-DD of the kickoff — at the venue when ``tz`` is given.

    A Sunday-night game in Denver kicks off at 00:20Z on Monday; its UTC date
    asked Open-Meteo for Monday's weather.
    """
    local = _local_kickoff(kickoff, tz)
    return local.date().isoformat() if local else None


_HOURLY = "wind_speed_10m,precipitation,temperature_2m"


def _params(lat, lon, date: str) -> dict:
    return {
        "latitude": lat,
        "longitude": lon,
        "hourly": _HOURLY,
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "temperature_unit": "fahrenheit",
        # Local time at each location: the hours line up with the venue's
        # own clock, which is how the kickoff window is chosen.
        "timezone": "auto",
        "start_date": date,
        "end_date": date,
    }


async def _fetch_open_meteo(lat: float, lon: float, date: str) -> dict | None:
    """One day's hourly forecast at one stadium (local time). None if unavailable."""
    try:
        async with create_http_client() as client:
            resp = await client.get(OPEN_METEO_URL, params=_params(lat, lon, date))
            if resp.status_code != 200:
                return None
            hourly = (resp.json() or {}).get("hourly") or {}
    except Exception as e:
        logger.debug(f"Open-Meteo fetch failed for ({lat},{lon},{date}): {e}")
        return None

    return _parse_hourly(hourly)


def _parse_hourly(hourly: dict) -> dict | None:
    """``{"hours": [(local_time, wind_mph, precip_in, temp_f), ...]}`` from an
    Open-Meteo ``hourly`` block, or None when it carries no wind at all."""
    times = hourly.get("time") or []
    wind = hourly.get("wind_speed_10m") or []
    precip = hourly.get("precipitation") or []
    temp = hourly.get("temperature_2m") or []
    hours = []
    for i, t in enumerate(times):
        w = wind[i] if i < len(wind) else None
        if w is None:
            continue
        p = precip[i] if i < len(precip) else None
        tf = temp[i] if i < len(temp) else None
        hours.append((t, float(w), float(p or 0.0), None if tf is None else float(tf)))
    return {"hours": hours} if hours else None


def kickoff_window(series: dict | None, kickoff_local: datetime | None) -> dict | None:
    """Game-time conditions from a day's hourly series: ``{wind_mph, precip_in,
    temp_f}`` over kickoff ±2h, or None if the series does not reach it.

    Wind is the window's peak, precipitation its total, temperature the hour
    nearest kickoff. A daily maximum described 3 a.m. gusts and a morning
    shower as if they fell on a 4:25 game.
    """
    if not series or kickoff_local is None:
        return None
    window = []
    for t, wind, precip, temp in series.get("hours") or []:
        try:
            at = datetime.fromisoformat(t)
        except (TypeError, ValueError):
            continue
        gap = abs((at - kickoff_local).total_seconds()) / 3600
        if gap <= _WINDOW_HOURS:
            window.append((gap, wind, precip, temp))
    if not window:
        return None
    temps = [(gap, temp) for gap, _, _, temp in window if temp is not None]
    return {
        "wind_mph": round(max(w for _, w, _, _ in window), 1),
        "precip_in": round(sum(p for _, _, p, _ in window), 2),
        "temp_f": round(min(temps)[1], 0) if temps else None,
    }


# Hourly forecasts per (lat, lon, local date). The daily routine asks for the
# same week several times in a row (briefing, league changes, ...); Open-Meteo
# refreshes its models hourly at best, so re-asking within half an hour buys
# nothing but latency. Only successful forecasts are kept — a miss is retried
# next call.
_FORECAST_TTL_SECONDS = 1800.0
_forecast_cache: dict[tuple[float, float, str], tuple[float, dict]] = {}


def clear_forecast_cache() -> None:
    """Drop every cached forecast (tests, or to force a refetch)."""
    _forecast_cache.clear()


async def _fetch_open_meteo_batch(
    points: list[tuple[float, float]], date: str,
) -> dict[tuple[float, float], dict | None]:
    """Hourly forecasts for several stadiums on one local date, in a single request.

    Open-Meteo takes comma-separated coordinate lists and answers with one
    result per location, in order — the same numbers as one request per
    stadium, without a dozen sequential round trips (each of which could stall
    for the full client timeout). Empty on any failure; the caller falls back
    to per-stadium requests.
    """
    if not points:
        return {}
    params = _params(
        ",".join(str(lat) for lat, _ in points),
        ",".join(str(lon) for _, lon in points),
        date,
    )
    try:
        async with create_http_client() as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            if resp.status_code != 200:
                return {}
            body = resp.json()
    except Exception as e:
        logger.debug(f"Open-Meteo batch fetch failed for {date}: {e}")
        return {}
    results = body if isinstance(body, list) else [body]
    if len(results) != len(points):
        return {}
    return {
        point: _parse_hourly((res or {}).get("hourly") or {})
        for point, res in zip(points, results, strict=True)
    }


async def _forecasts_for(
    keys: list[tuple[float, float, str]],
) -> dict[tuple[float, float, str], dict | None]:
    """Forecast per ``(lat, lon, local date)``: cache, then one request per date,
    then per stadium."""
    now = time.monotonic()
    out: dict[tuple[float, float, str], dict | None] = {}
    missing: list[tuple[float, float, str]] = []
    for key in dict.fromkeys(keys):
        hit = _forecast_cache.get(key)
        if hit and now - hit[0] < _FORECAST_TTL_SECONDS:
            out[key] = hit[1]
        else:
            missing.append(key)
    if not missing:
        return out

    by_date: dict[str, list[tuple[float, float]]] = {}
    for lat, lon, date in missing:
        by_date.setdefault(date, []).append((lat, lon))
    dates = list(by_date)
    batches = await asyncio.gather(
        *(_fetch_open_meteo_batch(by_date[d], d) for d in dates)
    )
    for date, batch in zip(dates, batches, strict=True):
        for (lat, lon), wx in batch.items():
            out[(lat, lon, date)] = wx

    # Whatever the batch could not answer, one stadium at a time — concurrently.
    retry = [k for k in missing if out.get(k) is None]
    if retry:
        singles = await asyncio.gather(*(_fetch_open_meteo(*k) for k in retry))
        for key, wx in zip(retry, singles, strict=True):
            out[key] = wx

    stamp = time.monotonic()
    for key in missing:
        if out.get(key):
            _forecast_cache[key] = (stamp, out[key])
    return out


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

    Uses Open-Meteo (free, no key) at the game's venue — the home stadium, or
    the neutral site for international games — hour by hour around kickoff on
    the venue's local date, and flags passing/kicking/running impact. Games
    are returned worst-weather-first. Dome games are reported as neutral
    without a network call. NEVER ask for confirmation; compute and return
    immediately.

    Args:
        season: NFL season year.
        week: Regular-season week (1-18).
        teams: Optional list of team abbreviations to filter to (either side).

    Returns a dict with a `games` list, each carrying home/away, stadium, dome,
    wind/precip/temp over kickoff ±2h, `venue_source` / `venue_uncertain` and
    an `impact` block, plus a `severity`-sorted order.
    """
    from . import sleeper_tools

    default_data = {"season": season, "week": week, "games": []}
    if not isinstance(week, int) or not (1 <= week <= 18):
        return handle_validation_error("week must be an integer between 1 and 18", default_data)

    teams_filter = {normalize_team(t) or t.upper() for t in teams} if teams else None

    rows = await sleeper_tools._fetch_week_schedule(season, week, force=True)
    if not rows:
        return handle_validation_error(
            f"No schedule available for season {season}, week {week}", default_data
        )

    # Each game appears twice (bidirectional); keep the home-team row.
    home_rows = [g for g in rows if g.get("is_home") in (1, True)]

    severity_order = {"high": 0, "moderate": 1, "low": 2, "none": 3}

    def _wanted(g) -> bool:
        home = normalize_team(g.get("team")) or g.get("team")
        away = normalize_team(g.get("opponent")) or g.get("opponent")
        return not (teams_filter and home not in teams_filter and away not in teams_filter)

    # Every outdoor forecast is fetched up front, together — one request per
    # local game date — rather than one awaited request per game.
    plans = []
    for g in home_rows:
        if not _wanted(g):
            continue
        venue = resolve_venue(g)
        tz = (venue or {}).get("tz")
        plans.append((g, venue, _local_kickoff(g.get("kickoff"), tz), _game_date(g.get("kickoff"), tz)))
    wanted_keys = [
        (venue["lat"], venue["lon"], date)
        for _, venue, _, date in plans
        if venue and not venue["dome"] and venue["lat"] is not None and date
    ]
    forecasts = await _forecasts_for(wanted_keys)

    games = []
    for g, venue, local_kick, date in plans:
        entry = {
            "home": g.get("team"),
            "away": g.get("opponent"),
            "kickoff": g.get("kickoff"),
            "kickoff_local": local_kick.isoformat(timespec="minutes") if local_kick else None,
            "stadium": venue["name"] if venue else None,
            "dome": bool(venue["dome"]) if venue else None,
            "venue_source": venue["source"] if venue else None,
            "venue_uncertain": bool(venue and venue["venue_uncertain"]),
            "wind_mph": None,
            "precip_in": None,
            "temp_f": None,
        }
        if venue and venue["dome"]:
            entry["impact"] = weather_impact(0.0, 0.0, None, is_dome=True)
        elif venue and venue["venue_uncertain"]:
            entry["impact"] = {
                "severity": "unknown",
                "note": (f"Neutral-site game at {venue['name'] or 'an unknown venue'} "
                         "with no known coordinates — not forecast at the home stadium."),
            }
        elif venue and date:
            wx = kickoff_window(forecasts.get((venue["lat"], venue["lon"], date)), local_kick)
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
        "worst-weather-first, over kickoff ±2h at the venue. Wind >=15 mph fades "
        "passing; kickers are hit hardest."
    )
    if unavailable:
        msg += (
            f" ⚠️ {unavailable} game(s) have no forecast yet (beyond the ~16-day "
            "horizon, fetch failed, or unknown neutral-site venue) — reported as "
            "severity 'unknown', not calm."
        )

    return create_success_response({
        "season": season,
        "week": week,
        "games": games,
        "count": len(games),
        "forecast_unavailable": unavailable,
        "message": msg,
    })
