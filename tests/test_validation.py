"""
Tests for enhanced input validation functions.
"""

from unittest.mock import patch

import pytest

from nfl_mcp import input_warnings
from nfl_mcp.config import (
    is_safe_public_url,
    sanitize_content,
    validate_limit,
    validate_numeric_input,
    validate_string_input,
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

        # Returned as typed: the value is a lookup key, and an escaped
        # apostrophe matches no player.
        result = validate_string_input("D'Andre Swift", "athlete_name")
        assert result == "D'Andre Swift"

    def test_apostrophe_names_survive_as_lookup_keys(self):
        for name in ("Ja'Marr Chase", "De'Von Achane", "D'Andre Swift"):
            assert validate_string_input(name, "player_name") == name

    def test_dangerous_input_is_rejected_not_escaped(self):
        """Validation rejects injection; it does not rewrite safe content."""
        with pytest.raises(ValueError, match="dangerous pattern"):
            validate_string_input("<script>alert('xss')</script>", "general")

        assert validate_string_input("  Hello & World ", "general") == "Hello & World"

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
        """Out of range with a default: clamped to the BOUND, never the default."""
        assert validate_numeric_input(0, min_val=1, max_val=100, default=50) == 1
        assert validate_numeric_input(101, min_val=1, max_val=100, default=50) == 100

    def test_clamp_is_reported_as_input_warning(self):
        """The correction is collected for the tool response, not silent."""
        token = input_warnings.begin_collection()
        try:
            assert validate_numeric_input(500, min_val=1, max_val=100, default=25) == 100
        finally:
            collected = input_warnings.end_collection(token)
        assert collected == ["Value 500 exceeds maximum 100; clamped to 100"]

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


class TestValidateLimit:
    """Test the validate_limit function for backward compatibility."""

    def test_basic_limit_validation(self):
        """Test basic limit validation."""
        result = validate_limit(5, min_val=1, max_val=10)
        assert result == 5

    def test_limit_clamping(self):
        """Out-of-range limits clamp to the violated bound, not the default."""
        # Below minimum
        assert validate_limit(0, min_val=1, max_val=10, default=5) == 1

        # Above maximum
        assert validate_limit(15, min_val=1, max_val=10, default=5) == 10

    def test_unparseable_limit_uses_default_with_warning(self):
        token = input_warnings.begin_collection()
        try:
            assert validate_limit("lots", min_val=1, max_val=10, default=5) == 5
        finally:
            collected = input_warnings.end_collection(token)
        assert collected and "used default 5" in collected[0]

    def test_none_handling(self):
        """Test None value handling."""
        result = validate_limit(None, min_val=1, max_val=10, default=5)
        assert result == 5

        result = validate_limit(None, min_val=1, max_val=10)
        assert result == 1  # Should use min_val when no default


class TestIsSafePublicUrl:
    """DNS-resolving SSRF guard used before fetching user-supplied URLs."""

    def test_invalid_scheme_rejected(self):
        ok, reason = is_safe_public_url("file:///etc/passwd")
        assert ok is False
        assert "http://" in reason

    def test_no_host_rejected(self):
        ok, _reason = is_safe_public_url("http:///nohost")
        assert ok is False

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
    ])
    def test_blocks_ip_literals(self, url):
        ok, reason = is_safe_public_url(url)
        assert ok is False
        assert "Blocked non-public address" in reason

    def test_allows_public_ip_literal(self):
        ok, reason = is_safe_public_url("http://93.184.216.34/")
        assert ok is True
        assert reason is None

    def test_blocks_host_resolving_to_private(self):
        with patch("nfl_mcp.config.resolve_host_addresses", return_value=["10.0.0.5"]):
            ok, reason = is_safe_public_url("http://internal.example.test/")
        assert ok is False
        assert "Blocked non-public address" in reason

    def test_allows_host_resolving_to_public(self):
        with patch("nfl_mcp.config.resolve_host_addresses", return_value=["93.184.216.34"]):
            ok, reason = is_safe_public_url("https://example.com/page")
        assert ok is True
        assert reason is None

    def test_unresolvable_host_rejected(self):
        import socket

        with patch("nfl_mcp.config.resolve_host_addresses", side_effect=socket.gaierror):
            ok, reason = is_safe_public_url("https://does-not-resolve.invalid/")
        assert ok is False
        assert "Could not resolve host" in reason

    def test_private_url_opt_in_bypass(self):
        # Explicit opt-in (NFL_MCP_ALLOW_PRIVATE_URLS) permits private targets.
        with patch("nfl_mcp.config.allow_private_urls", return_value=True):
            ok, reason = is_safe_public_url("http://127.0.0.1:9000/internal")
        assert ok is True
        assert reason is None
