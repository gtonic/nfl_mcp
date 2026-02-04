"""
Unit tests for coaching-related MCP tools.

Tests the get_coaching_staff, get_coaching_tree, and get_scheme_classification tools.
"""

import pytest
import httpx
from unittest.mock import patch, AsyncMock, MagicMock
from nfl_mcp import coaching_tools


class TestGetCoachingStaff:
    """Test the get_coaching_staff function."""

    @pytest.mark.asyncio
    async def test_coaching_staff_invalid_team_id_empty(self):
        """Test get_coaching_staff with empty team ID."""
        result = await coaching_tools.get_coaching_staff("")
        assert result["success"] is False
        assert "Team ID is required" in result["error"]

    @pytest.mark.asyncio
    async def test_coaching_staff_invalid_team_id_none(self):
        """Test get_coaching_staff with None team ID."""
        result = await coaching_tools.get_coaching_staff(None)
        assert result["success"] is False
        assert "Team ID is required" in result["error"]

    @pytest.mark.asyncio
    async def test_coaching_staff_successful_response(self):
        """Test successful get_coaching_staff response."""
        
        # Mock ESPN API responses
        mock_coaches_response = {
            "items": [
                {"$ref": "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/2025/teams/12/coaches/1"},
            ]
        }
        
        mock_coach_detail = {
            "id": "1",
            "displayName": "Andy Reid",
            "firstName": "Andy",
            "lastName": "Reid",
            "position": {"name": "Head Coach"},
            "experience": 25,
            "team": {"$ref": "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/12"}
        }
        
        mock_team_detail = {
            "id": "12",
            "abbreviation": "KC",
            "displayName": "Kansas City Chiefs"
        }

        with patch('nfl_mcp.coaching_tools.create_http_client') as mock_client:
            mock_response = MagicMock()
            
            async def mock_get(url, **kwargs):
                if "/coaches" in url and "$ref" not in str(url):
                    mock_response.json.return_value = mock_coaches_response
                elif "coaches/1" in url:
                    mock_response.json.return_value = mock_coach_detail
                elif "/teams/12" in url and "coaches" not in url:
                    mock_response.json.return_value = mock_team_detail
                return mock_response
            
            mock_response.raise_for_status.return_value = None
            mock_cm = MagicMock()
            mock_cm.__aenter__.return_value.get = mock_get
            mock_cm.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_cm

            result = await coaching_tools.get_coaching_staff("KC")
            
            assert result["success"] is True
            assert result["team_id"] == "KC"
            assert result["total_coaches"] >= 0


class TestGetCoachingTree:
    """Test the get_coaching_tree function."""

    @pytest.mark.asyncio
    async def test_coaching_tree_andy_reid(self):
        """Test get_coaching_tree for Andy Reid."""
        result = await coaching_tools.get_coaching_tree("Andy Reid")
        
        assert result["success"] is True
        assert result["found"] is True
        assert result["coach_name"] == "Andy Reid"
        assert "Doug Pederson" in result["proteges"]
        assert "John Harbaugh" in result["proteges"]
        assert "West Coast Offense" in result["scheme_family"]

    @pytest.mark.asyncio
    async def test_coaching_tree_bill_belichick(self):
        """Test get_coaching_tree for Bill Belichick."""
        result = await coaching_tools.get_coaching_tree("Bill Belichick")
        
        assert result["success"] is True
        assert result["found"] is True
        assert result["coach_name"] == "Bill Belichick"
        assert "Nick Saban" in result["proteges"]
        assert "Josh McDaniels" in result["proteges"]

    @pytest.mark.asyncio
    async def test_coaching_tree_kyle_shanahan(self):
        """Test get_coaching_tree for Kyle Shanahan."""
        result = await coaching_tools.get_coaching_tree("Kyle Shanahan")
        
        assert result["success"] is True
        assert result["found"] is True
        assert "Mike McDaniel" in result["proteges"]
        assert "Shanahan Wide Zone" in result["scheme_family"]

    @pytest.mark.asyncio
    async def test_coaching_tree_protege_lookup(self):
        """Test get_coaching_tree for a known protege."""
        result = await coaching_tools.get_coaching_tree("Doug Pederson")
        
        assert result["success"] is True
        assert result["found"] is True
        assert "Andy Reid" in result["mentors"]

    @pytest.mark.asyncio
    async def test_coaching_tree_unknown_coach(self):
        """Test get_coaching_tree for unknown coach."""
        result = await coaching_tools.get_coaching_tree("Unknown Coach")
        
        assert result["success"] is True
        assert result["found"] is False
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_coaching_tree_empty_name(self):
        """Test get_coaching_tree with empty name."""
        result = await coaching_tools.get_coaching_tree("")
        
        assert result["success"] is False
        assert "required" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_coaching_tree_case_insensitive(self):
        """Test get_coaching_tree is case insensitive."""
        result = await coaching_tools.get_coaching_tree("andy reid")
        
        assert result["success"] is True
        assert result["found"] is True
        assert result["coach_name"] == "Andy Reid"


