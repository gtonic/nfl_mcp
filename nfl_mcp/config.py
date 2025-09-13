"""
Configuration constants and shared utilities for NFL MCP Server.

This module contains common configuration values and utility functions
to reduce code duplication across the application.

This module now uses the new ConfigManager for flexible configuration
while maintaining backward compatibility with existing code.
"""

import re
import html
import urllib.parse
import time
from collections import defaultdict, deque
import httpx
from typing import Dict, Any, Optional, Union
import os

# Import the new configuration manager
from .config_manager import get_config_manager


# Rate limiting storage (in production, use Redis or similar)
_rate_limit_storage = defaultdict(lambda: deque())


def check_rate_limit(identifier: str, limit: int, window_seconds: int = 60) -> bool:
    """
    Check if a request is within rate limits.
    
    Args:
        identifier: Unique identifier (IP, user_id, etc.)
        limit: Maximum requests allowed in window
        window_seconds: Time window in seconds
        
    Returns:
        True if request is allowed, False if rate limited
    """
    now = time.time()
    requests = _rate_limit_storage[identifier]
    
    # Remove old requests outside the window
    while requests and requests[0] <= now - window_seconds:
        requests.popleft()
    
    # Check if we're at the limit
    if len(requests) >= limit:
        return False
    
    # Add current request
    requests.append(now)
    return True


def get_rate_limit_status(identifier: str, limit: int, window_seconds: int = 60) -> dict:
    """
    Get current rate limit status for an identifier.
    
    Args:
        identifier: Unique identifier
        limit: Maximum requests allowed
        window_seconds: Time window in seconds
        
    Returns:
        Dictionary with rate limit status information
    """
    now = time.time()
    requests = _rate_limit_storage[identifier]
    
    # Remove old requests
    while requests and requests[0] <= now - window_seconds:
        requests.popleft()
    
    remaining = max(0, limit - len(requests))
    reset_time = int(now + window_seconds) if requests else int(now)
    
    return {
        "limit": limit,
        "remaining": remaining,
        "reset": reset_time,
        "retry_after": max(0, int(requests[0] + window_seconds - now)) if len(requests) >= limit else 0
    }


# HTTP Client Configuration - now loaded from ConfigManager
def _get_timeout_config():
    """Get timeout configuration from ConfigManager."""
    try:
        return get_config_manager().get_http_timeout()
    except Exception:
        # Fallback to hardcoded values if ConfigManager fails
        return httpx.Timeout(30.0, connect=10.0)

def _get_long_timeout_config():
    """Get long timeout configuration from ConfigManager."""
    try:
        return get_config_manager().get_long_http_timeout()
    except Exception:
        # Fallback to hardcoded values if ConfigManager fails
        return httpx.Timeout(60.0, connect=15.0)

DEFAULT_TIMEOUT = _get_timeout_config()
LONG_TIMEOUT = _get_long_timeout_config()

# User Agent Configuration - now loaded from ConfigManager
def _get_server_version():
    """Get server version from ConfigManager."""
    try:
        return get_config_manager().config.server.version
    except Exception:
        return "0.1.0"

def _get_base_user_agent():
    """Get base user agent from ConfigManager."""
    try:
        return get_config_manager().config.server.base_user_agent
    except Exception:
        return f"NFL-MCP-Server/{_get_server_version()}"

SERVER_VERSION = _get_server_version()
BASE_USER_AGENT = _get_base_user_agent()

