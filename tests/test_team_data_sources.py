"""Team-keyed ESPN tools: code normalization, current injuries, standings
labels and the bye week on both schedule paths."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nfl_mcp import nfl_tools
from nfl_mcp.nfl_tools import (
    get_depth_chart,
    get_nfl_standings,
    get_team_injuries,
    get_team_schedule,
)


def _client(payload=None, by_url=None):
    """A mocked httpx client; ``by_url`` answers per URL, else ``payload``."""
    def _resp(data):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = data
        r.text = data if isinstance(data, str) else ""
        r.raise_for_status = MagicMock()
        return r

    async def _get(url, *args, **kwargs):
        if by_url is not None:
            return _resp(by_url[url])
        return _resp(payload)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=_get)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _url(client) -> str:
    return client.get.await_args_list[0].args[0]


class TestTeamCodesReachEspnCanonical:
    """ESPN answers WAS / LA / JAC with HTTP 400; every tool sends its own code."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("given,sent", [("WAS", "WSH"), ("la", "LAR"), ("JAC", "JAX")])
    async def test_schedule(self, given, sent):
        client = _client({"team": {"displayName": "x"}, "events": []})
        with patch("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", False), \
             patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            result = await get_team_schedule(given, 2026)
        assert f"/teams/{sent}/schedule" in _url(client)
        assert result["team_id"] == sent

    @pytest.mark.asyncio
    async def test_injuries(self):
        client = _client({"items": []})
        with patch("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", False), \
             patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            result = await get_team_injuries("was")
        assert "/teams/WSH/injuries" in _url(client)
        assert result["team_id"] == "WSH"

    @pytest.mark.asyncio
    async def test_depth_chart(self):
        client = _client("<html></html>")
        with patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            await get_depth_chart("JAC")
        assert _url(client).endswith("/name/JAX")

    @pytest.mark.asyncio
    async def test_schedule_matches_own_team_under_any_spelling(self):
        # ESPN spells the team WSH; the caller said WAS. Home/away and the
        # opponent must still resolve, not the Commanders as their own opponent.
        event = {
            "id": "1", "date": "2026-09-13T17:00Z", "week": {"number": 1},
            "competitions": [{"competitors": [
                {"homeAway": "home", "team": {"abbreviation": "WSH", "displayName": "Commanders"}},
                {"homeAway": "away", "team": {"abbreviation": "NYG", "displayName": "Giants"}},
            ], "status": {"type": {"name": "STATUS_SCHEDULED"}}}],
        }
        client = _client({"team": {"displayName": "Commanders"}, "events": [event]})
        with patch("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", False), \
             patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            result = await get_team_schedule("WAS", 2026)
        game = result["schedule"][0]
        assert game["is_home"] is True
        assert game["opponent"]["abbreviation"] == "NYG"

    def test_athletes_by_team_accepts_any_spelling(self, tmp_path):
        from nfl_mcp.athlete_tools import get_athletes_by_team
        from nfl_mcp.database import NFLDatabase

        db = NFLDatabase(str(tmp_path / "t.db"))
        db.upsert_athletes({
            "1": {"full_name": "Jayden Daniels", "team": "WAS", "position": "QB"},
            "2": {"full_name": "Puka Nacua", "team": "LAR", "position": "WR"},
        })
        for spelling in ("WAS", "wsh", "Washington Commanders"):
            result = get_athletes_by_team(db, spelling)
            assert result["count"] == 1, spelling
            assert result["team_id"] == "WSH"
        assert get_athletes_by_team(db, "LA")["count"] == 1


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%MZ")


class TestTeamInjuriesAreCurrent:
    """ESPN's list is every player's latest report ever; only open ones count."""

    @pytest.mark.asyncio
    async def test_api_path_drops_active_and_last_season(self):
        base = "http://espn/injuries/"
        details = {
            base + "1": {"status": "Questionable", "date": _iso(2),
                         "athlete": {"$ref": "http://espn/athletes/1"}},
            base + "2": {"status": "Active", "date": _iso(1),
                         "athlete": {"$ref": "http://espn/athletes/2"}},
            base + "3": {"status": "Injured Reserve", "date": _iso(60),
                         "details": {"returnDate": (datetime.now(UTC) + timedelta(days=40)).date().isoformat()},
                         "athlete": {"$ref": "http://espn/athletes/3"}},
            base + "4": {"status": "Out", "date": _iso(400),
                         "details": {"returnDate": "2025-02-15"},
                         "athlete": {"$ref": "http://espn/athletes/4"}},
        }
        athletes = {
            f"http://espn/athletes/{i}": {"id": str(i), "displayName": f"Player {i}",
                                           "position": {"abbreviation": "WR"}}
            for i in range(1, 5)
        }
        listing = {"items": [{"$ref": u} for u in details]}
        url = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/KC/injuries?limit=200"
        client = _client(by_url={url: listing, **details, **athletes})
        with patch("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", False), \
             patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            result = await get_team_injuries("KC", limit=10)

        assert [i["player_name"] for i in result["injuries"]] == ["Player 1", "Player 3"]
        assert result["count"] == 2
        assert result["resolved_excluded"] == 2
        # A resolved report costs no athlete lookup.
        fetched = [c.args[0] for c in client.get.await_args_list]
        assert "http://espn/athletes/2" not in fetched
        assert "http://espn/athletes/4" not in fetched

    @pytest.mark.asyncio
    async def test_limit_applies_after_filtering(self):
        base = "http://espn/injuries/"
        details = {base + str(i): {"status": "Active", "date": _iso(1)} for i in range(5)}
        details[base + "9"] = {"status": "Out", "date": _iso(1),
                               "athlete": {"displayName": "Hurt Guy"}}
        listing = {"items": [{"$ref": u} for u in details]}
        url = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/KC/injuries?limit=200"
        client = _client(by_url={url: listing, **details})
        with patch("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", False), \
             patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            result = await get_team_injuries("KC", limit=1)
        assert [i["player_name"] for i in result["injuries"]] == ["Hurt Guy"]

    @pytest.mark.asyncio
    async def test_cache_path_drops_active_and_survives_null_status(self):
        db = MagicMock()
        db.get_team_injuries_from_cache.return_value = [
            {"player_id": "1", "player_name": "A", "injury_status": "Active",
             "date_reported": _iso(1)},
            {"player_id": "2", "player_name": "B", "injury_status": "Out",
             "date_reported": _iso(3)},
            {"player_id": "3", "player_name": "C", "injury_status": None,
             "date_reported": _iso(3)},
        ]
        with patch("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", True), \
             patch("nfl_mcp.database.get_nfl_database", return_value=db):
            result = await get_team_injuries("KC")
        assert result["cache_source"] == "database"
        assert [i["player_name"] for i in result["injuries"]] == ["B", "C"]
        assert result["injuries"][0]["severity"] == "High"
        assert result["resolved_excluded"] == 1
        db.get_team_injuries_from_cache.assert_called_once_with("KC", max_age_hours=12)


