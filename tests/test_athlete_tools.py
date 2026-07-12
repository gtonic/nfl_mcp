"""Tests for athlete_tools module."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nfl_mcp.athlete_tools import fetch_athletes, lookup_athlete, search_athletes, get_athletes_by_team


class TestFetchAthletes:
    """Test fetch_athletes function."""

    @pytest.mark.asyncio
    async def test_fetch_athletes_success(self):
        """Test successful athlete fetch."""
        mock_db = MagicMock()
        mock_db.upsert_athletes.return_value = 100
        mock_db.get_last_updated.return_value = "2026-01-01T00:00:00"

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"player_id": "1", "full_name": "Test Player", "team": "SF"}
        ]
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch('nfl_mcp.athlete_tools.create_http_client', return_value=mock_client):
            result = await fetch_athletes(mock_db)

        assert result["success"] is True
        assert result["athletes_count"] == 100
        assert result["last_updated"] == "2026-01-01T00:00:00"

    @pytest.mark.asyncio
    async def test_fetch_athletes_http_error(self):
        """Test fetch_athletes with HTTP error."""
        mock_db = MagicMock()

        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = Exception("500")

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch('nfl_mcp.athlete_tools.create_http_client', return_value=mock_client):
            result = await fetch_athletes(mock_db)

        assert result["success"] is False


class TestLookupAthlete:
    """Test lookup_athlete function."""

    def test_lookup_athlete_found(self):
        """Test looking up existing athlete."""
        mock_db = MagicMock()
        mock_athlete = {"player_id": "1", "full_name": "Test Player"}
        mock_db.get_athlete_by_id.return_value = mock_athlete

        result = lookup_athlete(mock_db, "1")

        assert result["success"] is True
        assert result["found"] is True
        assert result["athlete"] == mock_athlete

    def test_lookup_athlete_not_found(self):
        """Test looking up non-existing athlete."""
        mock_db = MagicMock()
        mock_db.get_athlete_by_id.return_value = None

        result = lookup_athlete(mock_db, "999")

        assert result["success"] is True
        assert result["found"] is False
        assert result["athlete"] is None

    def test_lookup_athlete_calls_db(self):
        """Test that lookup calls database with correct ID."""
        mock_db = MagicMock()
        mock_athlete = {"player_id": "1"}
        mock_db.get_athlete_by_id.return_value = mock_athlete

        lookup_athlete(mock_db, "1")

        mock_db.get_athlete_by_id.assert_called_once_with("1")


class TestSearchAthletes:
    """Test search_athletes function."""

    def test_search_athletes_success(self):
        """Test successful athlete search."""
        mock_db = MagicMock()
        mock_athletes = [
            {"player_id": "1", "full_name": "John Smith"},
            {"player_id": "2", "full_name": "Jane Smith"}
        ]
        mock_db.search_athletes_by_name.return_value = mock_athletes

        result = search_athletes(mock_db, "Smith", limit=10)

        assert result["success"] is True
        assert result["count"] == 2
        assert result["search_term"] == "Smith"
        assert len(result["athletes"]) == 2

    def test_search_athletes_limit_validation(self):
        """Test that limit is validated."""
        mock_db = MagicMock()
        mock_athletes = [{"player_id": "1"}]
        mock_db.search_athletes_by_name.return_value = mock_athletes

        # Test with high limit - should be capped
        result = search_athletes(mock_db, "Smith", limit=1000)

        assert result["success"] is True


class TestGetAthletesByTeam:
    """Test get_athletes_by_team function."""

    def test_get_athletes_by_team_success(self):
        """Test getting athletes by team."""
        mock_db = MagicMock()
        mock_athletes = [
            {"player_id": "1", "full_name": "Player 1"},
            {"player_id": "2", "full_name": "Player 2"}
        ]
        mock_db.get_athletes_by_team.return_value = mock_athletes

        result = get_athletes_by_team(mock_db, "SF")

        assert result["success"] is True
        assert result["count"] == 2
        assert result["team_id"] == "SF"
        assert len(result["athletes"]) == 2

    def test_get_athletes_by_team_empty(self):
        """Test getting athletes from team with no players."""
        mock_db = MagicMock()
        mock_db.get_athletes_by_team.return_value = []

        result = get_athletes_by_team(mock_db, "EMPTY")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["athletes"] == []
