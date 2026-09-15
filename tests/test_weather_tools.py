"""Tests for weather/wind tools (nfl_mcp.weather_tools)."""
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nfl_mcp.weather_tools import (
    _fetch_open_meteo,
    _game_date,
    get_weather_forecast,
    weather_impact,
    weather_multiplier,
)


class TestWeatherMultiplier:
    def test_dome_is_neutral(self):
        assert weather_multiplier("QB", wind_mph=40, precip_in=1.0, is_dome=True) == 1.0
        assert weather_multiplier("K", wind_mph=40, is_dome=True) == 1.0

    def test_calm_is_neutral(self):
        assert weather_multiplier("QB", wind_mph=8) == 1.0
        assert weather_multiplier("WR", wind_mph=10, precip_in=0.0) == 1.0

    def test_high_wind_downgrades_passing_and_kicking(self):
        assert weather_multiplier("QB", wind_mph=25) < 1.0
        # Kicker hit harder than passers at the same wind.
        assert weather_multiplier("K", wind_mph=25) < weather_multiplier("QB", wind_mph=25)

    def test_rb_neutral_in_wind(self):
        assert weather_multiplier("RB", wind_mph=30) == 1.0

    def test_bounded_floor(self):
        assert weather_multiplier("K", wind_mph=200, precip_in=5) >= 0.7


class TestWeatherImpact:
    def test_dome_none(self):
        imp = weather_impact(30, 1.0, 10, is_dome=True)
        assert imp["severity"] == "none"
        assert imp["passing"] == "neutral" and imp["kicking"] == "neutral"

    def test_high_wind_high_severity(self):
        imp = weather_impact(22, 0.0, 45, is_dome=False)
        assert imp["severity"] == "high"
        assert imp["passing"] == "downgrade" and imp["kicking"] == "downgrade"

    def test_mild_low_severity(self):
        imp = weather_impact(8, 0.0, 60, is_dome=False)
        assert imp["severity"] == "low"
        assert imp["passing"] == "neutral"


class TestGameDate:
    def test_extracts_date(self):
        assert _game_date("2026-12-20T18:00Z") == "2026-12-20"
        assert _game_date(None) is None
        assert _game_date("bad") is None


def _meteo_client(payload=None, status=200):
    resp = Mock()
    resp.status_code = status
    resp.json = Mock(return_value=payload or {})
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


class TestFetchOpenMeteo:
    @pytest.mark.asyncio
    async def test_parses_daily_values(self):
        payload = {"daily": {
            "time": ["2026-12-20"],
            "wind_speed_10m_max": [22.4],
            "precipitation_sum": [0.41],
            "temperature_2m_max": [28.6],
        }}
        with patch("nfl_mcp.weather_tools.create_http_client", return_value=_meteo_client(payload)):
            wx = await _fetch_open_meteo(42.0, -78.0, "2026-12-20")
        assert wx == {"wind_mph": 22.4, "precip_in": 0.41, "temp_f": 29.0}

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        with patch("nfl_mcp.weather_tools.create_http_client", return_value=_meteo_client(status=400)):
            assert await _fetch_open_meteo(42.0, -78.0, "2026-12-20") is None


class TestGetWeatherForecast:
    def _schedule(self):
        # Bidirectional rows; BUF (outdoor) home vs MIA, DET (dome) home vs GB.
        return [
            {"team": "BUF", "opponent": "MIA", "is_home": 1, "kickoff": "2026-12-20T18:00Z"},
            {"team": "MIA", "opponent": "BUF", "is_home": 0, "kickoff": "2026-12-20T18:00Z"},
            {"team": "DET", "opponent": "GB", "is_home": 1, "kickoff": "2026-12-21T18:00Z"},
            {"team": "GB", "opponent": "DET", "is_home": 0, "kickoff": "2026-12-21T18:00Z"},
        ]

    @pytest.mark.asyncio
    async def test_sorts_worst_first_and_domes_neutral(self):
        windy = {"wind_mph": 26.0, "precip_in": 0.1, "temp_f": 30.0}
        with patch("nfl_mcp.sleeper_tools._fetch_week_schedule",
                   new=AsyncMock(return_value=self._schedule())), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo",
                   new=AsyncMock(return_value=windy)) as mock_meteo:
            result = await get_weather_forecast(season=2026, week=16)

        assert result["success"] is True
        assert result["count"] == 2
        games = result["games"]
        # BUF (high wind) sorts before DET (dome / none).
        assert games[0]["home"] == "BUF"
        assert games[0]["impact"]["severity"] == "high"
        assert games[0]["impact"]["passing"] == "downgrade"
        det = next(g for g in games if g["home"] == "DET")
        assert det["dome"] is True
        assert det["impact"]["severity"] == "none"
        # Open-Meteo only called for the outdoor game, not the dome.
        assert mock_meteo.await_count == 1

    @pytest.mark.asyncio
    async def test_team_filter(self):
        with patch("nfl_mcp.sleeper_tools._fetch_week_schedule",
                   new=AsyncMock(return_value=self._schedule())), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo",
                   new=AsyncMock(return_value={"wind_mph": 10.0, "precip_in": 0.0, "temp_f": 50.0})):
            result = await get_weather_forecast(season=2026, week=16, teams=["GB"])
        assert result["count"] == 1
        assert result["games"][0]["home"] == "DET"  # GB is the away team

    @pytest.mark.asyncio
    async def test_invalid_week_rejected(self):
        result = await get_weather_forecast(season=2026, week=25)
        assert result["success"] is False
        assert "week must be" in result["error"]


class TestForecastUnavailable:
    """Games beyond the forecast horizon are flagged 'unknown' and counted."""

    @pytest.mark.asyncio
    async def test_forecast_unavailable_counted(self):
        rows = [
            {"team": "GB", "opponent": "CHI", "is_home": 1, "kickoff": "2026-09-13T17:00Z"},
            {"team": "CHI", "opponent": "GB", "is_home": 0, "kickoff": "2026-09-13T17:00Z"},
        ]
        with patch("nfl_mcp.sleeper_tools._fetch_week_schedule",
                   new=AsyncMock(return_value=rows)), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo",
                   new=AsyncMock(return_value=None)):
            res = await get_weather_forecast(season=2026, week=2)
        assert res["success"] is True
        assert res["count"] == 1                       # one home game (GB)
        assert res["forecast_unavailable"] == 1        # out-of-range -> unknown
        assert res["games"][0]["impact"]["severity"] == "unknown"
        assert "no forecast" in res["message"]
