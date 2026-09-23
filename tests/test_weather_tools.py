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


def _series(wind, precip, temp, date="2026-12-20"):
    """A day's hourly series with the same reading every hour."""
    return {"hours": [(f"{date}T{h:02d}:00", wind, precip, temp) for h in range(24)]}


def _hourly(wind, precip, temp, date="2026-12-20"):
    """An Open-Meteo ``hourly`` payload with the same reading every hour."""
    return {"hourly": {
        "time": [f"{date}T{h:02d}:00" for h in range(24)],
        "wind_speed_10m": [wind] * 24,
        "precipitation": [precip] * 24,
        "temperature_2m": [temp] * 24,
    }}


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
    async def test_parses_hourly_values(self):
        payload = _hourly(22.4, 0.41, 28.6)
        client = _meteo_client(payload)
        with patch("nfl_mcp.weather_tools.create_http_client", return_value=client):
            wx = await _fetch_open_meteo(42.0, -78.0, "2026-12-20")
        assert len(wx["hours"]) == 24
        assert wx["hours"][13] == ("2026-12-20T13:00", 22.4, 0.41, 28.6)
        params = client.get.await_args.kwargs["params"]
        assert "hourly" in params and "daily" not in params
        assert params["timezone"] == "auto"

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
        windy = _series(26.0, 0.0, 30.0)
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
                   new=AsyncMock(return_value=_series(10.0, 0.0, 50.0))):
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




class TestBatchedForecasts:
    """One request per game date for every outdoor stadium, then cached."""

    @pytest.mark.asyncio
    async def test_batch_parses_one_result_per_location_in_order(self):
        payload = [_hourly(22.4, 0.41, 28.6), _hourly(5.0, 0.0, 70.2)]
        client = _meteo_client(payload)
        with patch("nfl_mcp.weather_tools.create_http_client", return_value=client):
            out = await _fetch_open_meteo_batch([(42.0, -78.0), (25.9, -80.2)], "2026-12-20")
        assert out[(42.0, -78.0)]["hours"][0] == ("2026-12-20T00:00", 22.4, 0.41, 28.6)
        assert out[(25.9, -80.2)]["hours"][0] == ("2026-12-20T00:00", 5.0, 0.0, 70.2)
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
            return {pt: _series(10.0, 0.0, 40.0, date) for pt in points}

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
        windy = _series(26.0, 0.0, 30.0)
        with patch("nfl_mcp.sleeper_tools._fetch_week_schedule",
                   new=AsyncMock(return_value=rows)), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo_batch",
                   new=AsyncMock(return_value={})), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo",
                   new=AsyncMock(return_value=windy)) as single:
            res = await get_weather_forecast(season=2026, week=16)
        assert single.await_count == 1
        assert res["games"][0]["wind_mph"] == 26.0
        assert res["games"][0]["precip_in"] == 0.0
        # Only successes are cached; a failed forecast is asked for again.
        assert weather_tools._forecast_cache


class TestLocalDateAndKickoffWindow:
    """The forecast is for the venue's local date and the hours around kickoff."""

    def test_sunday_night_game_uses_the_local_date(self):
        # DEN-LAR, Sunday night: 00:20Z is Monday in UTC, Sunday 18:20 in Denver.
        assert _game_date("2026-09-28T00:20Z", "America/Denver") == "2026-09-27"
        assert _game_date("2026-09-28T00:20Z") == "2026-09-28"  # no tz: UTC

    def test_window_is_kickoff_plus_minus_two_hours(self):
        hours = [(f"2026-09-27T{h:02d}:00", 5.0, 0.0, 70.0) for h in range(24)]
        # A 3 a.m. gale and a morning shower are not game weather.
        hours[3] = ("2026-09-27T03:00", 40.0, 1.0, 50.0)
        hours[18] = ("2026-09-27T18:00", 9.0, 0.0, 64.0)
        hours[19] = ("2026-09-27T19:00", 18.0, 0.2, 61.0)
        hours[20] = ("2026-09-27T20:00", 12.0, 0.1, 58.0)
        wx = weather_tools.kickoff_window(
            {"hours": hours}, weather_tools._local_kickoff("2026-09-28T00:20Z", "America/Denver")
        )
        # 16:20-20:20 -> hours 17..20; temperature at the hour nearest kickoff.
        assert wx == {"wind_mph": 18.0, "precip_in": 0.3, "temp_f": 64.0}

    def test_window_outside_the_series_is_none(self):
        hours = [("2026-09-27T12:00", 5.0, 0.0, 70.0)]
        assert weather_tools.kickoff_window(
            {"hours": hours}, weather_tools._local_kickoff("2026-09-28T00:20Z", "America/Denver")
        ) is None

    @pytest.mark.asyncio
    async def test_snf_forecast_requests_the_local_date(self):
        rows = [{"team": "DEN", "opponent": "LAR", "is_home": 1, "kickoff": "2026-09-28T00:20Z"}]
        single = AsyncMock(return_value=_series(22.0, 0.0, 60.0, "2026-09-27"))
        with patch("nfl_mcp.sleeper_tools._fetch_week_schedule", new=AsyncMock(return_value=rows)), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo", new=single):
            res = await get_weather_forecast(season=2026, week=4)
        assert single.await_args.args[2] == "2026-09-27"
        game = res["games"][0]
        assert game["kickoff_local"] == "2026-09-27T18:20"
        assert game["wind_mph"] == 22.0 and game["impact"]["severity"] == "high"


