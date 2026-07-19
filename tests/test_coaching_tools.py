"""Tests for coaching_tools module."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nfl_mcp.coaching_tools import (
    _get_espn_team_id,
    _classify_coach_role,
    get_coaching_staff,
    get_all_coaching_staffs,
    get_coaching_tree,
    get_scheme_classification,
    COACHING_TREES,
    TEAM_SCHEMES,
)


class TestGetEspnTeamId:
    """Test _get_espn_team_id function."""

    def test_known_abbreviation(self):
        """Test known team abbreviation."""
        result = _get_espn_team_id("KC")
        assert result == "12"

    def test_case_insensitive(self):
        """Test case insensitivity."""
        assert _get_espn_team_id("kc") == "12"
        assert _get_espn_team_id("Kc") == "12"

    def test_numeric_id_passthrough(self):
        """Test that numeric IDs pass through unchanged."""
        result = _get_espn_team_id("25")
        assert result == "25"

    def test_unknown_team_passthrough(self):
        """Test that unknown teams pass through unchanged."""
        result = _get_espn_team_id("UNKNOWN")
        assert result == "UNKNOWN"


class TestClassifyCoachRole:
    """Test _classify_coach_role function."""

    def test_head_coach(self):
        """Test head coach classification."""
        result = _classify_coach_role("Head Coach")
        assert result["category"] == "head_coach"
        assert result["side"] == "both"
        assert result["is_coordinator"] is False

    def test_offensive_coordinator(self):
        """Test OC classification."""
        result = _classify_coach_role("Offensive Coordinator")
        assert result["category"] == "coordinator"
        assert result["side"] == "offense"
        assert result["is_coordinator"] is True

    def test_defensive_coordinator(self):
        """Test DC classification."""
        result = _classify_coach_role("Defensive Coordinator")
        assert result["category"] == "coordinator"
        assert result["side"] == "defense"
        assert result["is_coordinator"] is True

    def test_position_coach_qb(self):
        """Test QB position coach classification."""
        result = _classify_coach_role("Quarterbacks Coach")
        assert result["category"] == "position_coach"
        assert result["side"] == "offense"

    def test_position_coach_wr(self):
        """Test WR position coach classification."""
        result = _classify_coach_role("Wide Receivers Coach")
        assert result["category"] == "position_coach"
        assert result["side"] == "offense"

    def test_assistant_unknown_role(self):
        """Test assistant classification for unknown role."""
        result = _classify_coach_role("Video Coach")
        assert result["category"] == "assistant"
        assert result["side"] == "unknown"


class TestGetCoachingTree:
    """Test get_coaching_tree function."""

    @pytest.mark.asyncio
    async def test_known_coach(self):
        """Test lookup for known coach."""
        result = await get_coaching_tree("Andy Reid")
        assert result["success"] is True
        assert result["found"] is True
        assert result["coach_name"] == "Andy Reid"
        assert len(result["mentors"]) > 0
        assert len(result["proteges"]) > 0

    @pytest.mark.asyncio
    async def test_unknown_coach(self):
        """Test lookup for unknown coach."""
        result = await get_coaching_tree("Nobody Coach")
        assert result["success"] is True
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_coach_as_protege(self):
        """Test looking up a coach as a protege."""
        result = await get_coaching_tree("Doug Pederson")
        assert result["success"] is True
        assert result["found"] is True

    @pytest.mark.asyncio
    async def test_empty_coach_name(self):
        """Test with empty coach name."""
        result = await get_coaching_tree("")
        assert result["success"] is False


class TestGetSchemeClassification:
    """Test get_scheme_classification function."""

    @pytest.mark.asyncio
    async def test_known_team(self):
        """Test classification for known team."""
        result = await get_scheme_classification("KC")
        assert result["success"] is True
        assert result["found"] is True
        assert "offensive_scheme" in result
        assert "defensive_scheme" in result

    @pytest.mark.asyncio
    async def test_unknown_team(self):
        """Test classification for unknown team."""
        result = await get_scheme_classification("UNKNOWN")
        assert result["success"] is True
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_empty_team_id(self):
        """Test with empty team ID."""
        result = await get_scheme_classification("")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_scheme_notes_generated(self):
        """Test that scheme notes are generated."""
        result = await get_scheme_classification("SF")
        assert result["success"] is True
        assert "scheme_notes" in result
        assert len(result["scheme_notes"]) > 0


class TestGetAllCoachingStaffs:
    """Test get_all_coaching_staffs async function."""

    @pytest.mark.asyncio
    async def test_all_coaching_staffs_success(self):
        """Test successful retrieval of all coaching staffs."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {
                    "$ref": "https://example.com/team/1",
                    "abbreviation": "KC",
                    "displayName": "Kansas City Chiefs"
                }
            ]
        }
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.coaching_tools.create_http_client', return_value=mock_client):
            result = await get_all_coaching_staffs()
            
            assert result["success"] is True
            assert "teams" in result
            assert "total_teams" in result

    @pytest.mark.asyncio
    async def test_all_coaching_staffs_empty(self):
        """Test with no teams."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"items": []}
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.coaching_tools.create_http_client', return_value=mock_client):
            result = await get_all_coaching_staffs()
            
            assert result["success"] is True
            assert result["total_teams"] == 0


class TestGetCoachingStaff:
    """Test get_coaching_staff async function."""

    @pytest.mark.asyncio
    async def test_coaching_staff_success(self):
        """Test successful coaching staff fetch."""
        # Mock team data response
        team_mock = MagicMock()
        team_mock.status_code = 200
        team_mock.json.return_value = {
            "abbreviation": "KC",
            "displayName": "Kansas City Chiefs"
        }
        
        # Mock coaches response
        coaches_mock = MagicMock()
        coaches_mock.status_code = 200
        coaches_mock.json.return_value = {
            "items": [
                {"$ref": "https://example.com/coach/1"}
            ]
        }
        
        # Mock coach details
        coach_mock = MagicMock()
        coach_mock.status_code = 200
        coach_mock.json.return_value = {
            "displayName": "Andy Reid",
            "firstName": "Andy",
            "lastName": "Reid",
            "position": {"name": "Head Coach"}
        }
        
        mock_client = AsyncMock()
        mock_client.get.side_effect = [coaches_mock, coach_mock, team_mock]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.coaching_tools.create_http_client', return_value=mock_client):
            result = await get_coaching_staff("KC")
            
            assert result["success"] is True
            assert "coaches" in result

    @pytest.mark.asyncio
    async def test_coaching_staff_404(self):
        """Test coaching staff fetch with 404 response."""
        import httpx
        
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=mock_response
        )
        
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch('nfl_mcp.coaching_tools.create_http_client', return_value=mock_client):
            result = await get_coaching_staff("INVALID")
            
            assert result["success"] is True  # Handled gracefully
            assert "message" in result

    @pytest.mark.asyncio
    async def test_coaching_staff_invalid_team_id(self):
        """Test with invalid team ID."""
        result = await get_coaching_staff("")
        assert result["success"] is False
        assert "validation" in result.get("error_type", "").lower()


class TestConstants:
    """Test COACHING_TREES and TEAM_SCHEMES constants."""

    def test_coaching_trees_have_required_keys(self):
        """Test coaching trees have required structure."""
        for coach_name, tree_data in COACHING_TREES.items():
            assert "mentors" in tree_data
            assert "proteges" in tree_data
            assert "scheme_family" in tree_data
            assert "known_for" in tree_data

    def test_team_schemes_have_required_keys(self):
        """Test team schemes have required structure."""
        for team_id, scheme_data in TEAM_SCHEMES.items():
            assert "offense" in scheme_data
            assert "defense" in scheme_data
