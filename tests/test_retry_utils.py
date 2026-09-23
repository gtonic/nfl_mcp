"""
Tests for retry and circuit breaker utilities.
"""

import time
from unittest.mock import AsyncMock

import httpx
import pytest

from nfl_mcp.retry_utils import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitState,
    RetryableHTTPStatus,
    get_circuit_breaker,
    retry_with_backoff,
)


class TestCircuitBreaker:
    """Test circuit breaker functionality."""

    def test_circuit_breaker_initialization(self):
        """Test circuit breaker is initialized in CLOSED state."""
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0

    def test_circuit_breaker_opens_on_failures(self):
        """Test circuit breaker opens after threshold failures."""
        cb = CircuitBreaker("test")
        cb.failure_threshold = 3  # Lower threshold for testing

        for _i in range(3):
            cb._on_failure()

        assert cb.state == CircuitState.OPEN
        assert cb.failure_count >= 3

    def test_circuit_breaker_rejects_when_open(self):
        """Test circuit breaker rejects calls when OPEN."""
        cb = CircuitBreaker("test")
        cb.state = CircuitState.OPEN
        cb.last_failure_time = time.time()

        assert cb.allow_request() is False

    def test_circuit_breaker_closes_after_success(self):
        """Test circuit breaker closes after successful recovery."""
        cb = CircuitBreaker("test")
        cb.state = CircuitState.HALF_OPEN
        cb.success_threshold = 2

        assert cb.allow_request()
        cb._on_success()
        assert cb.state == CircuitState.HALF_OPEN

        assert cb.allow_request()
        cb._on_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.success_count == 0

    def test_circuit_breaker_reopens_on_half_open_failure(self):
        """Test circuit breaker reopens if failure occurs in HALF_OPEN."""
        cb = CircuitBreaker("test")
        cb.state = CircuitState.HALF_OPEN

        assert cb.allow_request()
        cb._on_failure()

        assert cb.state == CircuitState.OPEN

    def test_half_open_admits_a_single_probe(self):
        """After the timeout exactly one caller gets through until it reports back."""
        cb = CircuitBreaker("test")
        cb.state = CircuitState.OPEN
        cb.last_failure_time = time.time() - cb.timeout - 1

        assert cb.allow_request() is True        # the probe
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allow_request() is False       # everyone else waits
        assert cb.allow_request() is False

        cb._on_success()                         # probe reported back
        assert cb.allow_request() is True        # next probe

    def test_circuit_breaker_manual_reset(self):
        """Test manual reset of circuit breaker."""
        cb = CircuitBreaker("test")
        cb.state = CircuitState.OPEN
        cb.failure_count = 10

        cb.reset()

        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.last_failure_time is None


class TestRetryWithBackoff:
    """Test retry with exponential backoff."""

    @pytest.mark.asyncio
    async def test_retry_success_on_first_attempt(self):
        """Test successful call on first attempt."""
        mock_func = AsyncMock(return_value="success")

        result = await retry_with_backoff(mock_func, max_retries=3)

        assert result == "success"
        assert mock_func.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_success_after_failures(self):
        """Test successful call after some failures."""
        mock_func = AsyncMock()
        # Fail twice, then succeed
        mock_func.side_effect = [
            httpx.ConnectError("fail 1"),
            httpx.ReadTimeout("fail 2"),
            "success"
        ]

        result = await retry_with_backoff(
            mock_func,
            max_retries=3,
            initial_delay=0.01  # Fast for testing
        )

        assert result == "success"
        assert mock_func.call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausts_retries(self):
        """Test all retries are exhausted on persistent failure."""
        mock_func = AsyncMock(side_effect=httpx.ConnectError("persistent error"))

        with pytest.raises(httpx.ConnectError, match="persistent error"):
            await retry_with_backoff(
                mock_func,
                max_retries=2,
                initial_delay=0.01
            )

        assert mock_func.call_count == 3  # Initial + 2 retries

    @pytest.mark.asyncio
    async def test_retry_exponential_backoff(self):
        """Test exponential backoff timing."""
        mock_func = AsyncMock()
        mock_func.side_effect = [httpx.ConnectError("fail"), "success"]

        start = time.time()
        await retry_with_backoff(
            mock_func,
            max_retries=1,
            initial_delay=0.1,
            exponential_base=2.0
        )
        elapsed = time.time() - start

        # Should have delayed ~0.1 seconds
        assert elapsed >= 0.1
        assert elapsed < 0.3  # Allow some margin

    @pytest.mark.asyncio
    async def test_retry_with_circuit_breaker(self):
        """Test retry with circuit breaker integration."""
        mock_func = AsyncMock(return_value="success")

        result = await retry_with_backoff(
            mock_func,
            circuit_breaker_name="test_cb",
            max_retries=2
        )

        assert result == "success"

        # Circuit breaker should exist
        cb = get_circuit_breaker("test_cb")
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_retry_stops_on_open_circuit(self):
        """Test retry stops when circuit breaker is open."""
        # Pre-open the circuit
        cb = get_circuit_breaker("test_cb_open")
        cb.state = CircuitState.OPEN
        cb.last_failure_time = time.time()

        mock_func = AsyncMock(return_value="success")

        with pytest.raises(CircuitBreakerError):
            await retry_with_backoff(
                mock_func,
                circuit_breaker_name="test_cb_open",
                max_retries=2
            )

        # Should not have called the function
        assert mock_func.call_count == 0

    @pytest.mark.asyncio
    async def test_retry_with_non_async_function(self):
        """Test retry works with synchronous functions."""
        call_count = [0]

        def sync_func():
            call_count[0] += 1
            if call_count[0] < 2:
                raise httpx.ConnectError("fail")
            return "success"

        result = await retry_with_backoff(
            sync_func,
            max_retries=2,
            initial_delay=0.01
        )

        assert result == "success"
        assert call_count[0] == 2