def _event(venue: dict, neutral: bool) -> dict:
    return {"competitions": [{"neutralSite": neutral, "venue": venue}]}


class TestNeutralSiteVenues:
    def test_home_game_uses_home_stadium(self):
        v = weather_tools.resolve_venue(
            {"team": "KC", "raw": _event({"id": "3622", "fullName": "GEHA Field"}, False)}
        )
        assert v["name"] == "Arrowhead Stadium" and v["source"] == "home_stadium"
        assert v["venue_uncertain"] is False

    def test_team_codes_are_normalized(self):
        assert weather_tools.resolve_venue({"team": "WAS"})["name"] == "Northwest Stadium"

    def test_international_game_uses_the_venue(self):
        v = weather_tools.resolve_venue({"team": "LAR", "raw": _event(
            {"id": "9119", "fullName": "Melbourne Cricket Ground", "indoor": False}, True)})
        assert v["name"] == "Melbourne Cricket Ground"
        assert v["tz"] == "Australia/Melbourne" and v["lat"] < 0
        assert v["dome"] is False and v["source"] == "neutral_site"

    def test_venue_matched_by_name_from_stored_json(self):
        import json
        raw = json.dumps(_event({"fullName": "Tottenham Hotspur Stadium"}, True))
        v = weather_tools.resolve_venue({"team": "JAX", "raw": raw})
        assert v["tz"] == "Europe/London"

    def test_unknown_neutral_site_is_uncertain(self):
        v = weather_tools.resolve_venue({"team": "MIA", "raw": _event(
            {"id": "999", "fullName": "Somewhere Arena",
             "address": {"city": "Oslo", "country": "Norway"}}, True)})
        assert v["venue_uncertain"] is True and v["lat"] is None

    @pytest.mark.asyncio
    async def test_forecast_at_the_neutral_site_on_its_local_date(self):
        # SF @ LAR in Melbourne: 00:35Z Friday is 10:35 Friday local time.
        rows = [{"team": "LAR", "opponent": "SF", "is_home": 1, "kickoff": "2026-09-11T00:35Z",
                 "raw": _event({"id": "9119", "fullName": "Melbourne Cricket Ground",
                                "indoor": False}, True)}]
        single = AsyncMock(return_value=_series(16.0, 0.0, 55.0, "2026-09-11"))
        with patch("nfl_mcp.sleeper_tools._fetch_week_schedule", new=AsyncMock(return_value=rows)), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo", new=single):
            res = await get_weather_forecast(season=2026, week=1)
        lat, _lon, date = single.await_args.args
        assert lat == weather_tools.VENUES["9119"]["lat"] and date == "2026-09-11"
        game = res["games"][0]
        assert game["stadium"] == "Melbourne Cricket Ground"
        assert game["dome"] is False  # SoFi's roof is not in Melbourne
        assert game["kickoff_local"] == "2026-09-11T10:35"
        assert game["wind_mph"] == 16.0

    @pytest.mark.asyncio
    async def test_unknown_neutral_site_is_not_forecast_at_home(self):
        rows = [{"team": "MIA", "opponent": "BUF", "is_home": 1, "kickoff": "2026-10-04T13:30Z",
                 "raw": _event({"id": "999", "fullName": "Somewhere Arena"}, True)}]
        single = AsyncMock(return_value=_series(30.0, 0.0, 50.0, "2026-10-04"))
        with patch("nfl_mcp.sleeper_tools._fetch_week_schedule", new=AsyncMock(return_value=rows)), \
             patch("nfl_mcp.weather_tools._fetch_open_meteo", new=single):
            res = await get_weather_forecast(season=2026, week=4)
        assert single.await_count == 0
        game = res["games"][0]
        assert game["venue_uncertain"] is True
        assert game["wind_mph"] is None and game["impact"]["severity"] == "unknown"
        assert res["forecast_unavailable"] == 1
