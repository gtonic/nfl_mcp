"""
Retry and Circuit Breaker utilities for robust API calls.

This module provides:
- Configurable retry logic with exponential backoff
- Circuit breaker pattern to prevent cascading failures
- Partial data return on errors
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, rejecting requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker to prevent repeated failures.

    States:
    - CLOSED: Normal operation, requests go through
    - OPEN: Too many failures, reject requests immediately
    - HALF_OPEN: Testing recovery, allow limited requests

    Configuration via environment variables:
    - NFL_MCP_CIRCUIT_FAILURE_THRESHOLD: failures before opening (default: 5)
    - NFL_MCP_CIRCUIT_TIMEOUT: seconds to wait before testing recovery (default: 60)
    - NFL_MCP_CIRCUIT_SUCCESS_THRESHOLD: successes needed to close (default: 2)
    """

    def __init__(self, name: str):
        self.name = name
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: float | None = None
        self._probe_in_flight = False
        self._probe_started: float | None = None

        # Configuration from environment
        self.failure_threshold = int(os.getenv("NFL_MCP_CIRCUIT_FAILURE_THRESHOLD", "5"))
        self.timeout = int(os.getenv("NFL_MCP_CIRCUIT_TIMEOUT", "60"))
        self.success_threshold = int(os.getenv("NFL_MCP_CIRCUIT_SUCCESS_THRESHOLD", "2"))
        # A probe that never reports back (lost task, bug) must not pin the
        # breaker in HALF_OPEN: after this long another caller may probe.
        self.probe_timeout = int(os.getenv("NFL_MCP_CIRCUIT_PROBE_TIMEOUT", "120"))

    def allow_request(self) -> bool:
        """Whether a call may go out now (and, when HALF_OPEN, claim the probe).

        OPEN rejects until the timeout has passed; the first caller after that
        moves the breaker to HALF_OPEN and becomes the single probe. While the
        probe is in flight every other caller is rejected, so a recovering host
        gets one request rather than the whole backlog at once.
        """
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if not self._should_attempt_reset():
                return False
            logger.info(f"[Circuit Breaker {self.name}] Attempting reset (HALF_OPEN)")
            self.state = CircuitState.HALF_OPEN
            self.success_count = 0
            self._probe_in_flight = False
        if self._probe_in_flight:
            started = self._probe_started
            if started is None or time.time() - started < self.probe_timeout:
                return False
            logger.warning(f"[Circuit Breaker {self.name}] Probe timed out; allowing a new one")
        self._probe_in_flight = True
        self._probe_started = time.time()
        return True

    def release_probe(self) -> None:
        """End a probe without a verdict (the call failed for a non-host reason)."""
        self._probe_in_flight = False
        self._probe_started = None

    def _on_success(self):
        """Handle successful call."""
        self.failure_count = 0
        self._probe_in_flight = False

        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                logger.info(f"[Circuit Breaker {self.name}] Closing circuit (recovered)")
                self.state = CircuitState.CLOSED
                self.success_count = 0

    def _on_failure(self):
        """Handle failed call."""
        self.failure_count += 1
        self._probe_in_flight = False
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            logger.warning(f"[Circuit Breaker {self.name}] Failed during HALF_OPEN, reopening")
            self.state = CircuitState.OPEN
            self.success_count = 0
        elif self.failure_count >= self.failure_threshold:
            logger.warning(
                f"[Circuit Breaker {self.name}] Opening circuit "
                f"({self.failure_count} failures >= {self.failure_threshold})"
            )
            self.state = CircuitState.OPEN

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        if self.last_failure_time is None:
            return True
        return (time.time() - self.last_failure_time) >= self.timeout

    def reset(self):
        """Manually reset the circuit breaker."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self._probe_in_flight = False
        logger.info(f"[Circuit Breaker {self.name}] Manual reset")


class CircuitBreakerError(Exception):
    """Exception raised when circuit breaker is open."""


class RetryableHTTPStatus(Exception):
    """An upstream answered, but with a server-side error (5xx) or 429.

    Raised from inside a retried fetch so ``retry_with_backoff`` retries it and,
    once retries are exhausted, records ONE circuit-breaker failure. Fetchers
    that turn a non-200 into ``[]`` would otherwise report an outage as success.
    Other 4xx (404 for an unpublished week etc.) are not failures of the host.
    """

    def __init__(self, status_code: int, url: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code}{f' from {url}' if url else ''}")


def is_retryable_status(status_code: int) -> bool:
    """5xx and 429 count as upstream failures; everything else does not."""
    return status_code >= 500 or status_code == 429


def raise_for_retryable_status(resp: Any) -> None:
    """Raise :class:`RetryableHTTPStatus` for a 5xx/429 response."""
    status = getattr(resp, "status_code", None)
    if isinstance(status, int) and is_retryable_status(status):
        raise RetryableHTTPStatus(status, str(getattr(resp, "url", "") or ""))


def is_retryable_error(exc: BaseException) -> bool:
    """Transport failures and 5xx/429 answers are worth retrying (and count
    against the host's breaker); anything else — a 404, a parse error, a bug
    in the caller — would fail the same way again and says nothing about
    whether the host is up.

    A JSON decode error after a successful transport is the exception: it is
    an HTML error/maintenance page served with a 200, which is the host's
    fault and usually transient.
    """
    if isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError, RetryableHTTPStatus,
                        json.JSONDecodeError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return is_retryable_status(exc.response.status_code)
    return False


# Global circuit breakers for different API endpoints
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str) -> CircuitBreaker:
    """
    Get or create a circuit breaker for an endpoint.

    Args:
        name: Unique name for the circuit breaker

    Returns:
        CircuitBreaker instance
    """
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(name)
    return _circuit_breakers[name]


async def retry_with_backoff(
    func: Callable,
    *args,
    max_retries: int | None = None,
    initial_delay: float | None = None,
    max_delay: float | None = None,
    exponential_base: float = 2.0,
    circuit_breaker_name: str | None = None,
    **kwargs
) -> Any:
    """
    Execute function with exponential backoff retry.

    Args:
        func: Async function to execute
        *args: Positional arguments for func
        max_retries: Maximum retry attempts (env: NFL_MCP_MAX_RETRIES, default: 3)
        initial_delay: Initial delay in seconds (env: NFL_MCP_RETRY_INITIAL_DELAY, default: 0.5)
        max_delay: Maximum delay in seconds (env: NFL_MCP_RETRY_MAX_DELAY, default: 10.0)
        exponential_base: Base for exponential backoff (default: 2.0)
        circuit_breaker_name: Name of circuit breaker to use (optional)
        **kwargs: Keyword arguments for func

    Returns:
        Function result

    Raises:
        Last exception if all retries fail
    """
    # Get configuration from environment or use defaults
    if max_retries is None:
        max_retries = int(os.getenv("NFL_MCP_MAX_RETRIES", "3"))
    if initial_delay is None:
        initial_delay = float(os.getenv("NFL_MCP_RETRY_INITIAL_DELAY", "0.5"))
    if max_delay is None:
        max_delay = float(os.getenv("NFL_MCP_RETRY_MAX_DELAY", "10.0"))

    # Get circuit breaker if name provided
    circuit_breaker = None
    if circuit_breaker_name:
        circuit_breaker = get_circuit_breaker(circuit_breaker_name)

    # The breaker is consulted once per logical call: the retries below belong
    # to it (a HALF_OPEN probe's own retries must not be rejected as a second
    # probe).
    if circuit_breaker and not circuit_breaker.allow_request():
        logger.warning(f"[Retry] Circuit breaker {circuit_breaker_name} open, skipping call")
        raise CircuitBreakerError(f"Circuit breaker {circuit_breaker_name} is OPEN, skipping attempt")

    last_exception: BaseException | None = None
    # Whether the breaker got a verdict (success/failure/release). Anything
    # else leaving this function — a cancellation during the backoff sleep
    # included, which an ``except Exception`` never sees — releases the probe,
    # or a HALF_OPEN breaker would reject every later call.
    settled = False
    try:
        for attempt in range(max_retries + 1):
            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
            except Exception as e:
                if not is_retryable_error(e):
                    # Not the host's fault: no retry, no breaker failure.
                    raise
                last_exception = e

                # Don't retry on last attempt. The breaker records ONE failure per
                # logical call, after retries are exhausted — counting every attempt
                # would open it after a single flaky call (4 attempts >= 5 - 1).
                if attempt >= max_retries:
                    logger.error(
                        f"[Retry] Failed after {attempt + 1} attempts: {type(e).__name__}: {e}"
                    )
                    if circuit_breaker:
                        circuit_breaker._on_failure()
                        settled = True
                    break

                delay = min(initial_delay * (exponential_base ** attempt), max_delay)
                logger.warning(
                    f"[Retry] Attempt {attempt + 1}/{max_retries + 1} failed: "
                    f"{type(e).__name__}: {e}. Retrying in {delay:.2f}s..."
                )
                await asyncio.sleep(delay)
                continue

            if circuit_breaker:
                circuit_breaker._on_success()
                settled = True
            if attempt > 0:
                logger.info(f"[Retry] Success on attempt {attempt + 1}/{max_retries + 1}")
            return result
    finally:
        if circuit_breaker and not settled:
            circuit_breaker.release_probe()

    # All retries exhausted
    raise last_exception


def get_all_circuit_breaker_status() -> dict[str, dict[str, Any]]:
    """
    Get status of all circuit breakers for monitoring.

    Returns:
        Dictionary mapping breaker name to status dict
    """
    return {
        name: {
            "state": breaker.state.value,
            "failure_count": breaker.failure_count,
            "success_count": breaker.success_count,
            "failure_threshold": breaker.failure_threshold,
            "timeout_seconds": breaker.timeout,
            "last_failure": datetime.fromtimestamp(breaker.last_failure_time, UTC).isoformat()
                if breaker.last_failure_time else None
        }
        for name, breaker in _circuit_breakers.items()
    }

