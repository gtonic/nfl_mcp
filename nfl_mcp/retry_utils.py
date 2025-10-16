"""
Retry and Circuit Breaker utilities for robust API calls.

This module provides:
- Configurable retry logic with exponential backoff
- Circuit breaker pattern to prevent cascading failures
- Configurable timeouts via environment variables
- Partial data return on errors
"""

import asyncio
import logging
import time
from typing import Optional, Callable, Any, Dict
from datetime import datetime, UTC
from enum import Enum
import os

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
        self.last_failure_time: Optional[float] = None
        
        # Configuration from environment
        self.failure_threshold = int(os.getenv("NFL_MCP_CIRCUIT_FAILURE_THRESHOLD", "5"))
        self.timeout = int(os.getenv("NFL_MCP_CIRCUIT_TIMEOUT", "60"))
        self.success_threshold = int(os.getenv("NFL_MCP_CIRCUIT_SUCCESS_THRESHOLD", "2"))
        
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function through circuit breaker.
        
        Args:
            func: Function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments
            
        Returns:
            Function result
            
        Raises:
            CircuitBreakerError: If circuit is open
        """
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                logger.info(f"[Circuit Breaker {self.name}] Attempting reset (HALF_OPEN)")
                self.state = CircuitState.HALF_OPEN
            else:
                raise CircuitBreakerError(f"Circuit breaker {self.name} is OPEN")
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e
    
    async def call_async(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute async function through circuit breaker.
        
        Args:
            func: Async function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments
            
        Returns:
            Function result
            
        Raises:
            CircuitBreakerError: If circuit is open
        """
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                logger.info(f"[Circuit Breaker {self.name}] Attempting reset (HALF_OPEN)")
                self.state = CircuitState.HALF_OPEN
            else:
                raise CircuitBreakerError(f"Circuit breaker {self.name} is OPEN")
        
        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e
    
    def _on_success(self):
        """Handle successful call."""
        self.failure_count = 0
        
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                logger.info(f"[Circuit Breaker {self.name}] Closing circuit (recovered)")
                self.state = CircuitState.CLOSED
                self.success_count = 0
    
    def _on_failure(self):
        """Handle failed call."""
        self.failure_count += 1
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
        logger.info(f"[Circuit Breaker {self.name}] Manual reset")


class CircuitBreakerError(Exception):
    """Exception raised when circuit breaker is open."""
    pass


# Global circuit breakers for different API endpoints
_circuit_breakers: Dict[str, CircuitBreaker] = {}


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
    max_retries: Optional[int] = None,
    initial_delay: Optional[float] = None,
    max_delay: Optional[float] = None,
    exponential_base: float = 2.0,
    circuit_breaker_name: Optional[str] = None,
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
    
    last_exception = None
    delay = initial_delay
    
    for attempt in range(max_retries + 1):
        try:
            # Check circuit breaker if enabled
            if circuit_breaker and circuit_breaker.state == CircuitState.OPEN:
                if not circuit_breaker._should_attempt_reset():
                    raise CircuitBreakerError(
                        f"Circuit breaker {circuit_breaker_name} is OPEN, skipping attempt"
                    )
                logger.info(f"[Retry] Circuit breaker {circuit_breaker_name} attempting reset")
                circuit_breaker.state = CircuitState.HALF_OPEN
            
            # Execute function
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            
            # Success - update circuit breaker
            if circuit_breaker:
                circuit_breaker._on_success()
            
            # Log retry success if this wasn't first attempt
            if attempt > 0:
                logger.info(f"[Retry] Success on attempt {attempt + 1}/{max_retries + 1}")
            
            return result
            
        except CircuitBreakerError as e:
            # Don't retry if circuit breaker is open
            logger.warning(f"[Retry] Circuit breaker open, stopping retries: {e}")
            raise e
            
        except Exception as e:
            last_exception = e
            
            # Update circuit breaker on failure
            if circuit_breaker:
                circuit_breaker._on_failure()
            
            # Don't retry on last attempt
            if attempt >= max_retries:
                logger.error(
                    f"[Retry] Failed after {attempt + 1} attempts: {type(e).__name__}: {e}"
                )
                break
            
            # Calculate delay with exponential backoff
            delay = min(initial_delay * (exponential_base ** attempt), max_delay)
            
            logger.warning(
                f"[Retry] Attempt {attempt + 1}/{max_retries + 1} failed: "
                f"{type(e).__name__}: {e}. Retrying in {delay:.2f}s..."
            )
            
            await asyncio.sleep(delay)
    
    # All retries exhausted
    raise last_exception


def get_configurable_timeout() -> float:
    """
    Get configurable timeout from environment.
    
    Environment variables:
    - NFL_MCP_API_TIMEOUT: API timeout in seconds (default: 30.0)
    
    Returns:
        Timeout in seconds
    """
    return float(os.getenv("NFL_MCP_API_TIMEOUT", "30.0"))


def get_configurable_long_timeout() -> float:
    """
    Get configurable long timeout from environment.
    
    Environment variables:
    - NFL_MCP_API_LONG_TIMEOUT: Long API timeout in seconds (default: 60.0)
    
    Returns:
        Timeout in seconds
    """
    return float(os.getenv("NFL_MCP_API_LONG_TIMEOUT", "60.0"))