class TestGetSchemeClassification:
    """Test the get_scheme_classification function."""

    @pytest.mark.asyncio
    async def test_scheme_classification_kc(self):
        """Test get_scheme_classification for Kansas City."""
        result = await coaching_tools.get_scheme_classification("KC")
        
        assert result["success"] is True
        assert result["found"] is True
        assert result["team_id"] == "KC"
        assert "West Coast" in result["offensive_scheme"]
        assert "4-3" in result["defensive_scheme"]

    @pytest.mark.asyncio
    async def test_scheme_classification_sf(self):
        """Test get_scheme_classification for San Francisco."""
        result = await coaching_tools.get_scheme_classification("SF")
        
        assert result["success"] is True
        assert result["found"] is True
        assert "Shanahan" in result["offensive_scheme"]
        assert len(result["scheme_notes"]) > 0

    @pytest.mark.asyncio
    async def test_scheme_classification_lar(self):
        """Test get_scheme_classification for LA Rams."""
        result = await coaching_tools.get_scheme_classification("LAR")
        
        assert result["success"] is True
        assert result["found"] is True
        assert "McVay" in result["offensive_scheme"]

    @pytest.mark.asyncio
    async def test_scheme_classification_all_teams(self):
        """Test get_scheme_classification for all 32 teams."""
        teams = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
                 "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
                 "LV", "LAC", "LAR", "MIA", "MIN", "NE", "NO", "NYG",
                 "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WAS"]
        
        for team in teams:
            result = await coaching_tools.get_scheme_classification(team)
            assert result["success"] is True
            assert result["found"] is True
            assert result["team_id"] == team

    @pytest.mark.asyncio
    async def test_scheme_classification_invalid_team(self):
        """Test get_scheme_classification for invalid team."""
        result = await coaching_tools.get_scheme_classification("XXX")
        
        assert result["success"] is True
        assert result["found"] is False
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_scheme_classification_empty_team(self):
        """Test get_scheme_classification with empty team ID."""
        result = await coaching_tools.get_scheme_classification("")
        
        assert result["success"] is False
        assert "required" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_scheme_classification_lowercase(self):
        """Test get_scheme_classification with lowercase team ID."""
        result = await coaching_tools.get_scheme_classification("kc")
        
        assert result["success"] is True
        assert result["found"] is True
        assert result["team_id"] == "KC"


class TestTeamIdMapping:
    """Test the team ID mapping functionality."""

    def test_team_id_map_complete(self):
        """Test that all 32 teams are in the mapping."""
        assert len(coaching_tools.TEAM_ID_MAP) == 32

    def test_espn_team_id_abbreviation(self):
        """Test _get_espn_team_id with abbreviation."""
        assert coaching_tools._get_espn_team_id("KC") == "12"
        assert coaching_tools._get_espn_team_id("NE") == "17"
        assert coaching_tools._get_espn_team_id("SF") == "25"

    def test_espn_team_id_numeric(self):
        """Test _get_espn_team_id with numeric ID."""
        assert coaching_tools._get_espn_team_id("12") == "12"
        assert coaching_tools._get_espn_team_id("17") == "17"

    def test_espn_team_id_lowercase(self):
        """Test _get_espn_team_id with lowercase."""
        assert coaching_tools._get_espn_team_id("kc") == "12"
        assert coaching_tools._get_espn_team_id("ne") == "17"


class TestClassifyCoachRole:
    """Test the coach role classification."""

    def test_classify_head_coach(self):
        """Test classification of head coach."""
        result = coaching_tools._classify_coach_role("Head Coach")
        assert result["category"] == "head_coach"
        assert result["side"] == "both"

    def test_classify_offensive_coordinator(self):
        """Test classification of offensive coordinator."""
        result = coaching_tools._classify_coach_role("Offensive Coordinator")
        assert result["category"] == "coordinator"
        assert result["side"] == "offense"
        assert result["is_coordinator"] is True

    def test_classify_defensive_coordinator(self):
        """Test classification of defensive coordinator."""
        result = coaching_tools._classify_coach_role("Defensive Coordinator")
        assert result["category"] == "coordinator"
        assert result["side"] == "defense"
        assert result["is_coordinator"] is True

    def test_classify_qb_coach(self):
        """Test classification of QB coach."""
        result = coaching_tools._classify_coach_role("Quarterbacks Coach")
        assert result["category"] == "position_coach"
        assert result["side"] == "offense"

    def test_classify_lb_coach(self):
        """Test classification of linebackers coach."""
        result = coaching_tools._classify_coach_role("Linebackers Coach")
        assert result["category"] == "position_coach"
        assert result["side"] == "defense"

    def test_classify_unknown_role(self):
        """Test classification of unknown role."""
        result = coaching_tools._classify_coach_role("Quality Control")
        assert result["category"] == "assistant"
        assert result["side"] == "unknown"


class TestCoachingTrees:
    """Test the coaching trees data structure."""

    def test_coaching_trees_structure(self):
        """Test that coaching trees have required fields."""
        for coach, data in coaching_tools.COACHING_TREES.items():
            assert "mentors" in data
            assert "proteges" in data
            assert "scheme_family" in data
            assert "known_for" in data
            assert isinstance(data["mentors"], list)
            assert isinstance(data["proteges"], list)
            assert isinstance(data["known_for"], list)

    def test_coaching_trees_major_coaches(self):
        """Test that major coaches are included."""
        assert "Andy Reid" in coaching_tools.COACHING_TREES
        assert "Bill Belichick" in coaching_tools.COACHING_TREES
        assert "Kyle Shanahan" in coaching_tools.COACHING_TREES
        assert "Sean McVay" in coaching_tools.COACHING_TREES


class TestTeamSchemes:
    """Test the team schemes data structure."""

    def test_team_schemes_complete(self):
        """Test that all 32 teams have scheme data."""
        assert len(coaching_tools.TEAM_SCHEMES) == 32

    def test_team_schemes_structure(self):
        """Test that scheme data has required fields."""
        for team, data in coaching_tools.TEAM_SCHEMES.items():
            assert "offense" in data
            assert "defense" in data
            assert isinstance(data["offense"], str)
            assert isinstance(data["defense"], str)