class TestCircuitBreakerRegistry:
    """Test circuit breaker registry."""

    def test_get_circuit_breaker_creates_new(self):
        """Test getting circuit breaker creates new one if not exists."""
        cb = get_circuit_breaker("new_test_cb")
        assert cb is not None
        assert cb.name == "new_test_cb"
        assert cb.state == CircuitState.CLOSED

    def test_get_circuit_breaker_returns_existing(self):
        """Test getting circuit breaker returns existing instance."""
        cb1 = get_circuit_breaker("existing_cb")
        cb1.failure_count = 5

        cb2 = get_circuit_breaker("existing_cb")
        assert cb2 is cb1
        assert cb2.failure_count == 5


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.test/x")
    return httpx.HTTPStatusError(f"HTTP {code}", request=req,
                                 response=httpx.Response(code, request=req))


class TestWhatIsRetried:
    """Only transport failures and 5xx/429 are retried or count against the host."""

    @pytest.mark.asyncio
    async def test_a_bug_is_not_retried_or_counted(self):
        cb = get_circuit_breaker("not_counted")
        cb.reset()
        mock_func = AsyncMock(side_effect=KeyError("parse bug"))

        with pytest.raises(KeyError):
            await retry_with_backoff(mock_func, max_retries=3, initial_delay=0.01,
                                     circuit_breaker_name="not_counted")

        assert mock_func.call_count == 1
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_a_404_is_not_retried(self):
        mock_func = AsyncMock(side_effect=_status_error(404))
        with pytest.raises(httpx.HTTPStatusError):
            await retry_with_backoff(mock_func, max_retries=3, initial_delay=0.01)
        assert mock_func.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [500, 503, 429])
    async def test_server_errors_are_retried(self, code):
        mock_func = AsyncMock(side_effect=[_status_error(code), "ok"])
        assert await retry_with_backoff(mock_func, max_retries=2, initial_delay=0.01) == "ok"
        assert mock_func.call_count == 2

    @pytest.mark.asyncio
    async def test_retryable_http_status_counts_once(self):
        cb = get_circuit_breaker("counted_once")
        cb.reset()
        mock_func = AsyncMock(side_effect=RetryableHTTPStatus(502))
        with pytest.raises(RetryableHTTPStatus):
            await retry_with_backoff(mock_func, max_retries=2, initial_delay=0.01,
                                     circuit_breaker_name="counted_once")
        assert mock_func.call_count == 3
        assert cb.failure_count == 1

    @pytest.mark.asyncio
    async def test_a_non_host_failure_releases_the_probe(self):
        cb = get_circuit_breaker("probe_release")
        cb.reset()
        cb.state = CircuitState.HALF_OPEN
        with pytest.raises(KeyError):
            await retry_with_backoff(AsyncMock(side_effect=KeyError("x")), max_retries=0,
                                     circuit_breaker_name="probe_release")
        # The probe slot is free again for the next caller.
        assert cb.allow_request() is True
