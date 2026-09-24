"""
Configuration constants and shared utilities for NFL MCP Server.

This module contains common configuration values and utility functions
to reduce code duplication across the application.

This module now uses the new ConfigManager for flexible configuration
while maintaining backward compatibility with existing code.
"""

import asyncio
import html
import ipaddress
import logging
import os
import re
import socket
import time
import urllib.parse
from typing import Any

import httpx

# Import the new configuration manager
from .config_manager import PROJECT_URL, get_config_manager
from .input_warnings import add_input_warning

logger = logging.getLogger(__name__)


class OutboundRateLimiter:
    """
    Token bucket rate limiter for outbound API calls.

    Prevents hitting external API rate limits by controlling request rate.
    Thread-safe and async-compatible.
    """

    def __init__(self, calls_per_minute: int = 60, burst_capacity: int | None = None):
        """
        Initialize the rate limiter.

        Args:
            calls_per_minute: Maximum sustained calls per minute
            burst_capacity: Maximum burst size (defaults to calls_per_minute)
        """
        self.rate = calls_per_minute / 60.0  # tokens per second
        self.capacity = burst_capacity or calls_per_minute
        self.tokens = float(self.capacity)
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> float:
        """
        Acquire tokens, waiting if necessary.

        Args:
            tokens: Number of tokens to acquire (default 1)

        Returns:
            Time waited in seconds
        """
        async with self._lock:
            waited = 0.0
            while True:
                now = time.monotonic()
                # Replenish tokens based on elapsed time
                elapsed = now - self.last_update
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_update = now

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return waited

                # Calculate wait time needed
                tokens_needed = tokens - self.tokens
                wait_time = tokens_needed / self.rate
                await asyncio.sleep(min(wait_time, 1.0))  # Cap at 1 second chunks
                waited += min(wait_time, 1.0)

    def get_status(self) -> dict[str, Any]:
        """Get current rate limiter status."""
        now = time.monotonic()
        elapsed = now - self.last_update
        current_tokens = min(self.capacity, self.tokens + elapsed * self.rate)

        return {
            "available_tokens": round(current_tokens, 2),
            "capacity": self.capacity,
            "rate_per_second": round(self.rate, 2),
            "rate_per_minute": round(self.rate * 60, 1)
        }


# Global rate limiters for external APIs
_rate_limiters: dict[str, OutboundRateLimiter] = {}

# Per-API sustained limits (requests/minute). Sleeper documents "stay under
# 1000 calls a minute" per IP; 600 leaves headroom for other clients on the
# same address. The others publish no number, so they are conservative.
_DEFAULT_API_RATE_LIMITS: dict[str, int] = {
    "sleeper": 600,
    "espn": 120,
    "odds_api": 30,
    "nflverse": 60,
    "fantasycalc": 60,
    "cbs": 30,
    "nfl_com": 60,
    "open_meteo": 120,
}

# Outbound host -> limiter name. Every client built by create_http_client
# acquires a token for its request's host before sending (redirect hops too,
# e.g. github.com -> release-assets for nflverse downloads).
_HOST_RATE_LIMITERS: tuple[tuple[str, str], ...] = (
    ("sleeper.app", "sleeper"),
    ("sleeper.com", "sleeper"),
    ("espn.com", "espn"),
    ("the-odds-api.com", "odds_api"),
    ("github.com", "nflverse"),
    ("githubusercontent.com", "nflverse"),
    ("fantasycalc.com", "fantasycalc"),
    ("cbssports.com", "cbs"),
    ("nfl.com", "nfl_com"),
    ("open-meteo.com", "open_meteo"),
)


def rate_limiter_name_for_host(host: str | None) -> str | None:
    """Limiter name for an outbound host (suffix match), or None if unlimited."""
    if not host:
        return None
    host = host.lower().rstrip(".")
    for suffix, name in _HOST_RATE_LIMITERS:
        if host == suffix or host.endswith("." + suffix):
            return name
    return None


