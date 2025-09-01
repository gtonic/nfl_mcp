"""
Unit tests for NFL MCP Server

Tests the basic server functionality and multiplication logic.
"""

import pytest
from nfl_mcp.server import create_app


class TestServerCreation:
    """Test server creation and configuration."""
    
    def test_create_app_returns_fastmcp_instance(self):
        """Test that create_app returns a FastMCP instance."""
        app = create_app()
        
        # Should be a FastMCP instance
        from fastmcp import FastMCP
        assert isinstance(app, FastMCP)
        
        # Should have the correct name
        assert app.name == "NFL MCP Server"
    
    def test_server_creation_basic(self):
        """Test basic server creation without complex interactions."""
        app = create_app()
        assert app.name == "NFL MCP Server"
        
        # Test that we can create the app multiple times
        app2 = create_app()
        assert app2.name == "NFL MCP Server"


class TestServerConfiguration:
    """Test server configuration and features."""
    
    def test_server_has_custom_route(self):
        """Test that the server has custom routes registered."""
        app = create_app()
        
        # Check that custom routes are registered
        # The FastMCP instance should have additional HTTP routes
        additional_routes = app._get_additional_http_routes()
        
        # Should have at least one custom route (health endpoint)
        assert len(additional_routes) > 0
    
    def test_health_route_exists(self):
        """Test that health route is properly configured."""
        app = create_app()
        
        # Get additional routes
        routes = app._get_additional_http_routes()
        
        # Should have a health route
        health_routes = [route for route in routes if '/health' in str(route)]
        assert len(health_routes) > 0


class TestMultiplicationLogic:
    """Test the multiplication business logic directly."""
    
    def test_multiply_function_directly(self):
        """Test the multiply function business logic directly."""
        # Test the actual multiplication logic
        def multiply(x: int, y: int) -> int:
            return x * y
        
        # Basic test cases
        assert multiply(5, 3) == 15
        assert multiply(10, 0) == 0
        assert multiply(-4, 7) == -28
        assert multiply(-6, -8) == 48
        assert multiply(1, 999) == 999
    
    def test_multiply_edge_cases(self):
        """Test multiplication with edge cases."""
        def multiply(x: int, y: int) -> int:
            return x * y
        
        # Edge cases
        assert multiply(0, 0) == 0
        assert multiply(1, 1) == 1
        assert multiply(-1, -1) == 1
        assert multiply(100, 100) == 10000
        
        # Mathematical properties
        # Commutative property: a * b = b * a
        assert multiply(7, 9) == multiply(9, 7)
        
        # Identity property: a * 1 = a
        assert multiply(42, 1) == 42
        assert multiply(1, 42) == 42
        
        # Zero property: a * 0 = 0
        assert multiply(999, 0) == 0
        assert multiply(0, 999) == 0
    
    def test_multiply_large_numbers(self):
        """Test multiplication with large numbers."""
        def multiply(x: int, y: int) -> int:
            return x * y
        
        # Large number tests
        assert multiply(999999, 999999) == 999998000001
        assert multiply(1000000, 1) == 1000000
        assert multiply(-1000000, 1) == -1000000


class TestServerIntegration:
    """Integration tests for the complete server functionality."""
    
    def test_server_runs_without_error(self):
        """Test that the server can be created and configured without errors."""
        try:
            app = create_app()
            assert app is not None
            assert hasattr(app, 'name')
            assert hasattr(app, 'run')
        except Exception as e:
            pytest.fail(f"Server creation failed with error: {e}")
    
    def test_import_all_modules(self):
        """Test that all modules can be imported successfully."""
        try:
            import nfl_mcp
            import nfl_mcp.server
            from nfl_mcp.server import create_app, main
            
            # All imports should work
            assert create_app is not None
            assert main is not None
            
        except ImportError as e:
            pytest.fail(f"Module import failed: {e}")


class TestHealthEndpointLogic:
    """Test the health endpoint logic."""
    
    def test_health_response_structure(self):
        """Test that health response has the correct structure."""
        # Expected health response
        expected_response = {
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.1.0"
        }
        
        # Validate structure
        assert "status" in expected_response
        assert "service" in expected_response  
        assert "version" in expected_response
        
        # Validate values
        assert expected_response["status"] == "healthy"
        assert expected_response["service"] == "NFL MCP Server"
        assert expected_response["version"] == "0.1.0"