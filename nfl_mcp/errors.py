"""
Error handling utilities for the NFL MCP Server.

This module provides standardized error handling utilities, decorators, and
response formats to ensure consistent error management across all tools.
"""

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

import httpx

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
    ACCESS_DENIED = "access_denied_error"
    ROSTER_PRIVATE = "roster_private_error"
    # Referenced by sleeper_tools/nfl_tools error paths; were missing here, so
    # hitting those paths raised AttributeError (surfaced by the mypy pass).
    API_ERROR = "api_error"
    NOT_FOUND = "not_found_error"


def create_error_response(
    error_message: str,
    error_type: str = ErrorType.UNEXPECTED,
    data: dict[str, Any] | None = None,
    success: bool = False
) -> dict[str, Any]:
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


def create_success_response(data: dict[str, Any]) -> dict[str, Any]:
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
    default_data: dict[str, Any] | None = None,
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
        async def wrapper(*args, **kwargs) -> dict[str, Any]:
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
                    f"Network error while {operation_name}: {e!s}",
                    ErrorType.NETWORK,
                    default_data or {}
                )

            except Exception as e:
                return create_error_response(
                    f"Unexpected error during {operation_name}: {e!s}",
                    ErrorType.UNEXPECTED,
                    default_data or {}
                )

        return wrapper
    return decorator


def handle_database_errors(
    default_data: dict[str, Any] | None = None,
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
        def wrapper(*args, **kwargs) -> dict[str, Any]:
            try:
                result = func(*args, **kwargs)
                return result

            except Exception as e:
                return create_error_response(
                    f"Error during {operation_name}: {e!s}",
                    ErrorType.DATABASE,
                    default_data or {}
                )

        return wrapper
    return decorator


def handle_validation_error(
    error_message: str,
    default_data: dict[str, Any] | None = None
) -> dict[str, Any]:
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