def get_rate_limiter(api_name: str) -> OutboundRateLimiter:
    """
    Get or create a rate limiter for an API.

    Defaults come from ``_DEFAULT_API_RATE_LIMITS``; an API without an entry
    uses the configured ``rate_limits.default_requests_per_minute`` (60).

    Can be overridden via environment variables, e.g.:
    - NFL_MCP_SLEEPER_RATE_LIMIT
    - NFL_MCP_ESPN_RATE_LIMIT
    - NFL_MCP_ODDS_API_RATE_LIMIT
    """
    if api_name not in _rate_limiters:
        # Check for env var override
        env_key = f"NFL_MCP_{api_name.upper()}_RATE_LIMIT"
        env_value = os.getenv(env_key)

        limit = None
        if env_value:
            try:
                limit = int(env_value)
            except ValueError:
                limit = None
        if limit is None or limit <= 0:
            limit = _DEFAULT_API_RATE_LIMITS.get(api_name)
        if limit is None:
            try:
                limit = get_config_manager().config.rate_limits.default_requests_per_minute
            except Exception:
                limit = 60

        _rate_limiters[api_name] = OutboundRateLimiter(
            calls_per_minute=limit,
            burst_capacity=min(limit * 2, 200)  # Allow burst up to 2x, max 200
        )

    return _rate_limiters[api_name]


def get_all_rate_limiter_status() -> dict[str, dict[str, Any]]:
    """Get status of all rate limiters."""
    return {name: limiter.get_status() for name, limiter in _rate_limiters.items()}


async def _outbound_rate_limit_hook(request: httpx.Request) -> None:
    """httpx request hook: wait for the host's limiter before sending."""
    name = rate_limiter_name_for_host(request.url.host)
    if name is None:
        return
    try:
        waited = await get_rate_limiter(name).acquire()
    except Exception as e:  # a limiter fault must never block the request
        logger.debug(f"[RateLimit] {name} limiter error ignored: {e}")
        return
    if waited > 0:
        logger.debug(f"[RateLimit] {name}: waited {waited:.2f}s for {request.url.host}")


# HTTP Client Configuration - now loaded from ConfigManager with env var support
def _get_timeout_config():
    """Get timeout configuration from ConfigManager and environment variables."""
    try:
        # Check for environment variable override first
        timeout_seconds = os.getenv("NFL_MCP_API_TIMEOUT")
        if timeout_seconds:
            timeout_val = float(timeout_seconds)
            return httpx.Timeout(timeout_val, connect=timeout_val / 3)
        # Fall back to ConfigManager
        return get_config_manager().get_http_timeout()
    except Exception:
        # Fallback to hardcoded values if ConfigManager fails
        return httpx.Timeout(30.0, connect=10.0)

def _get_long_timeout_config():
    """Get long timeout configuration from ConfigManager and environment variables."""
    try:
        # Check for environment variable override first
        timeout_seconds = os.getenv("NFL_MCP_API_LONG_TIMEOUT")
        if timeout_seconds:
            timeout_val = float(timeout_seconds)
            return httpx.Timeout(timeout_val, connect=timeout_val / 4)
        # Fall back to ConfigManager
        return get_config_manager().get_long_http_timeout()
    except Exception:
        # Fallback to hardcoded values if ConfigManager fails
        return httpx.Timeout(60.0, connect=15.0)

DEFAULT_TIMEOUT = _get_timeout_config()
LONG_TIMEOUT = _get_long_timeout_config()

# User Agent Configuration - now loaded from ConfigManager
def _get_server_version():
    """Get server version from ConfigManager (which defaults to the package version)."""
    try:
        return get_config_manager().config.server.version
    except Exception:
        from ._version import get_version
        return get_version()

def _get_base_user_agent():
    """Get base user agent from ConfigManager."""
    try:
        return get_config_manager().config.server.base_user_agent
    except Exception:
        return f"NFL-MCP-Server/{_get_server_version()} (+{PROJECT_URL})"

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
            "cbs_fantasy": config_manager.get_user_agent("cbs_fantasy"),
        }
    except Exception:
        # Fallback to hardcoded values
        base_agent = f"NFL-MCP-Server/{_get_server_version()} (+{PROJECT_URL})"
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
            "cbs_fantasy": f"{base_agent} (CBS Fantasy Football Fetcher)",
        }

