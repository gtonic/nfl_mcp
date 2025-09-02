"""
Tests for enhanced input validation functions.
"""

import time
import pytest
from nfl_mcp.config import (
    validate_string_input,
    validate_numeric_input, 
    validate_limit,
    sanitize_content,
    validate_url_enhanced,
    is_valid_url,
    check_rate_limit,
    get_rate_limit_status
)


class TestStringValidation:
    """Test string input validation and sanitization."""
    
    def test_valid_general_string(self):
        """Test valid general string input."""
        result = validate_string_input("Hello World", "general")
        assert result == "Hello World"
    
    def test_valid_team_id(self):
        """Test valid NFL team ID."""
        result = validate_string_input("KC", "team_id")
        assert result == "KC"
        
        result = validate_string_input("NE", "team_id")
        assert result == "NE"
    
    def test_valid_league_id(self):
        """Test valid Sleeper league ID."""
        result = validate_string_input("123456789", "league_id")
        assert result == "123456789"
    
    def test_valid_trend_type(self):
        """Test valid trend types."""
        result = validate_string_input("add", "trend_type")
        assert result == "add"
        
        result = validate_string_input("drop", "trend_type")
        assert result == "drop"
    
    def test_valid_athlete_name(self):
        """Test valid athlete name."""
        result = validate_string_input("Patrick Mahomes", "athlete_name")
        assert result == "Patrick Mahomes"
        
        result = validate_string_input("D'Andre Swift", "athlete_name")
        assert result == "D&#x27;Andre Swift"  # HTML escaped
    
    def test_html_escaping(self):
        """Test HTML escaping works correctly."""
        # For general content, it should detect script tags as dangerous
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("<script>alert('xss')</script>", "general")
        
        # But basic HTML escaping should work for safe content
        result = validate_string_input("Hello & World", "general")
        assert "&amp;" in result
    
    def test_sql_injection_detection(self):
        """Test SQL injection pattern detection."""
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("' OR 1=1 --", "general")
        
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("UNION SELECT * FROM users", "general")
        
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("admin'/**/AND/**/1=1", "general")
    
    def test_xss_injection_detection(self):
        """Test XSS injection pattern detection."""
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("<script>alert('xss')</script>", "general")
        
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("javascript:alert(1)", "general")
        
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("onload=alert(1)", "general")
    
    def test_command_injection_detection(self):
        """Test command injection pattern detection."""
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("test; rm -rf /", "general")
        
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("$(cat /etc/passwd)", "general")
        
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("test && curl evil.com", "general")
    
    def test_path_traversal_detection(self):
        """Test path traversal pattern detection."""
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("../../../etc/passwd", "general")
        
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("/etc/passwd", "general")
    
    def test_length_validation(self):
        """Test string length validation."""
        with pytest.raises(ValueError, match="exceeds maximum"):
            validate_string_input("a" * 1001, "general", max_length=1000)
    
    def test_empty_string_validation(self):
        """Test empty string validation."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_string_input("", "general", required=True)
        
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_string_input("   ", "general", required=True)
        
        # Should work when not required
        result = validate_string_input("", "general", required=False)
        assert result == ""
    
    def test_none_value_handling(self):
        """Test None value handling."""
        with pytest.raises(ValueError, match="cannot be None"):
            validate_string_input(None, "general", required=True)
        
        # Should work when not required
        result = validate_string_input(None, "general", required=False)
        assert result == ""
    
    def test_invalid_team_id_pattern(self):
        """Test invalid team ID patterns."""
        with pytest.raises(ValueError, match="does not match required pattern"):
            validate_string_input("INVALID", "team_id")
        
        with pytest.raises(ValueError, match="does not match required pattern"):
            validate_string_input("KC123", "team_id")
    
    def test_invalid_league_id_pattern(self):
        """Test invalid league ID patterns."""
        with pytest.raises(ValueError, match="does not match required pattern"):
            validate_string_input("abc123", "league_id")
        
        with pytest.raises(ValueError, match="does not match required pattern"):
            validate_string_input("123abc", "league_id")
    
    def test_invalid_trend_type_pattern(self):
        """Test invalid trend type patterns."""
        with pytest.raises(ValueError, match="does not match required pattern"):
            validate_string_input("invalid", "trend_type")
        
        with pytest.raises(ValueError, match="does not match required pattern"):
            validate_string_input("ADD", "trend_type")  # Case sensitive


class TestNumericValidation:
    """Test numeric input validation."""
    
    def test_valid_integer(self):
        """Test valid integer input."""
        result = validate_numeric_input(42, min_val=1, max_val=100)
        assert result == 42
    
    def test_string_to_integer_conversion(self):
        """Test string to integer conversion."""
        result = validate_numeric_input("42", min_val=1, max_val=100)
        assert result == 42
    
    def test_range_validation(self):
        """Test range validation."""
        # Below minimum
        with pytest.raises(ValueError, match="below minimum"):
            validate_numeric_input(0, min_val=1, max_val=100)
        
        # Above maximum
        with pytest.raises(ValueError, match="exceeds maximum"):
            validate_numeric_input(101, min_val=1, max_val=100)
    
    def test_range_validation_with_default(self):
        """Test range validation with default values."""
        # Below minimum - should return minimum when default provided
        result = validate_numeric_input(0, min_val=1, max_val=100, default=50)
        assert result == 50
        
        # Above maximum - should return default
        result = validate_numeric_input(101, min_val=1, max_val=100, default=50)
        assert result == 50
    
    def test_none_value_handling(self):
        """Test None value handling."""
        # With default
        result = validate_numeric_input(None, min_val=1, max_val=100, default=50)
        assert result == 50
        
        # Without default but not required
        result = validate_numeric_input(None, min_val=1, max_val=100, required=False)
        assert result == 0
        
        # Required but None
        with pytest.raises(ValueError, match="cannot be None"):
            validate_numeric_input(None, min_val=1, max_val=100, required=True)
    
    def test_invalid_conversion(self):
        """Test invalid type conversion."""
        with pytest.raises(ValueError, match="Cannot convert"):
            validate_numeric_input("not_a_number", min_val=1, max_val=100)
        
        with pytest.raises(ValueError, match="Cannot convert"):
            validate_numeric_input([], min_val=1, max_val=100)
    
    def test_dangerous_string_numbers(self):
        """Test dangerous patterns in string numbers."""
        # The function should fail conversion, not detect invalid characters
        with pytest.raises(ValueError, match="Cannot convert"):
            validate_numeric_input("42; rm -rf /", min_val=1, max_val=100)
        
        with pytest.raises(ValueError, match="Cannot convert"):
            validate_numeric_input("$(cat /etc/passwd)", min_val=1, max_val=100)


class TestContentSanitization:
    """Test content sanitization."""
    
    def test_basic_sanitization(self):
        """Test basic HTML escaping."""
        result = sanitize_content("<h1>Title</h1>")
        assert "&lt;h1&gt;" in result
        assert "<h1>" not in result
    
    def test_script_removal(self):
        """Test script tag removal."""
        content = "Safe content <script>alert('xss')</script> more content"
        result = sanitize_content(content)
        assert "script" not in result.lower()
        assert "Safe content" in result
        assert "more content" in result
    
    def test_javascript_removal(self):
        """Test javascript: URL removal."""
        content = "Click <a href='javascript:alert(1)'>here</a>"
        result = sanitize_content(content)
        assert "javascript:" not in result
    
    def test_whitespace_normalization(self):
        """Test whitespace normalization."""
        content = "Line 1\n\n\nLine 2\t\t\tLine 3"
        result = sanitize_content(content)
        assert result == "Line 1 Line 2 Line 3"
    
    def test_length_truncation(self):
        """Test content length truncation."""
        content = "a" * 100
        result = sanitize_content(content, max_length=50)
        assert len(result) == 53  # 50 + "..."
        assert result.endswith("...")
    
    def test_empty_content(self):
        """Test empty content handling."""
        assert sanitize_content("") == ""
        assert sanitize_content(None) == ""
        assert sanitize_content("   ") == ""


class TestEnhancedUrlValidation:
    """Test enhanced URL validation."""
    
    def test_basic_valid_urls(self):
        """Test basic valid URLs."""
        assert validate_url_enhanced("https://example.com") is True
        assert validate_url_enhanced("http://example.com") is True
        assert validate_url_enhanced("https://api.sleeper.app/v1/league/123") is True
    
    def test_invalid_schemes(self):
        """Test invalid URL schemes."""
        assert validate_url_enhanced("ftp://example.com") is False
        assert validate_url_enhanced("file:///etc/passwd") is False
        assert validate_url_enhanced("data:text/html,<script>") is False
    
    def test_dangerous_patterns_in_urls(self):
        """Test dangerous patterns in URLs."""
        assert validate_url_enhanced("https://example.com/page?id=1' OR 1=1--") is False
        assert validate_url_enhanced("https://example.com/<script>alert(1)</script>") is False
        assert validate_url_enhanced("https://example.com/page;rm -rf /") is False
    
    def test_local_network_blocking(self):
        """Test blocking of local/private network URLs."""
        assert validate_url_enhanced("http://localhost/test") is False
        assert validate_url_enhanced("http://127.0.0.1/test") is False
        assert validate_url_enhanced("http://0.0.0.0/test") is False
        assert validate_url_enhanced("http://192.168.1.1/test") is False
        assert validate_url_enhanced("http://10.0.0.1/test") is False
        assert validate_url_enhanced("http://172.16.0.1/test") is False
    
    def test_domain_restrictions(self):
        """Test domain restrictions."""
        allowed_domains = ["example.com", "api.sleeper.app"]
        
        assert validate_url_enhanced("https://example.com/test", 
                                   allowed_domains=allowed_domains) is True
        assert validate_url_enhanced("https://api.sleeper.app/v1/test", 
                                   allowed_domains=allowed_domains) is True
        assert validate_url_enhanced("https://evil.com/test", 
                                   allowed_domains=allowed_domains) is False
    
    def test_malformed_urls(self):
        """Test malformed URLs."""
        assert validate_url_enhanced("not_a_url") is False
        assert validate_url_enhanced("") is False
        assert validate_url_enhanced(None) is False
        assert validate_url_enhanced(123) is False


class TestValidateLimit:
    """Test the validate_limit function for backward compatibility."""
    
    def test_basic_limit_validation(self):
        """Test basic limit validation."""
        result = validate_limit(5, min_val=1, max_val=10)
        assert result == 5
    
    def test_limit_clamping(self):
        """Test limit value clamping."""
        # Below minimum
        result = validate_limit(0, min_val=1, max_val=10, default=5)
        assert result == 5
        
        # Above maximum
        result = validate_limit(15, min_val=1, max_val=10, default=5)
        assert result == 5
    
    def test_none_handling(self):
        """Test None value handling."""
        result = validate_limit(None, min_val=1, max_val=10, default=5)
        assert result == 5
        
        result = validate_limit(None, min_val=1, max_val=10)
        assert result == 1  # Should use min_val when no default


class TestRateLimiting:
    """Test rate limiting functionality."""
    
    def test_basic_rate_limiting(self):
        """Test basic rate limiting functionality."""
        identifier = "test_user_1"
        
        # First request should pass
        assert check_rate_limit(identifier, limit=2, window_seconds=60) is True
        
        # Second request should pass
        assert check_rate_limit(identifier, limit=2, window_seconds=60) is True
        
        # Third request should fail
        assert check_rate_limit(identifier, limit=2, window_seconds=60) is False
    
    def test_rate_limit_window_expiry(self):
        """Test that rate limits reset after time window."""
        identifier = "test_user_2"
        
        # Make requests up to limit
        assert check_rate_limit(identifier, limit=1, window_seconds=1) is True
        assert check_rate_limit(identifier, limit=1, window_seconds=1) is False
        
        # Wait for window to expire
        time.sleep(1.1)
        
        # Should be allowed again
        assert check_rate_limit(identifier, limit=1, window_seconds=1) is True
    
    def test_rate_limit_per_identifier(self):
        """Test that rate limits are per identifier."""
        user1 = "test_user_3"
        user2 = "test_user_4"
        
        # Each user should have their own limit
        assert check_rate_limit(user1, limit=1, window_seconds=60) is True
        assert check_rate_limit(user2, limit=1, window_seconds=60) is True
        
        # Both should be at limit now
        assert check_rate_limit(user1, limit=1, window_seconds=60) is False
        assert check_rate_limit(user2, limit=1, window_seconds=60) is False
    
    def test_rate_limit_status(self):
        """Test rate limit status reporting."""
        identifier = "test_user_5"
        limit = 3
        
        # Initial status
        status = get_rate_limit_status(identifier, limit, window_seconds=60)
        assert status["limit"] == limit
        assert status["remaining"] == limit
        assert status["retry_after"] == 0
        
        # After one request
        check_rate_limit(identifier, limit=limit, window_seconds=60)
        status = get_rate_limit_status(identifier, limit, window_seconds=60)
        assert status["remaining"] == limit - 1
        
        # After hitting limit
        check_rate_limit(identifier, limit=limit, window_seconds=60)
        check_rate_limit(identifier, limit=limit, window_seconds=60)
        status = get_rate_limit_status(identifier, limit, window_seconds=60)
        assert status["remaining"] == 0
        assert status["retry_after"] > 0