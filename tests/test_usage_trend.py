"""Tests for usage trend calculation."""
import pytest
from nfl_mcp.sleeper_tools import _calculate_usage_trend


class TestUsageTrend:
    """Test usage trend calculation functionality."""
    
    def test_trend_up(self):
        """Test upward trend detection."""
        # Most recent week has 20 targets, previous weeks had ~10
        weekly_data = [
            {"week": 5, "targets": 20, "routes": 30},  # Most recent
            {"week": 4, "targets": 10, "routes": 25},
            {"week": 3, "targets": 12, "routes": 28}
        ]
        
        trend = _calculate_usage_trend(weekly_data, "targets")
        assert trend == "up", "Should detect upward trend when recent value is 15% higher"
    
    def test_trend_down(self):
        """Test downward trend detection."""
        # Most recent week has 5 targets, previous weeks had ~12
        weekly_data = [
            {"week": 5, "targets": 5, "routes": 15},  # Most recent
            {"week": 4, "targets": 12, "routes": 25},
            {"week": 3, "targets": 10, "routes": 28}
        ]
        
        trend = _calculate_usage_trend(weekly_data, "targets")
        assert trend == "down", "Should detect downward trend when recent value is 15% lower"
    
    def test_trend_flat(self):
        """Test flat/stable trend detection."""
        # Values are relatively stable
        weekly_data = [
            {"week": 5, "targets": 10, "routes": 25},  # Most recent
            {"week": 4, "targets": 11, "routes": 26},
            {"week": 3, "targets": 9, "routes": 24}
        ]
        
        trend = _calculate_usage_trend(weekly_data, "targets")
        assert trend == "flat", "Should detect flat trend when change is less than 15%"
    
    def test_trend_with_none_values(self):
        """Test trend calculation with some None values."""
        weekly_data = [
            {"week": 5, "targets": 15, "routes": None},  # Most recent
            {"week": 4, "targets": 10, "routes": 25},
            {"week": 3, "targets": None, "routes": 28}
        ]
        
        trend = _calculate_usage_trend(weekly_data, "targets")
        assert trend == "up", "Should handle None values and still calculate trend"
    
    def test_trend_insufficient_data(self):
        """Test trend calculation with insufficient data."""
        # Only one week of data
        weekly_data = [
            {"week": 5, "targets": 10, "routes": 25}
        ]
        
        trend = _calculate_usage_trend(weekly_data, "targets")
        assert trend is None, "Should return None with insufficient data"
    
    def test_trend_missing_metric(self):
        """Test trend calculation when metric is missing."""
        weekly_data = [
            {"week": 5, "targets": 10},
            {"week": 4, "targets": 12},
            {"week": 3, "targets": 11}
        ]
        
        trend = _calculate_usage_trend(weekly_data, "routes")
        assert trend is None, "Should return None when metric is missing"
    
    def test_trend_zero_baseline(self):
        """Test trend when baseline is zero."""
        weekly_data = [
            {"week": 5, "targets": 5, "routes": 25},  # Most recent
            {"week": 4, "targets": 0, "routes": 0},
            {"week": 3, "targets": 0, "routes": 0}
        ]
        
        trend = _calculate_usage_trend(weekly_data, "targets")
        assert trend == "up", "Should detect upward trend from zero baseline"
    
    def test_trend_routes_metric(self):
        """Test trend calculation with routes metric."""
        weekly_data = [
            {"week": 5, "targets": 10, "routes": 35},  # Most recent
            {"week": 4, "targets": 10, "routes": 25},
            {"week": 3, "targets": 10, "routes": 20}
        ]
        
        trend = _calculate_usage_trend(weekly_data, "routes")
        assert trend == "up", "Should calculate trend for routes metric"
    
    def test_trend_snap_share_metric(self):
        """Test trend calculation with snap_share metric."""
        weekly_data = [
            {"week": 5, "snap_share": 50.0},  # Most recent, dropping
            {"week": 4, "snap_share": 75.0},
            {"week": 3, "snap_share": 80.0}
        ]
        
        trend = _calculate_usage_trend(weekly_data, "snap_share")
        assert trend == "down", "Should calculate trend for snap_share metric"
