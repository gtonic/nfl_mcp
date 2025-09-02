"""
Tests for the error handling utilities.

This module tests the standardized error handling decorators and utilities.
"""

import pytest
import httpx
from unittest.mock import AsyncMock

from nfl_mcp.errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors, handle_database_errors, handle_validation_error
)


class TestErrorResponseCreation:
    """Test error response creation utilities."""
    
    def test_create_error_response(self):
        """Test create_error_response function."""
        response = create_error_response(
            "Test error message",
            ErrorType.VALIDATION,
            {"data": "test"}
        )
        
        assert response["success"] is False
        assert response["error"] == "Test error message"
        assert response["error_type"] == ErrorType.VALIDATION
        assert response["data"] == "test"
    
    def test_create_success_response(self):
        """Test create_success_response function."""
        response = create_success_response({"data": "test", "count": 5})
        
        assert response["success"] is True
        assert response["error"] is None
        assert response["error_type"] is None
        assert response["data"] == "test"
        assert response["count"] == 5
    
    def test_handle_validation_error(self):
        """Test handle_validation_error function."""
        response = handle_validation_error(
            "Invalid input parameter",
            {"field": "value"}
        )
        
        assert response["success"] is False
        assert response["error"] == "Invalid input parameter"
        assert response["error_type"] == ErrorType.VALIDATION
        assert response["field"] == "value"


class TestHttpErrorDecorator:
    """Test the HTTP error handling decorator."""
    
    @pytest.mark.asyncio
    async def test_successful_function(self):
        """Test decorator with successful function."""
        @handle_http_errors(
            default_data={"items": []},
            operation_name="test operation"
        )
        async def test_func():
            return create_success_response({"items": ["item1", "item2"]})
        
        result = await test_func()
        assert result["success"] is True
        assert result["items"] == ["item1", "item2"]
    
    @pytest.mark.asyncio
    async def test_timeout_error(self):
        """Test decorator with timeout error."""
        @handle_http_errors(
            default_data={"items": []},
            operation_name="test operation"
        )
        async def test_func():
            raise httpx.TimeoutException("Request timed out")
        
        result = await test_func()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.TIMEOUT
        assert "Request timed out while test operation" in result["error"]
        assert result["items"] == []
    
    @pytest.mark.asyncio
    async def test_http_status_error(self):
        """Test decorator with HTTP status error."""
        # Create a mock response for the HTTP error
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_response.reason_phrase = "Not Found"
        
        @handle_http_errors(
            default_data={"data": None},
            operation_name="test operation"
        )
        async def test_func():
            raise httpx.HTTPStatusError("HTTP Error", request=None, response=mock_response)
        
        result = await test_func()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert "HTTP 404: Not Found" in result["error"]
        assert result["data"] is None


class TestDatabaseErrorDecorator:
    """Test the database error handling decorator."""
    
    def test_successful_function(self):
        """Test decorator with successful function."""
        @handle_database_errors(
            default_data={"records": []},
            operation_name="test database operation"
        )
        def test_func():
            return create_success_response({"records": ["record1"]})
        
        result = test_func()
        assert result["success"] is True
        assert result["records"] == ["record1"]
    
    def test_database_error(self):
        """Test decorator with database error."""
        @handle_database_errors(
            default_data={"records": []},
            operation_name="test database operation"
        )
        def test_func():
            raise Exception("Database connection failed")
        
        result = test_func()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.DATABASE
        assert "Error during test database operation" in result["error"]
        assert "Database connection failed" in result["error"]
        assert result["records"] == []


class TestErrorTypes:
    """Test error type constants."""
    
    def test_error_type_constants(self):
        """Test that error type constants are defined correctly."""
        assert ErrorType.VALIDATION == "validation_error"
        assert ErrorType.TIMEOUT == "timeout_error"
        assert ErrorType.HTTP == "http_error"
        assert ErrorType.DATABASE == "database_error"
        assert ErrorType.NETWORK == "network_error"
        assert ErrorType.UNEXPECTED == "unexpected_error"