# User Agent strings for different services - now uses ConfigManager
def _get_user_agents():
    """Get user agents dictionary from ConfigManager."""
    try:
        config_manager = get_config_manager()
        return {
            "nfl_news": config_manager.get_user_agent("nfl_news"),
            "nfl_teams": config_manager.get_user_agent("nfl_teams"),
            "depth_chart": config_manager.get_user_agent("depth_chart"),
            "web_crawler": config_manager.get_user_agent("web_crawler"),
            "athletes": config_manager.get_user_agent("athletes"),
            "sleeper_league": config_manager.get_user_agent("sleeper_league"),
            "sleeper_rosters": config_manager.get_user_agent("sleeper_rosters"),
            "sleeper_users": config_manager.get_user_agent("sleeper_users"),
            "sleeper_matchups": config_manager.get_user_agent("sleeper_matchups"),
            "sleeper_playoffs": config_manager.get_user_agent("sleeper_playoffs"),
            "sleeper_transactions": config_manager.get_user_agent("sleeper_transactions"),
            "sleeper_traded_picks": config_manager.get_user_agent("sleeper_traded_picks"),
            "sleeper_nfl_state": config_manager.get_user_agent("sleeper_nfl_state"),
            "sleeper_trending": config_manager.get_user_agent("sleeper_trending"),
        }
    except Exception:
        # Fallback to hardcoded values
        base_agent = f"NFL-MCP-Server/{_get_server_version()}"
        return {
            "nfl_news": f"{base_agent} (NFL News Fetcher)",
            "nfl_teams": f"{base_agent} (NFL Teams Fetcher)",
            "depth_chart": f"{base_agent} (NFL Depth Chart Fetcher)",
            "web_crawler": f"{base_agent} (Web Content Extractor)",
            "athletes": f"{base_agent} (NFL Athletes Fetcher)",
            "sleeper_league": f"{base_agent} (Sleeper League Fetcher)",
            "sleeper_rosters": f"{base_agent} (Sleeper Rosters Fetcher)",
            "sleeper_users": f"{base_agent} (Sleeper Users Fetcher)",
            "sleeper_matchups": f"{base_agent} (Sleeper Matchups Fetcher)",
            "sleeper_playoffs": f"{base_agent} (Sleeper Playoffs Fetcher)",
            "sleeper_transactions": f"{base_agent} (Sleeper Transactions Fetcher)",
            "sleeper_traded_picks": f"{base_agent} (Sleeper Traded Picks Fetcher)",
            "sleeper_nfl_state": f"{base_agent} (Sleeper NFL State Fetcher)",
            "sleeper_trending": f"{base_agent} (Sleeper Trending Players Fetcher)",
        }

USER_AGENTS = _get_user_agents()


def get_http_headers(service_name: str) -> Dict[str, str]:
    """
    Get standardized HTTP headers for a service.
    
    Args:
        service_name: The service name key from USER_AGENTS
        
    Returns:
        Dictionary with standard headers including User-Agent
    """
    return {
        "User-Agent": USER_AGENTS.get(service_name, BASE_USER_AGENT)
    }


def create_http_client(timeout: httpx.Timeout = None) -> httpx.AsyncClient:
    """
    Create a configured HTTP client with standard settings.
    
    Args:
        timeout: Optional custom timeout, uses DEFAULT_TIMEOUT if not provided
        
    Returns:
        Configured httpx.AsyncClient
    """
    return httpx.AsyncClient(
        timeout=timeout or DEFAULT_TIMEOUT,
        follow_redirects=True
    )


# URL Validation - now loaded from ConfigManager
def _get_allowed_url_schemes():
    """Get allowed URL schemes from ConfigManager."""
    try:
        return get_config_manager().config.security.allowed_url_schemes
    except Exception:
        return ["http://", "https://"]

ALLOWED_URL_SCHEMES = _get_allowed_url_schemes()


def is_valid_url(url: str) -> bool:
    """
    Validate URL format for security.
    
    Args:
        url: URL to validate
        
    Returns:
        True if URL is valid and safe, False otherwise
    """
    if not url or not isinstance(url, str):
        return False
    
    return any(url.startswith(scheme) for scheme in ALLOWED_URL_SCHEMES)


# Parameter Validation Limits - now loaded from ConfigManager
def _get_limits():
    """Get validation limits from ConfigManager."""
    try:
        return get_config_manager().get_limits_dict()
    except Exception:
        # Fallback to hardcoded values
        return {
            "nfl_news_max": 50,
            "nfl_news_min": 1,
            "athletes_search_max": 100,
            "athletes_search_min": 1,
            "athletes_search_default": 10,
            "week_min": 1,
            "week_max": 22,
            "round_min": 1,
            "round_max": 18,
            "trending_lookback_min": 1,
            "trending_lookback_max": 168,
            "trending_limit_min": 1,
            "trending_limit_max": 100,
        }

LIMITS = _get_limits()

# Feature flags (environment-variable driven for simplicity). Use ConfigManager later if desired.
def is_feature_enabled(flag_name: str, default: bool = False) -> bool:
    env_key = f"MCP_FEATURE_{flag_name.upper()}"
    val = os.getenv(env_key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "enabled"}

