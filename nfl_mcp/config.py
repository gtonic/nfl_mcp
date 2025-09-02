"""
Configuration constants and shared utilities for NFL MCP Server.

This module contains common configuration values and utility functions
to reduce code duplication across the application.
"""

import httpx
from typing import Dict


# HTTP Client Configuration
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
LONG_TIMEOUT = httpx.Timeout(60.0, connect=15.0)  # For large data fetches

# User Agent Configuration
SERVER_VERSION = "0.1.0"
BASE_USER_AGENT = f"NFL-MCP-Server/{SERVER_VERSION}"

# User Agent strings for different services
USER_AGENTS = {
    "nfl_news": f"{BASE_USER_AGENT} (NFL News Fetcher)",
    "nfl_teams": f"{BASE_USER_AGENT} (NFL Teams Fetcher)",
    "depth_chart": f"{BASE_USER_AGENT} (NFL Depth Chart Fetcher)",
    "web_crawler": f"{BASE_USER_AGENT} (Web Content Extractor)",
    "athletes": f"{BASE_USER_AGENT} (NFL Athletes Fetcher)",
    "sleeper_league": f"{BASE_USER_AGENT} (Sleeper League Fetcher)",
    "sleeper_rosters": f"{BASE_USER_AGENT} (Sleeper Rosters Fetcher)",
    "sleeper_users": f"{BASE_USER_AGENT} (Sleeper Users Fetcher)",
    "sleeper_matchups": f"{BASE_USER_AGENT} (Sleeper Matchups Fetcher)",
    "sleeper_playoffs": f"{BASE_USER_AGENT} (Sleeper Playoffs Fetcher)",
    "sleeper_transactions": f"{BASE_USER_AGENT} (Sleeper Transactions Fetcher)",
    "sleeper_traded_picks": f"{BASE_USER_AGENT} (Sleeper Traded Picks Fetcher)",
    "sleeper_nfl_state": f"{BASE_USER_AGENT} (Sleeper NFL State Fetcher)",
    "sleeper_trending": f"{BASE_USER_AGENT} (Sleeper Trending Players Fetcher)",
}


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


# URL Validation
ALLOWED_URL_SCHEMES = ["http://", "https://"]


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


# Parameter Validation Limits
LIMITS = {
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
        return default or min_val
    
    if value < min_val:
        return min_val
    
    if value > max_val:
        return max_val
    
    return value