class TestScheduleByeWeekOnBothPaths:
    def _weeks(self, bye: int):
        return [
            {"week": w, "opponent": "BUF", "is_home": w % 2, "kickoff": f"2026-10-{w:02d}T17:00Z"}
            for w in range(1, 19) if w != bye
        ]

    @pytest.mark.asyncio
    async def test_cache_path_returns_bye_week(self):
        db = MagicMock()
        db.get_team_schedule_from_cache.return_value = self._weeks(bye=7)
        with patch("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", True), \
             patch("nfl_mcp.database.get_nfl_database", return_value=db):
            result = await get_team_schedule("was", 2026)
        assert result["cache_source"] == "database"
        assert result["bye_week"] == 7
        db.get_team_schedule_from_cache.assert_called_once_with("WSH", 2026)

    @pytest.mark.asyncio
    async def test_partial_cache_has_no_bye_guess(self):
        db = MagicMock()
        db.get_team_schedule_from_cache.return_value = self._weeks(bye=7)[:3]
        with patch("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", True), \
             patch("nfl_mcp.database.get_nfl_database", return_value=db):
            result = await get_team_schedule("KC", 2026)
        assert result["bye_week"] is None

    @pytest.mark.asyncio
    async def test_api_path_returns_bye_week(self):
        events = [{"id": str(w), "week": {"number": w}, "competitions": []}
                  for w in range(1, 19) if w != 11]
        client = _client({"team": {"displayName": "Chiefs"}, "events": events})
        with patch("nfl_mcp.sleeper_tools.ADVANCED_ENRICH_ENABLED", False), \
             patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            result = await get_team_schedule("KC", 2026)
        assert result["bye_week"] == 11


def _standings(*records):
    entries = [
        {"team": {"id": str(i), "displayName": f"T{i}", "abbreviation": f"T{i}"},
         "stats": [{"name": "wins", "value": w}, {"name": "losses", "value": l},
                   {"name": "ties", "value": t}]}
        for i, (w, l, t) in enumerate(records)
    ]
    return {"children": [{"abbreviation": "AFC", "standings": {"entries": entries}}]}


class TestStandingsLabels:
    @pytest.mark.asyncio
    async def test_early_season_is_not_development_mode(self):
        client = _client(_standings((3, 0, 0), (0, 3, 0), (1, 2, 0)))
        with patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            result = await get_nfl_standings(2026)
        levels = {t["motivation_level"] for t in result["standings"]}
        assert levels == {"High (Early season)"}

    @pytest.mark.asyncio
    async def test_labels_follow_win_percentage_later(self):
        client = _client(_standings((2, 9, 0), (12, 1, 0), (6, 5, 0), (4, 4, 0)))
        with patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            result = await get_nfl_standings(2026)
        levels = [t["motivation_level"] for t in result["standings"]]
        assert levels == [
            "Medium (Development mode)",
            "Low (Playoff lock)",
            "High (Playoff hunt)",
            # 4-4 was "Development mode" under the old `wins <= 4` rule.
            "High (Playoff hunt)",
        ]


def test_is_current_injury_rules():
    now = datetime(2026, 9, 23, tzinfo=UTC)
    assert nfl_tools._is_current_injury("Active", "2026-09-22T00:00Z", now=now) is False
    assert nfl_tools._is_current_injury("Questionable", "2026-09-20T00:00Z", now=now) is True
    assert nfl_tools._is_current_injury("Out", "2025-10-01T00:00Z", now=now) is False
    assert nfl_tools._is_current_injury(
        "Injured Reserve", "2025-12-01T00:00Z", "2027-02-15", now=now) is True
    assert nfl_tools._is_current_injury("Out", None, now=now) is True