# Specific flags
FEATURE_LEAGUE_LEADERS = is_feature_enabled("league_leaders", default=False)


# Input Validation Security Patterns
DANGEROUS_PATTERNS = {
    'sql_injection': [
        r'(\bunion\b|\bselect\b|\binsert\b|\bupdate\b|\bdelete\b|\bcreate\b|\balter\b)',
        r'(--|\/\*|\*\/)',
        r'(\bor\b|\band\b)\s+\d+\s*=\s*\d+',
        r'(\bor\b|\band\b)\s+\w+\s*=\s*\w+',
        r"('\s*or\s*'|\"\s*or\s*\")",
    ],
    'xss_injection': [
        r'<script[^>]*>',
        r'javascript:',
        r'onload\s*=',
        r'onerror\s*=',
        r'onclick\s*=',
        r'onmouseover\s*=',
    ],
    'command_injection': [
        r'(\||;|`|\$\(|\${)',
        r'(rm\s+|del\s+|format\s+)',
        r'(wget\s+|curl\s+|nc\s+|netcat\s+)',
    ],
    'path_traversal': [
        r'\.\./|\.\.\\\|%2e%2e',
        r'/etc/|/proc/|/sys/',
        r'\.\.%2f|\.\.%5c',
    ]
}

# Safe character patterns for different input types
SAFE_PATTERNS = {
    'alphanumeric_id': re.compile(r'^[a-zA-Z0-9_-]+$'),
    'team_id': re.compile(r'^[A-Z]{2,4}$'),  # NFL team abbreviations
    'athlete_name': re.compile(r"^[a-zA-Z\s\.\-']+$"),  # Names with common punctuation
    'league_id': re.compile(r'^[0-9]+$'),  # Sleeper league IDs are numeric
    'trend_type': re.compile(r'^(add|drop)$'),  # Only valid trend types
}

# Rate limiting constants - now loaded from ConfigManager
def _get_rate_limits():
    """Get rate limits from ConfigManager."""
    try:
        return get_config_manager().get_rate_limits_dict()
    except Exception:
        # Fallback to hardcoded values
        return {
            'default_requests_per_minute': 60,
            'heavy_requests_per_minute': 10,
            'burst_limit': 5,
        }

RATE_LIMITS = _get_rate_limits()


def validate_string_input(value: str, input_type: str = 'general', max_length: int = None, required: bool = True) -> str:
    """
    Validate and sanitize string inputs to prevent injection attacks.
    
    Args:
        value: The string value to validate
        input_type: Type of input (general, id, name, team_id, league_id, trend_type)
        max_length: Maximum allowed length (uses ConfigManager default if None)
        required: Whether the input is required (cannot be empty)
        
    Returns:
        Validated and sanitized string
        
    Raises:
        ValueError: If validation fails
    """
    if value is None:
        if required:
            raise ValueError("Required string input cannot be None")
        return ""
    
    if not isinstance(value, str):
        raise ValueError(f"Input must be a string, got {type(value)}")
    
    # Get max_length from ConfigManager if not provided
    if max_length is None:
        try:
            max_length = get_config_manager().config.security.max_string_length
        except Exception:
            max_length = 1000  # Fallback default
    
    # Check length
    if len(value) > max_length:
        raise ValueError(f"Input length ({len(value)}) exceeds maximum ({max_length})")
    
    if required and not value.strip():
        raise ValueError("Required string input cannot be empty")
    
    # Validate against specific patterns if input_type is specified FIRST
    if input_type in SAFE_PATTERNS:
        if not SAFE_PATTERNS[input_type].match(value):
            raise ValueError(f"Input does not match required pattern for {input_type}")
    
    # Sanitize the input
    sanitized = html.escape(value.strip())
    
    # Check for dangerous patterns only if not a specific safe pattern type
    # and if injection detection is enabled
    try:
        enable_injection_detection = get_config_manager().config.security.enable_injection_detection
    except Exception:
        enable_injection_detection = True  # Default to enabled
    
    if input_type not in SAFE_PATTERNS and enable_injection_detection:
        value_lower = value.lower()
        for pattern_type, patterns in DANGEROUS_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, value_lower, re.IGNORECASE):
                    raise ValueError(f"Input contains potentially dangerous pattern ({pattern_type})")
    
    return sanitized


