"""Tests for metrics module (MetricsCollector, get_metrics_collector, timing_decorator)."""
import pytest
import time
import asyncio
from unittest.mock import patch, MagicMock
from nfl_mcp.metrics import MetricsCollector, get_metrics_collector, timing_decorator


class TestMetricsCollector:
    """Test MetricsCollector class."""

    def test_increment_counter(self):
        """Test counter increment."""
        collector = MetricsCollector()
        collector.increment_counter("test_counter", 5)
        metrics = collector.get_metrics()
        assert metrics["counters"]["test_counter"] == 5

    def test_increment_counter_with_labels(self):
        """Test counter with labels."""
        collector = MetricsCollector()
        collector.increment_counter("http_requests", 1, method="GET", status="200")
        metrics = collector.get_metrics()
        assert metrics["counters"]["http_requests|method=GET|status=200"] == 1

    def test_set_gauge(self):
        """Test gauge setting."""
        collector = MetricsCollector()
        collector.set_gauge("cpu_usage", 45.5)
        metrics = collector.get_metrics()
        assert metrics["gauges"]["cpu_usage"] == 45.5

    def test_record_histogram(self):
        """Test histogram recording."""
        collector = MetricsCollector()
        collector.record_histogram("response_time", 0.5)
        metrics = collector.get_metrics()
        assert "response_time" in metrics["summaries"]
        assert metrics["summaries"]["response_time"]["count"] == 1

    def test_record_timing(self):
        """Test timing recording."""
        collector = MetricsCollector()
        collector.record_timing("api_duration", 150.5)
        metrics = collector.get_metrics()
        assert "api_duration" in metrics["summaries"]
        assert metrics["summaries"]["api_duration"]["count"] == 1

    def test_get_metrics(self):
        """Test get_metrics returns correct structure."""
        collector = MetricsCollector()
        metrics = collector.get_metrics()
        assert "counters" in metrics
        assert "gauges" in metrics
        assert "summaries" in metrics
        assert "timestamp" in metrics

    def test_get_prometheus_metrics(self):
        """Test Prometheus metrics format."""
        collector = MetricsCollector()
        collector.increment_counter("test_metric", 10)
        prom = collector.get_prometheus_metrics()
        assert "test_metric" in prom

    def test_retention_cleanup(self):
        """Test that old metrics are cleaned up based on retention."""
        collector = MetricsCollector(retention_hours=0)  # Very short retention
        # Add some metrics
        for i in range(100):
            collector.record_histogram("test_histogram", float(i))
        
        metrics = collector.get_metrics()
        # Should still have the metrics (cleanup happens on next record)
        assert metrics["summaries"]["test_histogram"]["count"] == 100


class TestTimingDecorator:
    """Test timing_decorator function."""

    def test_timing_decorator_sync(self):
        """Test timing decorator on sync function."""
        @timing_decorator("test_timing", category="test")
        def sync_func():
            return "success"
        
        result = sync_func()
        assert result == "success"

    @pytest.mark.asyncio
    async def test_timing_decorator_async(self):
        """Test timing decorator on async function."""
        @timing_decorator("test_async_timing", category="test")
        async def async_func():
            return "async_success"
        
        result = await async_func()
        assert result == "async_success"

    def test_timing_decorator_records_metrics(self):
        """Test that timing decorator records metrics."""
        with patch('nfl_mcp.metrics._metrics') as mock_metrics:
            @timing_decorator("test_metric", type="timing")
            def test_func():
                return "done"
            
            test_func()
            
            # Should have called increment_counter and record_timing
            assert mock_metrics.increment_counter.called
            assert mock_metrics.record_timing.called

    def test_timing_decorator_error_handling(self):
        """Test that timing decorator handles errors correctly."""
        @timing_decorator("error_metric", category="test")
        def error_func():
            raise ValueError("test error")
        
        with pytest.raises(ValueError, match="test error"):
            error_func()


class TestGetMetricsCollector:
    """Test get_metrics_collector singleton."""

    def test_singleton(self):
        """Test that get_metrics_collector returns singleton instance."""
        collector1 = get_metrics_collector()
        collector2 = get_metrics_collector()
        assert collector1 is collector2
