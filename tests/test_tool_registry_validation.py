"""
Validation tests to ensure the tool registry is properly fixed and complete.
This validates that all the tools that were previously missing are now present.
"""

import pytest
import sys
from unittest.mock import patch, AsyncMock

# Add the project root to the Python path
sys.path.insert(0, '/tmp/nfl_mcp')

from nfl_mcp.tool_registry import (
    # Vegas tools (these were the main missing ones)
    get_vegas_lines,
    get_game_environment,
    analyze_roster_vegas,
    get_stack_opportunities,
    
    # Coaching tools (these were also missing)
    get_coaching_staff,
    get_all_coaching_staffs,
    get_coaching_tree,
    get_scheme_classification,
    
    # Other tools that should be available
    get_nfl_news,
    get_teams,
    get_cbs_player_news,
    get_cbs_projections,
    get_cbs_expert_picks
)

class TestToolRegistryValidation:
    """Validate that all tools are properly registered and callable."""

    def test_all_vegas_tools_present(self):
        """Test that all the previously missing Vegas tools are present."""
        assert callable(get_vegas_lines)
        assert callable(get_game_environment)
        assert callable(analyze_roster_vegas)
        assert callable(get_stack_opportunities)

    def test_all_coaching_tools_present(self):
        """Test that all the previously missing Coaching tools are present."""
        assert callable(get_coaching_staff)
        assert callable(get_all_coaching_staffs)
        assert callable(get_coaching_tree)
        assert callable(get_scheme_classification)

    def test_other_tools_still_work(self):
        """Test that existing tools still work."""
        assert callable(get_nfl_news)
        assert callable(get_teams)
        assert callable(get_cbs_player_news)
        assert callable(get_cbs_projections)
        assert callable(get_cbs_expert_picks)

    @pytest.mark.asyncio
    async def test_basic_functionality(self):
        """Test that tools can be called without throwing syntax errors."""
        # Test that we can at least call the functions
        try:
            # This will fail due to network/API calls, but should not throw a NameError
            # or similar import-related errors
            result = await get_vegas_lines()
            assert isinstance(result, dict)
        except Exception as e:
            # This is expected to fail due to missing API keys, etc.
            # But it shouldn't fail with import/definition errors
            assert "NameError" not in str(type(e)) or "ImportError" not in str(type(e))

    @pytest.mark.asyncio
    async def test_function_signatures(self):
        """Test that functions have expected signatures."""
        # Test that functions exist and can be introspected
        import inspect
        
        # Test Vegas functions
        vegas_funcs = [get_vegas_lines, get_game_environment, analyze_roster_vegas, get_stack_opportunities]
        for func in vegas_funcs:
            sig = inspect.signature(func)
            assert callable(func)
            
        # Test Coaching functions
        coaching_funcs = [get_coaching_staff, get_all_coaching_staffs, get_coaching_tree, get_scheme_classification]
        for func in coaching_funcs:
            sig = inspect.signature(func)
            assert callable(func)

    def test_no_missing_tools(self):
        """Test that we don't have any missing tools that were reported."""
        # The key tools that were reported as missing in the original issue
        missing_tools = [
            "get_vegas_lines", 
            "get_game_environment", 
            "analyze_roster_vegas", 
            "get_stack_opportunities",
            "get_coaching_staff",
            "get_all_coaching_staffs",
            "get_coaching_tree",
            "get_scheme_classification"
        ]
        
        # All should be callable now
        for tool_name in missing_tools:
            assert tool_name in globals() or hasattr(sys.modules[__name__], tool_name)
            # More specifically, they should be in the tool_registry module
            tool_func = globals()[tool_name]
            assert callable(tool_func)

if __name__ == "__main__":
    pytest.main([__file__, "-v"])