def validate_numeric_input(value: Any, min_val: int = None, max_val: int = None, 
                         default: int = None, required: bool = True) -> int:
    """
    Enhanced numeric validation with type checking and comprehensive validation.
    
    Args:
        value: The value to validate
        min_val: Minimum allowed value
        max_val: Maximum allowed value
        default: Default value if None or invalid
        required: Whether the input is required
        
    Returns:
        Validated integer value
        
    Raises:
        ValueError: If validation fails and no default provided
    """
    if value is None:
        if default is not None:
            return default
        if not required:
            return 0
        raise ValueError("Required numeric input cannot be None")
    
    # Try to convert to int
    try:
        if isinstance(value, str):
            # Check for dangerous patterns in string numbers
            if any(char in value for char in ['$', '(', ')', ';', '|', '&']):
                raise ValueError("Numeric input contains invalid characters")
        
        int_value = int(value)
    except (ValueError, TypeError):
        if default is not None:
            return default
        raise ValueError(f"Cannot convert '{value}' to integer")
    
    # Range validation
    if min_val is not None and int_value < min_val:
        if default is not None:
            return max(default, min_val)
        raise ValueError(f"Value {int_value} is below minimum {min_val}")
    
    if max_val is not None and int_value > max_val:
        if default is not None:
            return min(default, max_val)
        raise ValueError(f"Value {int_value} exceeds maximum {max_val}")
    
    return int_value


def validate_limit(value: int, min_val: int, max_val: int, default: int = None) -> int:
    """
    Validate and correct a limit parameter.
    
    Args:
        value: The value to validate
        min_val: Minimum allowed value
        max_val: Maximum allowed value
        default: Default value if None or invalid
        
    Returns:
        Validated and corrected value
    """
    if value is None:
        return default if default is not None else min_val
    
    try:
        return validate_numeric_input(value, min_val, max_val, default, required=True)
    except ValueError:
        return default if default is not None else min_val


def sanitize_content(content: str, max_length: int = None) -> str:
    """
    Sanitize text content for safe processing and display.
    
    Args:
        content: Text content to sanitize
        max_length: Maximum length (truncates with ellipsis)
        
    Returns:
        Sanitized content
    """
    if not content:
        return ""
    
    # Remove potentially dangerous script tags and javascript first
    sanitized = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.IGNORECASE | re.DOTALL)
    sanitized = re.sub(r'javascript:', '', sanitized, flags=re.IGNORECASE)
    
    # HTML escape
    sanitized = html.escape(sanitized)
    
    # Normalize whitespace
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    
    # Truncate if needed
    if max_length and len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."
    
    return sanitized


def validate_url_enhanced(url: str, allowed_schemes: list = None, allowed_domains: list = None) -> bool:
    """
    Enhanced URL validation with additional security checks.
    
    Args:
        url: URL to validate
        allowed_schemes: List of allowed schemes (defaults to http/https)
        allowed_domains: Optional list of allowed domains
        
    Returns:
        True if URL is valid and safe, False otherwise
    """
    if not url or not isinstance(url, str):
        return False
    
    # Use existing basic validation first
    if not is_valid_url(url):
        return False
    
    schemes = allowed_schemes or ALLOWED_URL_SCHEMES
    
    try:
        parsed = urllib.parse.urlparse(url)
        
        # Check scheme
        if not any(url.startswith(scheme) for scheme in schemes):
            return False
        
        # Check for dangerous patterns
        url_lower = url.lower()
        for pattern_type, patterns in DANGEROUS_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, url_lower):
                    return False
        
        # Check domain restrictions if provided
        if allowed_domains and parsed.netloc:
            domain_allowed = any(
                parsed.netloc.endswith(domain) or parsed.netloc == domain
                for domain in allowed_domains
            )
            if not domain_allowed:
                return False
        
        # Prevent local/private network access
        if parsed.hostname:
            if parsed.hostname in ['localhost', '127.0.0.1', '0.0.0.0']:
                return False
            if parsed.hostname.startswith('192.168.') or parsed.hostname.startswith('10.'):
                return False
            if parsed.hostname.startswith('172.') and parsed.hostname.split('.')[1].isdigit():
                second_octet = int(parsed.hostname.split('.')[1])
                if 16 <= second_octet <= 31:  # 172.16.0.0/12
                    return False
        
        return True
        
    except Exception:
        return False