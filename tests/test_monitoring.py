"""
Tests for monitoring and observability features.

This module tests the logging, metrics, health checks, and middleware functionality.
"""

import pytest
import asyncio
import time
import json
from unittest.mock import patch, MagicMock
from httpx import AsyncClient

from nfl_mcp.logging_config import get_logger, setup_logging, log_with_context
from nfl_mcp.metrics import get_metrics_collector, timing_decorator, time_operation
from nfl_mcp.monitoring import get_health_checker, HealthChecker
from nfl_mcp.middleware import RequestLoggingMiddleware
from nfl_mcp.server import create_app


class TestLoggingConfiguration:
    """Test structured logging configuration."""
    
    def test_logger_creation(self):
        """Test that loggers are created correctly."""
        logger = get_logger("test_logger")
        assert logger.name == "test_logger"
    
    def test_log_with_context(self):
        """Test logging with additional context."""
        logger = get_logger("test_context")
        
        # This should not raise an exception
        log_with_context(
            logger,
            "info",
            "Test message with context",
            user_id="123",
            request_id="abc-def"
        )
    
    def test_setup_logging_without_file(self):
        """Test logging setup without file logging."""
        setup_logging(enable_file_logging=False)
        logger = get_logger("test_no_file")
        logger.info("Test message")


class TestMetricsCollection:
    """Test metrics collection functionality."""
    
    def setUp(self):
        """Set up fresh metrics collector for each test."""
        # Reset global collector state
        from nfl_mcp.metrics import _metrics_collector
        _metrics_collector._counters.clear()
        _metrics_collector._gauges.clear()
        _metrics_collector._histograms.clear()
        _metrics_collector._timings.clear()
        _metrics_collector._summaries.clear()
        _metrics_collector._labels.clear()
    
    def test_counter_increment(self):
        """Test counter metric increments."""
        metrics = get_metrics_collector()
        
        metrics.increment_counter("test_counter", 1, test_label="value1")
        metrics.increment_counter("test_counter", 2, test_label="value1")
        
        metrics_data = metrics.get_metrics()
        assert "test_counter|test_label=value1" in metrics_data["counters"]
        assert metrics_data["counters"]["test_counter|test_label=value1"] == 3
    
    def test_gauge_setting(self):
        """Test gauge metric setting."""
        metrics = get_metrics_collector()
        
        metrics.set_gauge("test_gauge", 42.5, environment="test")
        
        metrics_data = metrics.get_metrics()
        assert "test_gauge|environment=test" in metrics_data["gauges"]
        assert metrics_data["gauges"]["test_gauge|environment=test"] == 42.5
    
    def test_histogram_recording(self):
        """Test histogram metric recording."""
        metrics = get_metrics_collector()
        
        for value in [1, 2, 3, 4, 5]:
            metrics.record_histogram("test_histogram", value)
        
        metrics_data = metrics.get_metrics()
        assert "test_histogram" in metrics_data["histograms"]
        assert metrics_data["histograms"]["test_histogram"]["count"] == 5
    
    def test_timing_recording(self):
        """Test timing metric recording."""
        metrics = get_metrics_collector()
        
        metrics.record_timing("test_timing", 100.5)
        metrics.record_timing("test_timing", 200.5)
        
        metrics_data = metrics.get_metrics()
        assert "test_timing" in metrics_data["timings"]
        assert metrics_data["timings"]["test_timing"]["count"] == 2
    
    def test_prometheus_format(self):
        """Test Prometheus format output."""
        metrics = get_metrics_collector()
        
        metrics.increment_counter("http_requests", 5, method="GET")
        metrics.set_gauge("memory_usage", 75.5)
        
        prometheus_output = metrics.get_prometheus_metrics()
        assert "http_requests{method=\"GET\"} 5" in prometheus_output
        assert "memory_usage 75.5" in prometheus_output
    
    def test_timing_decorator(self):
        """Test timing decorator functionality."""
        metrics = get_metrics_collector()
        
        @timing_decorator("test_function", component="test")
        def test_function():
            time.sleep(0.01)  # Small delay
            return "success"
        
        result = test_function()
        assert result == "success"
        
        metrics_data = metrics.get_metrics()
        assert any("test_function_total" in key for key in metrics_data["counters"])
        assert any("test_function_duration" in key for key in metrics_data["timings"])
    
    @pytest.mark.asyncio
    async def test_async_timing_decorator(self):
        """Test timing decorator with async functions."""
        metrics = get_metrics_collector()
        
        @timing_decorator("async_test_function", component="test")
        async def async_test_function():
            await asyncio.sleep(0.01)
            return "async_success"
        
        result = await async_test_function()
        assert result == "async_success"
        
        metrics_data = metrics.get_metrics()
        assert any("async_test_function_total" in key for key in metrics_data["counters"])
    
    def test_timing_context(self):
        """Test timing context manager."""
        metrics = get_metrics_collector()
        
        with time_operation("context_test", operation="database"):
            time.sleep(0.01)
        
        metrics_data = metrics.get_metrics()
        assert any("context_test" in key for key in metrics_data["timings"])


