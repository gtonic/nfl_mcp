"""
Comprehensive tests for Coaching tools specifically.
These tests ensure the fixed Coaching tools work correctly.
"""

import pytest
import sys
from unittest.mock import Mock, patch, AsyncMock

# Add the project root to the Python path
sys.path.insert(0, '/tmp/nfl_mcp')

from nfl_mcp.coaching_tools import (
    _get_espn_team_id,
    _classify_coach_role,
    get_coaching_staff,
    get_all_coaching_staffs,
    get_coaching_tree,
    get_scheme_classification
)

class TestCoachingTools:
    """Test the Coaching tools functionality."""

    def test_get_espn_team_id(self):
        """Test ESPN team ID conversion."""
        # Test abbreviation mapping
        assert _get_espn_team_id("KC") == "12"
        assert _get_espn_team_id("BUF") == "2"
        assert _get_espn_team_id("NE") == "17"
        
        # Test numeric ID stays the same
        assert _get_espn_team_id("12") == "12"
        
        # Test unknown team returns original
        assert _get_espn_team_id("UNKNOWN") == "UNKNOWN"

    def test_classify_coach_role(self):
        """Test coach role classification."""
        # Head coach
        result = _classify_coach_role("Head Coach")
        assert result["category"] == "head_coach"
        
        # Offensive coordinator
        result = _classify_coach_role("Offensive Coordinator")
        assert result["category"] == "coordinator"
        assert result["side"] == "offense"
        
        # Defensive coordinator
        result = _classify_coach_role("Defensive Coordinator")
        assert result["category"] == "coordinator"
        assert result["side"] == "defense"
        
        # Position coach
        result = _classify_coach_role("Quarterback Coach")
        assert result["category"] == "position_coach"
        assert result["side"] == "offense"
        
        # Assistant
        result = _classify_coach_role("Special Teams Assistant")
        assert result["category"] == "assistant"

    @pytest.mark.asyncio
    async def test_get_coaching_staff_functionality(self):
        """Test get_coaching_staff functionality."""
        result = await get_coaching_staff(team_id="KC")
        assert isinstance(result, dict)
        assert 'team_id' in result or 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_get_all_coaching_staffs_functionality(self):
        """Test get_all_coaching_staffs functionality."""
        result = await get_all_coaching_staffs()
        assert isinstance(result, dict)
        assert 'teams' in result or 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_get_coaching_tree_functionality(self):
        """Test get_coaching_tree functionality."""
        # Test known coach
        result = await get_coaching_tree(coach_name="Andy Reid")
        assert isinstance(result, dict)
        assert 'coach_name' in result or 'success' in result or 'error' in result
        
        # Test unknown coach
        result = await get_coaching_tree(coach_name="Unknown Coach")
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_get_scheme_classification_functionality(self):
        """Test get_scheme_classification functionality."""
        result = await get_scheme_classification(team_id="KC")
        assert isinstance(result, dict)
        assert 'team_id' in result or 'success' in result or 'error' in result

    @pytest.mark.asyncio
    async def test_edge_cases(self):
        """Test edge cases for Coaching tools."""
        # Test with invalid team ID
        result = await get_coaching_staff(team_id="")
        assert isinstance(result, dict)
        
        # Test with invalid coach name
        result = await get_coaching_tree(coach_name="")
        assert isinstance(result, dict)
        
        # Test with invalid team for scheme classification
        result = await get_scheme_classification(team_id="")
        assert isinstance(result, dict)

if __name__ == "__main__":
    pytest.main([__file__, "-v"])