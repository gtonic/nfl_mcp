"""Tests for new fantasy intelligence APIs."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nfl_mcp.nfl_tools import get_nfl_standings, get_team_injuries, get_team_player_stats

# Reports older than ~6 months are last season's and filtered out as resolved.
RECENT = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%MZ")


class TestTeamInjuries:
    """Test cases for team injuries API."""

    @pytest.mark.asyncio
    async def test_get_team_injuries_success(self):
        """Test successful injury report fetch."""
        mock_response_data = {
            "items": [
                {
                    "athlete": {
                        "displayName": "Patrick Mahomes",
                        "id": "3139477",
                        "position": {"abbreviation": "QB"}
                    },
                    "team": {
                        "displayName": "Kansas City Chiefs"
                    },
                    "status": {"name": "Questionable"},
                    "description": "Ankle injury",
                    "date": RECENT,
                    "type": {"name": "Ankle"}
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client

            result = await get_team_injuries("KC", 10)

            assert result["success"] is True
            assert result["team_id"] == "KC"
            assert result["count"] == 1
            assert len(result["injuries"]) == 1

            injury = result["injuries"][0]
            assert injury["player_name"] == "Patrick Mahomes"
            assert injury["position"] == "QB"
            assert injury["status"] == "Questionable"
            assert injury["severity"] == "Medium"  # Questionable should be medium severity

    @pytest.mark.asyncio
    async def test_get_team_injuries_invalid_team(self):
        """Test injury report with invalid team ID."""
        result = await get_team_injuries("", 10)

        assert result["success"] is False
        assert "Team ID is required" in result["error"]

    @pytest.mark.asyncio
    async def test_get_team_injuries_404_error(self):
        """Test injury report when team not found."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=mock_response
        )

        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client

            result = await get_team_injuries("XXX", 10)

            assert result["success"] is True  # We handle 404s gracefully
            assert result["count"] == 0
            assert "No injury data found" in result["message"]


class TestTeamPlayerStats:
    """Test cases for team player statistics API."""

    @pytest.mark.asyncio
    async def test_get_team_player_stats_success(self):
        """Season totals per player from Sleeper, for the team asked about."""
        mock_response_data = [
            {"player_id": "4046", "team": "KC",
             "player": {"first_name": "Patrick", "last_name": "Mahomes", "position": "QB"},
             "stats": {"gp": 2.0, "pass_yd": 566.0, "pass_td": 5.0, "pass_att": 74.0,
                       "rush_yd": 40.0, "pts_ppr": 51.64, "pts_half_ppr": 51.64,
                       "pts_std": 51.64}},
            {"player_id": "1466", "team": "KC",
             "player": {"first_name": "Travis", "last_name": "Kelce", "position": "TE"},
             "stats": {"gp": 2.0, "rec": 14.0, "rec_tgt": 18.0, "rec_yd": 172.0,
                       "pts_ppr": 35.2}},
            {"player_id": "999", "team": "KC",
             "player": {"first_name": "Practice", "last_name": "Squad", "position": "WR"},
             "stats": {"gms_active": 0.0}},
            {"player_id": "6", "team": "BUF",
             "player": {"first_name": "Josh", "last_name": "Allen", "position": "QB"},
             "stats": {"gp": 2.0, "pts_ppr": 60.0}},
        ]

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client

            result = await get_team_player_stats("kc", 2025, 2, 50)

            assert result["success"] is True
            assert result["team_id"] == "KC"
            assert result["team_name"] == "Kansas City Chiefs"
            assert result["season"] == 2025
            assert result["season_type"] == 2
            # Only KC players who have played; sorted by PPR points.
            assert [p["player_name"] for p in result["player_stats"]] == [
                "Patrick Mahomes", "Travis Kelce"]
            qb, te = result["player_stats"]
            assert qb["passing"]["yards"] == 566.0 and qb["passing"]["touchdowns"] == 5.0
            assert qb["games_played"] == 2.0
            assert qb["fantasy_points"]["ppr"] == 51.64
            assert te["receiving"] == {"targets": 18.0, "receptions": 14.0, "yards": 172.0}
            assert "passing" not in te
            url = mock_client.get.await_args.args[0]
            params = mock_client.get.await_args.kwargs["params"]
            assert url.endswith("/stats/nfl/2025")
            assert ("season_type", "regular") in params

    @pytest.mark.asyncio
    async def test_team_player_stats_season_type_and_aliases(self):
        """season_type picks the season part; Sleeper's WAS matches any spelling."""
        rows = [{"player_id": "1", "team": "WAS",
                 "player": {"first_name": "Jayden", "last_name": "Daniels", "position": "QB"},
                 "stats": {"gp": 1.0, "pts_ppr": 20.0}}]
        mock_response = MagicMock()
        mock_response.json.return_value = rows
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client
            result = await get_team_player_stats("WSH", 2025, 3, 10)
            # Cached: a second team from the same season costs no request.
            again = await get_team_player_stats("Washington Commanders", 2025, 3, 10)

        assert result["team_id"] == "WSH" and result["count"] == 1
        assert again["count"] == 1
        assert mock_client.get.await_count == 1
        assert ("season_type", "post") in mock_client.get.await_args.kwargs["params"]

    @pytest.mark.asyncio
    async def test_team_player_stats_rejects_off_season_type(self):
        result = await get_team_player_stats("KC", 2025, 4, 10)
        assert result["success"] is False
        assert "season_type" in result["error"]

    @pytest.mark.asyncio
    async def test_get_team_player_stats_invalid_team(self):
        """Test player stats with invalid team ID."""
        result = await get_team_player_stats(None, 2025, 2, 50)

        assert result["success"] is False
        assert "Team ID is required" in result["error"]


