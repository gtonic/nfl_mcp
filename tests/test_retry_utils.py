"""
Tests for retry and circuit breaker utilities.
"""

import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from nfl_mcp.retry_utils import (
    CircuitBreaker,
    CircuitState,
    CircuitBreakerError,
    retry_with_backoff,
    get_circuit_breaker,
    get_configurable_timeout,
    get_configurable_long_timeout,
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
        
        # Simulate failures
        for i in range(3):
            try:
                cb.call(lambda: 1/0)
            except ZeroDivisionError:
                pass
        
        assert cb.state == CircuitState.OPEN
        assert cb.failure_count >= 3
    
    def test_circuit_breaker_rejects_when_open(self):
        """Test circuit breaker rejects calls when OPEN."""
        cb = CircuitBreaker("test")
        cb.state = CircuitState.OPEN
        cb.last_failure_time = time.time()
        
        with pytest.raises(CircuitBreakerError):
            cb.call(lambda: "should fail")
    
    def test_circuit_breaker_closes_after_success(self):
        """Test circuit breaker closes after successful recovery."""
        cb = CircuitBreaker("test")
        cb.state = CircuitState.HALF_OPEN
        cb.success_threshold = 2
        
        # First success
        cb.call(lambda: "success")
        assert cb.state == CircuitState.HALF_OPEN
        
        # Second success should close
        cb.call(lambda: "success")
        assert cb.state == CircuitState.CLOSED
        assert cb.success_count == 0
    
    def test_circuit_breaker_reopens_on_half_open_failure(self):
        """Test circuit breaker reopens if failure occurs in HALF_OPEN."""
        cb = CircuitBreaker("test")
        cb.state = CircuitState.HALF_OPEN
        
        try:
            cb.call(lambda: 1/0)
        except ZeroDivisionError:
            pass
        
        assert cb.state == CircuitState.OPEN
    
    def test_circuit_breaker_manual_reset(self):
        """Test manual reset of circuit breaker."""
        cb = CircuitBreaker("test")
        cb.state = CircuitState.OPEN
        cb.failure_count = 10
        
        cb.reset()
        
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.last_failure_time is None
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_async_call(self):
        """Test circuit breaker works with async functions."""
        cb = CircuitBreaker("test")
        
        async def async_func():
            return "success"
        
        result = await cb.call_async(async_func)
        assert result == "success"
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_async_call_failure(self):
        """Test circuit breaker handles async failures."""
        cb = CircuitBreaker("test")
        cb.failure_threshold = 1
        
        async def async_func():
            raise ValueError("test error")
        
        with pytest.raises(ValueError):
            await cb.call_async(async_func)
        
        assert cb.failure_count == 1


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
            ValueError("fail 1"),
            ValueError("fail 2"),
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
        mock_func = AsyncMock(side_effect=ValueError("persistent error"))
        
        with pytest.raises(ValueError, match="persistent error"):
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
        mock_func.side_effect = [ValueError("fail"), "success"]
        
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
                raise ValueError("fail")
            return "success"
        
        result = await retry_with_backoff(
            sync_func,
            max_retries=2,
            initial_delay=0.01
        )
        
        assert result == "success"
        assert call_count[0] == 2


class TestConfigurableTimeouts:
    """Test configurable timeout functions."""
    
    def test_get_configurable_timeout_default(self, monkeypatch):
        """Test default timeout value."""
        monkeypatch.delenv("NFL_MCP_API_TIMEOUT", raising=False)
        timeout = get_configurable_timeout()
        assert timeout == 30.0
    
    def test_get_configurable_timeout_custom(self, monkeypatch):
        """Test custom timeout from environment."""
        monkeypatch.setenv("NFL_MCP_API_TIMEOUT", "45.0")
        timeout = get_configurable_timeout()
        assert timeout == 45.0
    
    def test_get_configurable_long_timeout_default(self, monkeypatch):
        """Test default long timeout value."""
        monkeypatch.delenv("NFL_MCP_API_LONG_TIMEOUT", raising=False)
        timeout = get_configurable_long_timeout()
        assert timeout == 60.0
    
    def test_get_configurable_long_timeout_custom(self, monkeypatch):
        """Test custom long timeout from environment."""
        monkeypatch.setenv("NFL_MCP_API_LONG_TIMEOUT", "90.0")
        timeout = get_configurable_long_timeout()
        assert timeout == 90.0


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