USER_AGENTS = _get_user_agents()


def get_http_headers(service_name: str) -> dict[str, str]:
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


def create_http_client(
    timeout: httpx.Timeout = None,
    follow_redirects: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """
    Create a configured HTTP client with standard settings.

    Args:
        timeout: Optional custom timeout, uses DEFAULT_TIMEOUT if not provided
        follow_redirects: Whether httpx should transparently follow redirects.
            Callers that fetch *user-supplied* URLs should pass ``False`` and
            follow redirects manually so each hop can be re-validated against
            :func:`is_safe_public_url` (SSRF protection).
        transport: Optional custom transport (crawl_url pins DNS with one).

    Outbound requests to known APIs (Sleeper, ESPN, Odds API, nflverse, ...)
    are paced by the per-host limiters via a request hook.

    Returns:
        Configured httpx.AsyncClient
    """
    kwargs: dict[str, Any] = {}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(
        timeout=timeout or DEFAULT_TIMEOUT,
        follow_redirects=follow_redirects,
        event_hooks={"request": [_outbound_rate_limit_hook]},
        **kwargs,
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


def allow_private_urls() -> bool:
    """
    Whether requests to private / non-public IP ranges are permitted.

    Off by default. Set ``NFL_MCP_ALLOW_PRIVATE_URLS=1`` (or true/yes/on) to
    opt in — only do this when the server runs in a trusted, isolated network
    and you intentionally need to crawl internal hosts.
    """
    return os.getenv("NFL_MCP_ALLOW_PRIVATE_URLS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _ip_is_disallowed(ip: "ipaddress._BaseAddress") -> bool:
    """Return True if an IP address must not be contacted (SSRF protection)."""
    # Normalize IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) to its IPv4 form so the
    # underlying private/loopback address is inspected rather than the wrapper.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # Belt-and-suspenders: ``is_global`` semantics have shifted across CPython
    # releases, so OR the individual special-range flags as well.
    return (
        not ip.is_global
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_private
        or ip.is_reserved
        or ip.is_unspecified
    )


# Ports a user-supplied URL may target. Anything else (Redis on 6379, an
# admin UI on 8080, ...) is a classic SSRF pivot even on a public address.
ALLOWED_URL_PORTS = frozenset({80, 443})


def _port_reason(parsed: urllib.parse.ParseResult) -> str | None:
    """Why the URL's port is refused, or None when it is allowed."""
    try:
        port = parsed.port
    except ValueError:
        return "Malformed URL port"
    if port is None or port in ALLOWED_URL_PORTS:
        return None
    return f"Blocked port {port} (only 80/443 are allowed)"


def resolve_host_addresses(hostname: str) -> list:
    """
    Resolve a hostname to the list of unique IP addresses it maps to.

    Wrapped in its own function so it can be mocked in tests without touching
    the network. Raises ``socket.gaierror`` when resolution fails.
    """
    infos = socket.getaddrinfo(hostname, None)
    return list({info[4][0] for info in infos})


async def resolve_host_addresses_async(hostname: str) -> list:
    """Non-blocking :func:`resolve_host_addresses` (the loop's resolver, not
    a blocking ``getaddrinfo`` on the event loop thread)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _check_candidates(hostname: str, candidates: list):
    """Every resolved address must be public; returns ``(ok, reason)``."""
    if not candidates:
        return False, f"Could not resolve host: {hostname}"
    for ip_str in candidates:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"Invalid resolved address: {ip_str}"
        if _ip_is_disallowed(ip):
            return False, f"Blocked non-public address: {ip_str} (host {hostname})"
    return True, None


async def resolve_safe_url(url):
    """Async SSRF gate that also returns the validated addresses.

    Like :func:`is_safe_public_url`, but resolves without blocking the event
    loop and returns ``(ok, reason, hostname, addresses)`` so the caller can
    pin the connection to exactly the addresses that were checked -- closing
    the DNS-rebinding window between "resolve to validate" and "resolve to
    connect". ``addresses`` is empty for IP literals and when private URLs are
    allowed (nothing to pin).
    """
    if not is_valid_url(url):
        return False, "URL must start with http:// or https://", None, []
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
    except Exception:
        return False, "Malformed URL", None, []
    if not hostname:
        return False, "URL has no host", None, []
    if allow_private_urls():
        return True, None, hostname, []
    port_reason = _port_reason(parsed)
    if port_reason:
        return False, port_reason, hostname, []
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        ok, reason = _check_candidates(hostname, [hostname])
        return ok, reason, hostname, []
    try:
        candidates = await resolve_host_addresses_async(hostname)
    except OSError:  # socket.gaierror is an OSError
        return False, f"Could not resolve host: {hostname}", hostname, []
    ok, reason = _check_candidates(hostname, candidates)
    return ok, reason, hostname, (candidates if ok else [])


def is_safe_public_url(url):
    """
    SSRF-hardened URL check used before fetching *user-supplied* URLs.

    Validates the scheme and then resolves the host, blocking the request if
    any resolved address is loopback, private, link-local (incl. the
    ``169.254.169.254`` cloud-metadata endpoint), multicast, reserved or
    otherwise non-global. IP literals (decimal/octal/IPv6 forms are normalized
    by :mod:`ipaddress`) are checked directly.

    Returns:
        Tuple ``(ok, reason)`` where ``reason`` is ``None`` when ``ok`` is True
        and a human-readable block reason otherwise.
    """
    if not is_valid_url(url):
        return False, "URL must start with http:// or https://"

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "Malformed URL"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no host"

    if allow_private_urls():
        return True, None

    port_reason = _port_reason(parsed)
    if port_reason:
        return False, port_reason

    # If the host is an IP literal, check it directly; otherwise resolve DNS
    # and check every address it maps to.
    try:
        ipaddress.ip_address(hostname)
        candidates = [hostname]
    except ValueError:
        try:
            candidates = resolve_host_addresses(hostname)
        except socket.gaierror:
            return False, f"Could not resolve host: {hostname}"
        if not candidates:
            return False, f"Could not resolve host: {hostname}"

    return _check_candidates(hostname, candidates)


def safe_espn_ref(ref: object) -> str | None:
    """An ESPN ``$ref``/``href`` link, forced to https, or None if off-ESPN.

    The Core API hands back ``http://`` follow-up links; they are fetched
    with our headers, so only ``espn.com`` / ``*.espn.com`` hosts are
    followed (over TLS, default port) -- a tampered or unexpected payload
    cannot point the server at an arbitrary host.
    """
    if not isinstance(ref, str) or not ref:
        return None
    try:
        parts = urllib.parse.urlsplit(ref)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or port not in (None, 80, 443):
        return None
    if host != "espn.com" and not host.endswith(".espn.com"):
        return None
    return urllib.parse.urlunsplit(("https", host, parts.path, parts.query, ""))


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


def _update_timeout(target: httpx.Timeout, source: httpx.Timeout) -> None:
    for attr in ("connect", "read", "write", "pool"):
        setattr(target, attr, getattr(source, attr))


def refresh_from_config_manager() -> None:
    """Re-derive the module-level config values from the current ConfigManager.

    The constants above are built at import, before ``main()`` installs the
    manager for ``NFL_MCP_CONFIG_FILE`` (and before any hot reload). Other
    modules hold ``from .config import LIMITS, DEFAULT_TIMEOUT`` references,
    so everything is updated IN PLACE -- the same dict/list/Timeout objects --
    rather than rebound. Called by ``config_manager.set_config_manager`` and
    after every (hot) reload.
    """
    global SERVER_VERSION, BASE_USER_AGENT
    _update_timeout(DEFAULT_TIMEOUT, _get_timeout_config())
    _update_timeout(LONG_TIMEOUT, _get_long_timeout_config())
    SERVER_VERSION = _get_server_version()
    BASE_USER_AGENT = _get_base_user_agent()
    _swap_in_place(USER_AGENTS, _get_user_agents())
    ALLOWED_URL_SCHEMES[:] = _get_allowed_url_schemes()
    _swap_in_place(LIMITS, _get_limits())


def _swap_in_place(target: dict, new: dict) -> None:
    """Replace ``target``'s contents with ``new`` without an empty window.

    A hot reload runs on the watchdog thread while tool calls read these dicts;
    ``clear()`` then ``update()`` let a reader in between see no keys at all
    (a KeyError on ``LIMITS["..."]``). One ``update()`` with the fully built
    dict is a single step, and only keys the new config dropped are removed.
    """
    target.update(new)
    for key in [k for k in target if k not in new]:
        target.pop(key, None)

# Feature flags (environment-variable driven for simplicity). Use ConfigManager later if desired.
def is_feature_enabled(flag_name: str, default: bool = False) -> bool:
    env_key = f"MCP_FEATURE_{flag_name.upper()}"
    val = os.getenv(env_key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "enabled"}

# Specific flags
FEATURE_LEAGUE_LEADERS = is_feature_enabled("league_leaders", default=True)


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

def validate_string_input(value: str, input_type: str = 'general', max_length: int | None = None, required: bool = True) -> str:
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

    # Team abbreviations are case-insensitive for the caller ("kc" is KC).
    if input_type == 'team_id':
        value = value.strip().upper()

    # Validate against specific patterns if input_type is specified FIRST
    if input_type in SAFE_PATTERNS and not SAFE_PATTERNS[input_type].match(value):
        raise ValueError(f"Input does not match required pattern for {input_type}")

    # Returned unescaped. These values are lookup keys and URL parameters, never
    # HTML: escaping here turned "Ja'Marr Chase" into "Ja&#x27;Marr Chase",
    # which matched no market value or game log, so every apostrophe name fell
    # back to a generic rank projection. Output escaping is `sanitize_content`.
    sanitized = value.strip()

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


def validate_numeric_input(value: Any, min_val: int | None = None, max_val: int | None = None,
                         default: int | None = None, required: bool = True) -> int:
    """
    Enhanced numeric validation with type checking and comprehensive validation.

    ``default`` only stands in for a MISSING value (None) or one that cannot be
    parsed. An out-of-range value is never replaced by the default: without a
    default it raises; with one it is clamped to the violated bound and the
    correction is reported to the caller (``input_warnings`` on the tool
    response), so ``limit=500`` becomes the maximum, visibly -- not 25.

    Args:
        value: The value to validate
        min_val: Minimum allowed value
        max_val: Maximum allowed value
        default: Value used when ``value`` is None or unparseable
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
            add_input_warning(f"Could not parse {value!r} as an integer; used default {default}")
            return default
        raise ValueError(f"Cannot convert '{value}' to integer") from None

    # Range validation
    if min_val is not None and int_value < min_val:
        if default is not None:
            add_input_warning(f"Value {int_value} is below minimum {min_val}; clamped to {min_val}")
            return min_val
        raise ValueError(f"Value {int_value} is below minimum {min_val}")

    if max_val is not None and int_value > max_val:
        if default is not None:
            add_input_warning(f"Value {int_value} exceeds maximum {max_val}; clamped to {max_val}")
            return max_val
        raise ValueError(f"Value {int_value} exceeds maximum {max_val}")

    return int_value


def validate_limit(value: int, min_val: int, max_val: int, default: int | None = None) -> int:
    """
    Validate and correct a limit parameter.

    None -> ``default`` (or ``min_val``). Out of range -> clamped to the
    violated bound, never reset to the default; the correction is reported via
    ``input_warnings`` on the tool response.

    Args:
        value: The value to validate
        min_val: Minimum allowed value
        max_val: Maximum allowed value
        default: Default value if None or unparseable

    Returns:
        Validated and corrected value
    """
    fallback = default if default is not None else min_val
    if value is None:
        return fallback

    try:
        return validate_numeric_input(value, min_val, max_val, fallback, required=True)
    except ValueError:
        add_input_warning(f"Invalid limit {value!r}; used {fallback}")
        return fallback


def sanitize_content(content: str, max_length: int | None = None) -> str:
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