class TestHealthChecking:
    """Test health checking functionality."""
    
    @pytest.mark.asyncio
    async def test_basic_health_check(self):
        """Test basic health check without dependencies."""
        health_checker = HealthChecker()
        
        result = await health_checker.check_health(include_dependencies=False)
        
        assert "status" in result
        assert result["status"] in ["healthy", "degraded", "unhealthy"]
        assert "service" in result
        assert "checks" in result
        assert len(result["checks"]) >= 3  # server, database, system_resources
    
    @pytest.mark.asyncio
    async def test_health_check_with_dependencies(self):
        """Test health check including external dependencies."""
        health_checker = HealthChecker()
        
        result = await health_checker.check_health(include_dependencies=True)
        
        assert "status" in result
        assert "checks" in result
        
        # Should include external API checks
        check_names = [check["name"] for check in result["checks"]]
        assert any("external_api" in name for name in check_names)
    
    @pytest.mark.asyncio 
    async def test_health_check_server_component(self):
        """Test server health check component."""
        health_checker = HealthChecker()
        
        server_check = await health_checker._check_server_health()
        
        assert server_check.name == "server"
        assert server_check.status in ["healthy", "degraded", "unhealthy"]
        assert server_check.response_time_ms >= 0
        assert server_check.details is not None
    
    @pytest.mark.asyncio
    async def test_health_check_system_resources(self):
        """Test system resources health check."""
        health_checker = HealthChecker()
        
        system_check = await health_checker._check_system_resources()
        
        assert system_check.name == "system_resources"
        assert system_check.status in ["healthy", "degraded", "unhealthy"]
        assert "cpu_percent" in system_check.details
        assert "memory_percent" in system_check.details
        assert "disk_percent" in system_check.details


class TestRequestLoggingMiddleware:
    """Test request logging middleware."""
    
    def test_middleware_initialization(self):
        """Test middleware can be initialized."""
        app = MagicMock()
        middleware = RequestLoggingMiddleware(app, exclude_paths=["/health"])
        
        assert middleware.exclude_paths == ["/health"]
        assert middleware.request_logger is not None
        assert middleware.metrics is not None
    
    def test_client_ip_extraction(self):
        """Test client IP extraction logic."""
        app = MagicMock()
        middleware = RequestLoggingMiddleware(app)
        
        # Mock request with forwarded header
        request = MagicMock()
        request.headers = {"x-forwarded-for": "192.168.1.1, 10.0.0.1"}
        request.client = None
        
        ip = middleware._get_client_ip(request)
        assert ip == "192.168.1.1"
        
        # Mock request with real IP header
        request.headers = {"x-real-ip": "192.168.1.2"}
        ip = middleware._get_client_ip(request)
        assert ip == "192.168.1.2"
        
        # Mock request with client IP
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.3"
        ip = middleware._get_client_ip(request)
        assert ip == "192.168.1.3"
    
    def test_path_normalization(self):
        """Test path normalization for metrics."""
        app = MagicMock()
        middleware = RequestLoggingMiddleware(app)
        
        assert middleware._normalize_path_for_metrics("/health") == "/health"
        assert middleware._normalize_path_for_metrics("/metrics") == "/metrics"
        assert middleware._normalize_path_for_metrics("/some/api/path") == "/api"
        assert middleware._normalize_path_for_metrics("/health?check=full") == "/health"


class TestServerIntegration:
    """Test server integration with monitoring features."""
    
    @pytest.mark.asyncio
    async def test_health_endpoints_exist(self):
        """Test that health endpoints are properly configured."""
        app = create_app()
        
        # This is a basic test to ensure the app can be created with monitoring
        assert app is not None
    
    def test_monitoring_modules_imported(self):
        """Test that monitoring modules are properly imported."""
        from nfl_mcp import server
        
        # Check that monitoring components are imported
        assert hasattr(server, 'get_logger')
        assert hasattr(server, 'get_metrics_collector')
        assert hasattr(server, 'get_health_checker')
        assert hasattr(server, 'RequestLoggingMiddleware')


class TestEndToEndMonitoring:
    """End-to-end tests for monitoring functionality."""
    
    @pytest.mark.asyncio
    async def test_metrics_collection_during_operations(self):
        """Test that metrics are collected during normal operations."""
        metrics = get_metrics_collector()
        
        # Simulate some operations
        metrics.increment_counter("test_operations", component="test")
        metrics.record_timing("test_operation_duration", 150.0, component="test")
        
        # Get metrics data
        metrics_data = metrics.get_metrics()
        
        # Verify metrics were recorded
        assert len(metrics_data["counters"]) > 0
        assert len(metrics_data["timings"]) > 0
        assert "timestamp" in metrics_data
    
    def test_logging_during_operations(self):
        """Test that structured logging works during operations."""
        logger = get_logger("test_operations")
        
        # This should not raise an exception
        logger.info("Starting test operation")
        logger.warning("Test warning with data", extra={"test_data": "value"})
        logger.error("Test error", extra={"error_code": 500})


if __name__ == "__main__":
    pytest.main([__file__])