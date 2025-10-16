"""
Integration tests for API robustness features.

Tests the retry, circuit breaker, and validation features working together.
"""

import pytest
from unittest.mock import AsyncMock, patch
from nfl_mcp.retry_utils import (
    CircuitBreaker,
    CircuitState,
    retry_with_backoff,
    get_circuit_breaker,
)
from nfl_mcp.response_validation import (
    validate_snap_count_response,
    validate_response_and_log,
)


class TestRetryAndValidationIntegration:
    """Test retry and validation working together."""
    
    @pytest.mark.asyncio
    async def test_successful_fetch_with_validation(self):
        """Test successful fetch with validation passes."""
        # Mock fetch function that returns valid data
        async def mock_fetch():
            return {
                "123": {"snaps": 50, "snap_pct": 75.0},
                "456": {"snaps": 40, "snap_pct": 60.0},
            }
        
        # Execute with retry
        result = await retry_with_backoff(mock_fetch, max_retries=2)
        
        # Validate result
        validation = validate_snap_count_response(result)
        assert validation.is_valid()
        assert len(result) == 2
    
    @pytest.mark.asyncio
    async def test_retry_then_validate_success(self):
        """Test retry recovers from failure then validation passes."""
        call_count = [0]
        
        async def mock_fetch():
            call_count[0] += 1
            if call_count[0] < 2:
                raise ValueError("Temporary error")
            return {
                "123": {"snaps": 50, "snap_pct": 75.0},
            }
        
        # Execute with retry
        result = await retry_with_backoff(
            mock_fetch,
            max_retries=2,
            initial_delay=0.01
        )
        
        # Should have retried once
        assert call_count[0] == 2
        
        # Validate result
        validation = validate_snap_count_response(result)
        assert validation.is_valid()
    
    @pytest.mark.asyncio
    async def test_validation_rejects_bad_data_after_retry(self):
        """Test validation rejects bad data even after successful retry."""
        # Mock fetch that returns invalid data
        async def mock_fetch():
            return []  # Wrong type (should be dict)
        
        # Execute with retry
        result = await retry_with_backoff(mock_fetch, max_retries=1)
        
        # Validate result
        validation = validate_snap_count_response(result)
        assert not validation.is_valid()
    
    @pytest.mark.asyncio
    async def test_partial_data_accepted_with_warnings(self):
        """Test partial data with warnings is accepted."""
        # Mock fetch that returns partial data
        async def mock_fetch():
            # Only 1 of 10 players has snap data
            data = {str(i): {} for i in range(10)}
            data["0"] = {"snaps": 50, "snap_pct": 75.0}
            return data
        
        # Execute with retry
        result = await retry_with_backoff(mock_fetch, max_retries=1)
        
        # Validate result - should pass with warnings
        validation = validate_snap_count_response(result)
        assert validation.is_valid()
        assert len(validation.warnings) > 0


class TestCircuitBreakerIntegration:
    """Test circuit breaker integration."""
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_failures(self):
        """Test circuit breaker opens after threshold failures."""
        cb = CircuitBreaker("test_integration")
        cb.failure_threshold = 3
        
        # Failing function
        async def failing_fetch():
            raise ValueError("API error")
        
        # Execute multiple times until circuit opens
        # Need to catch the exception from retry_with_backoff
        for i in range(3):
            try:
                # Use circuit breaker directly, not retry_with_backoff
                await cb.call_async(failing_fetch)
            except ValueError:
                pass
        
        # Circuit should be open now
        assert cb.state == CircuitState.OPEN
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_prevents_calls_when_open(self):
        """Test circuit breaker prevents calls when open."""
        cb = get_circuit_breaker("test_prevent")
        cb.state = CircuitState.OPEN
        cb.last_failure_time = 999999999999  # Far future
        
        call_count = [0]
        
        async def mock_fetch():
            call_count[0] += 1
            return "success"
        
        # Should fail immediately without calling function
        from nfl_mcp.retry_utils import CircuitBreakerError
        with pytest.raises(CircuitBreakerError):
            await retry_with_backoff(
                mock_fetch,
                circuit_breaker_name="test_prevent"
            )
        
        # Function should not have been called
        assert call_count[0] == 0
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_recovers_after_success(self):
        """Test circuit breaker recovers after successful calls."""
        cb = CircuitBreaker("test_recover")
        cb.state = CircuitState.HALF_OPEN
        cb.success_threshold = 1  # Only need 1 success to close
        
        async def mock_fetch():
            return "success"
        
        # Success should close circuit
        result = await cb.call_async(mock_fetch)
        assert result == "success"
        assert cb.state == CircuitState.CLOSED


class TestEndToEndScenarios:
    """Test realistic end-to-end scenarios."""
    
    @pytest.mark.asyncio
    async def test_transient_api_failure_recovers(self):
        """Test system recovers from transient API failure."""
        call_count = [0]
        
        async def flaky_api():
            call_count[0] += 1
            # Fail first 2 times, then succeed
            if call_count[0] < 3:
                raise ConnectionError("Transient network error")
            return {
                "123": {"snaps": 50, "snap_pct": 75.0},
                "456": {"snaps": 40, "snap_pct": 60.0},
            }
        
        # Execute with retry and circuit breaker
        result = await retry_with_backoff(
            flaky_api,
            max_retries=3,
            initial_delay=0.01,
            circuit_breaker_name="flaky_test"
        )
        
        # Should have succeeded after retries
        assert call_count[0] == 3
        assert len(result) == 2
        
        # Validation should pass
        validation = validate_snap_count_response(result)
        assert validation.is_valid()
    
    @pytest.mark.asyncio
    async def test_persistent_failure_opens_circuit(self):
        """Test persistent failures open circuit breaker."""
        cb = get_circuit_breaker("persistent_test")
        cb.failure_threshold = 2
        cb.reset()  # Start fresh
        
        async def always_fails():
            raise ConnectionError("API down")
        
        # Try multiple times
        for i in range(3):
            try:
                await retry_with_backoff(
                    always_fails,
                    max_retries=0,
                    circuit_breaker_name="persistent_test"
                )
            except (ConnectionError, Exception):
                pass
        
        # Circuit should be open
        assert cb.state == CircuitState.OPEN
    
    @pytest.mark.asyncio
    async def test_degraded_data_quality_handled(self):
        """Test system handles degraded data quality gracefully."""
        async def degraded_api():
            # Return partial data with low coverage
            # Need enough data to trigger sampling (at least 10 items)
            data = {}
            for i in range(20):
                if i < 2:  # Only 10% have snap data
                    data[str(i)] = {"snaps": 50, "snap_pct": 75.0}
                else:
                    data[str(i)] = {}
            return data
        
        # Execute
        result = await retry_with_backoff(
            degraded_api,
            max_retries=1
        )
        
        # Validation should pass with warnings about low coverage
        validation = validate_snap_count_response(result)
        assert validation.is_valid()
        # Should have warnings about low snap coverage
        assert len(validation.warnings) > 0
        assert any("coverage" in w.lower() for w in validation.warnings)
        
        # Data is still usable
        assert len(result) == 20