class TestNFLStandings:
    """Test cases for NFL standings API."""

    @pytest.mark.asyncio
    async def test_get_nfl_standings_success(self):
        """Test successful standings fetch."""
        mock_response_data = {
            "children": [
                {
                    "standings": {
                        "entries": [
                            {
                                "team": {
                                    "id": "12",
                                    "displayName": "Kansas City Chiefs",
                                    "abbreviation": "KC"
                                },
                                "stats": [
                                    {"name": "wins", "value": 15},
                                    {"name": "losses", "value": 2},
                                    {"name": "ties", "value": 0},
                                    {"name": "winPercent", "value": 0.882}
                                ]
                            },
                            {
                                "team": {
                                    "id": "16",
                                    "displayName": "New York Giants",
                                    "abbreviation": "NYG"
                                },
                                "stats": [
                                    {"name": "wins", "value": 3},
                                    {"name": "losses", "value": 14},
                                    {"name": "ties", "value": 0},
                                    {"name": "winPercent", "value": 0.176}
                                ]
                            }
                        ]
                    }
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client

            result = await get_nfl_standings(2025, 2, None)

            assert result["success"] is True
            assert result["season"] == 2025
            assert result["season_type"] == 2
            assert result["count"] == 2

            # Check high-win team context
            kc_team = next(t for t in result["standings"] if t["abbreviation"] == "KC")
            assert kc_team["wins"] == 15
            assert kc_team["motivation_level"] == "Low (Playoff lock)"
            assert "rest starters" in kc_team["fantasy_context"]

            # Check low-win team context
            nyg_team = next(t for t in result["standings"] if t["abbreviation"] == "NYG")
            assert nyg_team["wins"] == 3
            assert nyg_team["motivation_level"] == "Medium (Development mode)"
            assert "evaluate young players" in nyg_team["fantasy_context"]

    @pytest.mark.asyncio
    async def test_get_nfl_standings_defaults(self):
        """Test standings with default parameters."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"children": []}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        with patch('nfl_mcp.nfl_tools.create_http_client') as mock_create_client:
            mock_create_client.return_value.__aenter__.return_value = mock_client

            result = await get_nfl_standings()

            assert result["success"] is True
            assert result["season"] == 2026  # Default
            assert result["season_type"] == 2  # Default
            assert result["group"] is None  # Default
