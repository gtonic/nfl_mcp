"""Tests for weather/wind tools (nfl_mcp.weather_tools)."""
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nfl_mcp import weather_tools
from nfl_mcp.weather_tools import (
    _fetch_open_meteo,
    _fetch_open_meteo_batch,
    _game_date,
    get_weather_forecast,
    weather_impact,
    weather_multiplier,
)


@pytest.fixture(autouse=True)
def _no_batch_network(request):
    """Keep the forecast tests offline: the batched request answers nothing,
    so they exercise the per-stadium path they mock. Batch tests opt out."""
    if request.cls is not None and request.cls.__name__ == "TestBatchedForecasts":
        yield
        return
    with patch("nfl_mcp.weather_tools._fetch_open_meteo_batch", new=AsyncMock(return_value={})):
        yield


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


def _daily(wind, precip, temp, date="2026-12-20"):
    return {"daily": {"time": [date], "wind_speed_10m_max": [wind],
                      "precipitation_sum": [precip], "temperature_2m_max": [temp]}}


class TestBatchedForecasts:
    """One request per game date for every outdoor stadium, then cached."""

    @pytest.mark.asyncio
    async def test_batch_parses_one_result_per_location_in_order(self):
        payload = [_daily(22.4, 0.41, 28.6), _daily(5.0, 0.0, 70.2)]
        client = _meteo_client(payload)
        with patch("nfl_mcp.weather_tools.create_http_client", return_value=client):
            out = await _fetch_open_meteo_batch([(42.0, -78.0), (25.9, -80.2)], "2026-12-20")
        assert out == {
            (42.0, -78.0): {"wind_mph": 22.4, "precip_in": 0.41, "temp_f": 29.0},
            (25.9, -80.2): {"wind_mph": 5.0, "precip_in": 0.0, "temp_f": 70.0},
        }
        params = client.get.await_args.kwargs["params"]
        assert params["latitude"] == "42.0,25.9"
        assert params["start_date"] == params["end_date"] == "2026-12-20"

    @pytest.mark.asyncio
    async def test_batch_failure_is_empty(self):
        with patch("nfl_mcp.weather_tools.create_http_client",
                   return_value=_meteo_client(status=400)):
            assert await _fetch_open_meteo_batch([(42.0, -78.0)], "2026-12-20") == {}

    @pytest.mark.asyncio
    async def test_week_is_one_request_per_date_and_cached(self):
        rows = [
            {"team": "BUF", "opponent": "MIA", "is_home": 1, "kickoff": "2026-12-20T18:00Z"},
            {"team": "CHI", "opponent": "GB", "is_home": 1, "kickoff": "2026-12-20T18:00Z"},
            {"team": "GB", "opponent": "DET", "is_home": 1, "kickoff": "2026-12-21T18:00Z"},
            {"team": "DET", "opponent": "NYG", "is_home": 1, "kickoff": "2026-12-20T18:00Z"},
        ]

        async def batch(points, date):
            return {pt: {"wind_mph": 10.0, "precip_in": 0.0, "temp_f": 40.0} for pt in points}

        batch_mock = AsyncMock(side_effect=batch)
        single = AsyncMock(return_value=None)
        with patch("nfl_mcp.sleeper_tools._fetch_week_schedule",
                   new=AsyncMock(return_value=rows)), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo_batch", new=batch_mock), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo", new=single):
            first = await get_weather_forecast(season=2026, week=16)
            second = await get_weather_forecast(season=2026, week=16)

        # Two game dates among the outdoor stadiums -> two requests, no
        # per-stadium fallback, and the dome is never asked about.
        assert batch_mock.await_count == 2
        assert sorted(len(c.args[0]) for c in batch_mock.await_args_list) == [1, 2]
        assert single.await_count == 0
        # The repeat call inside the TTL is served from the cache.
        assert first["games"] == second["games"]
        assert first["forecast_unavailable"] == 0

    @pytest.mark.asyncio
    async def test_batch_miss_falls_back_to_single_requests(self):
        rows = [{"team": "BUF", "opponent": "MIA", "is_home": 1, "kickoff": "2026-12-20T18:00Z"}]
        windy = {"wind_mph": 26.0, "precip_in": 0.1, "temp_f": 30.0}
        with patch("nfl_mcp.sleeper_tools._fetch_week_schedule",
                   new=AsyncMock(return_value=rows)), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo_batch",
                   new=AsyncMock(return_value={})), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo",
                   new=AsyncMock(return_value=windy)) as single:
            res = await get_weather_forecast(season=2026, week=16)
        assert single.await_count == 1
        assert res["games"][0]["wind_mph"] == 26.0
        # Only successes are cached; a failed forecast is asked for again.
        assert weather_tools._forecast_cache
