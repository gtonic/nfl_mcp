"""
Error handling utilities for the NFL MCP Server.

This module provides standardized error handling utilities, decorators, and 
response formats to ensure consistent error management across all tools.
"""

import logging
import httpx
from functools import wraps
from typing import Any, Dict, Optional, Callable, Union


# Configure logging for error tracking
logger = logging.getLogger(__name__)


class ErrorType:
    """Standard error type constants."""
    VALIDATION = "validation_error"
    TIMEOUT = "timeout_error"
    HTTP = "http_error"
    DATABASE = "database_error"
    NETWORK = "network_error"
    UNEXPECTED = "unexpected_error"


def create_error_response(
    error_message: str,
    error_type: str = ErrorType.UNEXPECTED,
    data: Optional[Dict[str, Any]] = None,
    success: bool = False
) -> Dict[str, Any]:
    """
    Create a standardized error response.
    
    Args:
        error_message: Human-readable error description
        error_type: Type of error (see ErrorType constants)
        data: Tool-specific data to include in response
        success: Whether the operation was successful
        
    Returns:
        Standardized error response dictionary
    """
    response = {
        "success": success,
        "error": error_message,
        "error_type": error_type
    }
    
    # Add tool-specific data if provided
    if data:
        response.update(data)
    
    # Log the error for debugging
    if not success:
        logger.error(f"Error ({error_type}): {error_message}")
    
    return response


def create_success_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a standardized success response.
    
    Args:
        data: Tool-specific data to include in response
        
    Returns:
        Standardized success response dictionary
    """
    response = {
        "success": True,
        "error": None,
        "error_type": None
    }
    response.update(data)
    return response


def handle_http_errors(
    default_data: Optional[Dict[str, Any]] = None,
    operation_name: str = "operation"
) -> Callable:
    """
    Decorator for standardizing HTTP API error handling.
    
    Args:
        default_data: Default data structure to return on errors
        operation_name: Name of the operation for error messages
        
    Returns:
        Decorator function
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Dict[str, Any]:
            try:
                result = await func(*args, **kwargs)
                return result
                
            except httpx.TimeoutException:
                return create_error_response(
                    f"Request timed out while {operation_name}",
                    ErrorType.TIMEOUT,
                    default_data or {}
                )
                
            except httpx.HTTPStatusError as e:
                return create_error_response(
                    f"HTTP {e.response.status_code}: {e.response.reason_phrase}",
                    ErrorType.HTTP,
                    default_data or {}
                )
                
            except httpx.NetworkError as e:
                return create_error_response(
                    f"Network error while {operation_name}: {str(e)}",
                    ErrorType.NETWORK,
                    default_data or {}
                )
                
            except Exception as e:
                return create_error_response(
                    f"Unexpected error during {operation_name}: {str(e)}",
                    ErrorType.UNEXPECTED,
                    default_data or {}
                )
                
        return wrapper
    return decorator


def handle_database_errors(
    default_data: Optional[Dict[str, Any]] = None,
    operation_name: str = "database operation"
) -> Callable:
    """
    Decorator for standardizing database operation error handling.
    
    Args:
        default_data: Default data structure to return on errors
        operation_name: Name of the operation for error messages
        
    Returns:
        Decorator function
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Dict[str, Any]:
            try:
                result = func(*args, **kwargs)
                return result
                
            except Exception as e:
                return create_error_response(
                    f"Error during {operation_name}: {str(e)}",
                    ErrorType.DATABASE,
                    default_data or {}
                )
                
        return wrapper
    return decorator


def handle_validation_error(
    error_message: str,
    default_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Create a standardized validation error response.
    
    Args:
        error_message: Validation error message
        default_data: Default data structure to return
        
    Returns:
        Standardized validation error response
    """
    return create_error_response(
        error_message,
        ErrorType.VALIDATION,
        default_data or {}
    )


class RetryConfig:
    """Configuration for retry strategies."""
    
    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
        retry_on_timeout: bool = True,
        retry_on_server_error: bool = True
    ):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.retry_on_timeout = retry_on_timeout
        self.retry_on_server_error = retry_on_server_error


def with_retry(
    retry_config: Optional[RetryConfig] = None,
    default_data: Optional[Dict[str, Any]] = None,
    operation_name: str = "operation"
) -> Callable:
    """
    Decorator that adds retry logic with exponential backoff.
    
    Args:
        retry_config: Retry configuration
        default_data: Default data structure to return on final failure
        operation_name: Name of the operation for error messages
        
    Returns:
        Decorator function
    """
    if retry_config is None:
        retry_config = RetryConfig()
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Dict[str, Any]:
            import asyncio
            
            last_exception = None
            
            for attempt in range(retry_config.max_attempts):
                try:
                    result = await func(*args, **kwargs)
                    return result
                    
                except httpx.TimeoutException as e:
                    last_exception = e
                    if not retry_config.retry_on_timeout or attempt == retry_config.max_attempts - 1:
                        break
                        
                except httpx.HTTPStatusError as e:
                    last_exception = e
                    # Retry on 5xx server errors
                    if (not retry_config.retry_on_server_error or 
                        e.response.status_code < 500 or 
                        attempt == retry_config.max_attempts - 1):
                        break
                        
                except Exception as e:
                    last_exception = e
                    break  # Don't retry on unexpected errors
                
                # Calculate delay for next attempt
                if attempt < retry_config.max_attempts - 1:
                    delay = min(
                        retry_config.base_delay * (retry_config.backoff_factor ** attempt),
                        retry_config.max_delay
                    )
                    logger.info(f"Retrying {operation_name} in {delay:.1f}s (attempt {attempt + 1}/{retry_config.max_attempts})")
                    await asyncio.sleep(delay)
            
            # Handle the final exception
            if isinstance(last_exception, httpx.TimeoutException):
                return create_error_response(
                    f"Request timed out while {operation_name} (after {retry_config.max_attempts} attempts)",
                    ErrorType.TIMEOUT,
                    default_data or {}
                )
            elif isinstance(last_exception, httpx.HTTPStatusError):
                return create_error_response(
                    f"HTTP {last_exception.response.status_code}: {last_exception.response.reason_phrase} (after {retry_config.max_attempts} attempts)",
                    ErrorType.HTTP,
                    default_data or {}
                )
            else:
                return create_error_response(
                    f"Unexpected error during {operation_name}: {str(last_exception)}",
                    ErrorType.UNEXPECTED,
                    default_data or {}
                )
                
        return wrapper
    return decorator