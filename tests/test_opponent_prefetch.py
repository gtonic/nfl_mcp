"""
Tests for opponent prefetch functionality to ensure schedules are fetched for multiple weeks.
"""
import pytest
from nfl_mcp.server import PREFETCH_SCHEDULE_WEEKS


def test_prefetch_schedule_weeks_configuration():
    """Test that PREFETCH_SCHEDULE_WEEKS configuration is properly loaded."""
    # The configuration should default to 4 weeks
    assert PREFETCH_SCHEDULE_WEEKS == 4, \
        f"Expected PREFETCH_SCHEDULE_WEEKS to be 4, got {PREFETCH_SCHEDULE_WEEKS}"


def test_schedule_week_range_logic():
    """Test the logic for calculating which weeks to fetch."""
    # Simulate the logic in the prefetch loop
    
    # Test case 1: Week 6, should fetch 6, 7, 8, 9
    week = 6
    schedule_weeks_to_fetch = list(range(week, min(week + PREFETCH_SCHEDULE_WEEKS, 19)))
    assert schedule_weeks_to_fetch == [6, 7, 8, 9], \
        f"Expected [6, 7, 8, 9], got {schedule_weeks_to_fetch}"
    
    # Test case 2: Week 17, should fetch 17, 18 (stops at 18)
    week = 17
    schedule_weeks_to_fetch = list(range(week, min(week + PREFETCH_SCHEDULE_WEEKS, 19)))
    assert schedule_weeks_to_fetch == [17, 18], \
        f"Expected [17, 18], got {schedule_weeks_to_fetch}"
    
    # Test case 3: Week 18, should fetch only 18
    week = 18
    schedule_weeks_to_fetch = list(range(week, min(week + PREFETCH_SCHEDULE_WEEKS, 19)))
    assert schedule_weeks_to_fetch == [18], \
        f"Expected [18], got {schedule_weeks_to_fetch}"
    
    # Test case 4: Week 1, should fetch 1, 2, 3, 4
    week = 1
    schedule_weeks_to_fetch = list(range(week, min(week + PREFETCH_SCHEDULE_WEEKS, 19)))
    assert schedule_weeks_to_fetch == [1, 2, 3, 4], \
        f"Expected [1, 2, 3, 4], got {schedule_weeks_to_fetch}"

