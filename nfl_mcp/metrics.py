"""
Performance metrics collection for the NFL MCP Server.

This module provides a simple metrics collection system for tracking
performance, request counts, and error rates.
"""

import time
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Dict, List, Optional, Any, Callable
from functools import wraps
import asyncio


@dataclass
class MetricPoint:
    """A single metric data point."""
    timestamp: float
    value: float
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass 
class MetricSummary:
    """Summary statistics for a metric."""
    count: int = 0
    total: float = 0.0
    min_value: float = float('inf')
    max_value: float = float('-inf')
    avg_value: float = 0.0
    last_updated: Optional[datetime] = None


class MetricsCollector:
    """Thread-safe metrics collection system."""
    
    def __init__(self, retention_hours: int = 24):
        self._lock = threading.RLock()
        self._retention_seconds = retention_hours * 3600
        
        # Time-series data storage
        self._counters: Dict[str, int] = defaultdict(int)
        self._gauges: Dict[str, float] = defaultdict(float)
        self._histograms: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10000))
        self._timings: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10000))
        
        # Summary statistics
        self._summaries: Dict[str, MetricSummary] = defaultdict(MetricSummary)
        
        # Labels for metrics
        self._labels: Dict[str, Dict[str, str]] = defaultdict(dict)
        
    def increment_counter(self, name: str, value: int = 1, **labels) -> None:
        """Increment a counter metric."""
        with self._lock:
            key = self._make_key(name, labels)
            self._counters[key] += value
            self._labels[key] = labels
            self._update_summary(key, value)
    
    def set_gauge(self, name: str, value: float, **labels) -> None:
        """Set a gauge metric value."""
        with self._lock:
            key = self._make_key(name, labels)
            self._gauges[key] = value
            self._labels[key] = labels
            self._update_summary(key, value)
    
    def record_histogram(self, name: str, value: float, **labels) -> None:
        """Record a value in a histogram."""
        with self._lock:
            key = self._make_key(name, labels)
            timestamp = time.time()
            self._histograms[key].append(MetricPoint(timestamp, value, labels))
            self._labels[key] = labels
            self._update_summary(key, value)
            self._cleanup_old_data(key)
    
    def record_timing(self, name: str, duration_ms: float, **labels) -> None:
        """Record a timing metric in milliseconds."""
        with self._lock:
            key = self._make_key(name, labels)
            timestamp = time.time()
            self._timings[key].append(MetricPoint(timestamp, duration_ms, labels))
            self._labels[key] = labels
            self._update_summary(key, duration_ms)
            self._cleanup_old_data(key)
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get all current metrics."""
        with self._lock:
            metrics = {
                "timestamp": datetime.now(UTC).isoformat(),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "summaries": {
                    name: {
                        "count": summary.count,
                        "total": summary.total,
                        "min": summary.min_value if summary.min_value != float('inf') else 0,
                        "max": summary.max_value if summary.max_value != float('-inf') else 0,
                        "avg": summary.avg_value,
                        "last_updated": summary.last_updated.isoformat() if summary.last_updated else None
                    }
                    for name, summary in self._summaries.items()
                },
                "labels": dict(self._labels)
            }
            
            # Add histogram percentiles
            histogram_stats = {}
            for name, points in self._histograms.items():
                if points:
                    values = [p.value for p in points]
                    values.sort()
                    n = len(values)
                    histogram_stats[name] = {
                        "count": n,
                        "p50": values[int(n * 0.5)] if n > 0 else 0,
                        "p90": values[int(n * 0.9)] if n > 0 else 0,
                        "p95": values[int(n * 0.95)] if n > 0 else 0,
                        "p99": values[int(n * 0.99)] if n > 0 else 0
                    }
            metrics["histograms"] = histogram_stats
            
            # Add timing percentiles
            timing_stats = {}
            for name, points in self._timings.items():
                if points:
                    values = [p.value for p in points]
                    values.sort()
                    n = len(values)
                    timing_stats[name] = {
                        "count": n,
                        "p50_ms": values[int(n * 0.5)] if n > 0 else 0,
                        "p90_ms": values[int(n * 0.9)] if n > 0 else 0,
                        "p95_ms": values[int(n * 0.95)] if n > 0 else 0,
                        "p99_ms": values[int(n * 0.99)] if n > 0 else 0,
                        "avg_ms": sum(values) / n if n > 0 else 0
                    }
            metrics["timings"] = timing_stats
            
            return metrics
    
    def get_prometheus_metrics(self) -> str:
        """Get metrics in Prometheus format."""
        with self._lock:
            lines = []
            
            # Add counters
            for name, value in self._counters.items():
                metric_name = name.split('|')[0]
                labels = self._labels.get(name, {})
                label_str = ','.join(f'{k}="{v}"' for k, v in labels.items())
                if label_str:
                    lines.append(f'{metric_name}{{{label_str}}} {value}')
                else:
                    lines.append(f'{metric_name} {value}')
            
            # Add gauges
            for name, value in self._gauges.items():
                metric_name = name.split('|')[0]
                labels = self._labels.get(name, {})
                label_str = ','.join(f'{k}="{v}"' for k, v in labels.items())
                if label_str:
                    lines.append(f'{metric_name}{{{label_str}}} {value}')
                else:
                    lines.append(f'{metric_name} {value}')
            
            return '\n'.join(lines) + '\n'
    
    def _make_key(self, name: str, labels: Dict[str, str]) -> str:
        """Create a unique key for a metric with labels."""
        if not labels:
            return name
        label_str = '|'.join(f'{k}={v}' for k, v in sorted(labels.items()))
        return f'{name}|{label_str}'
    
    def _update_summary(self, key: str, value: float) -> None:
        """Update summary statistics for a metric."""
        summary = self._summaries[key]
        summary.count += 1
        summary.total += value
        summary.min_value = min(summary.min_value, value)
        summary.max_value = max(summary.max_value, value)
        summary.avg_value = summary.total / summary.count
        summary.last_updated = datetime.now(UTC)
    
    def _cleanup_old_data(self, key: str) -> None:
        """Remove old data points beyond retention period."""
        cutoff_time = time.time() - self._retention_seconds
        
        # Clean histograms
        if key in self._histograms:
            histogram = self._histograms[key]
            while histogram and histogram[0].timestamp < cutoff_time:
                histogram.popleft()
        
        # Clean timings
        if key in self._timings:
            timings = self._timings[key]
            while timings and timings[0].timestamp < cutoff_time:
                timings.popleft()


# Global metrics collector instance
_metrics_collector = MetricsCollector()


def get_metrics_collector() -> MetricsCollector:
    """Get the global metrics collector instance."""
    return _metrics_collector


def timing_decorator(metric_name: str, **labels):
    """Decorator to time function execution."""
    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = await func(*args, **kwargs)
                    _metrics_collector.increment_counter(f"{metric_name}_total", status="success", **labels)
                    return result
                except Exception as e:
                    _metrics_collector.increment_counter(f"{metric_name}_total", status="error", **labels)
                    raise
                finally:
                    duration_ms = (time.time() - start_time) * 1000
                    _metrics_collector.record_timing(f"{metric_name}_duration", duration_ms, **labels)
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = func(*args, **kwargs)
                    _metrics_collector.increment_counter(f"{metric_name}_total", status="success", **labels)
                    return result
                except Exception as e:
                    _metrics_collector.increment_counter(f"{metric_name}_total", status="error", **labels)
                    raise
                finally:
                    duration_ms = (time.time() - start_time) * 1000
                    _metrics_collector.record_timing(f"{metric_name}_duration", duration_ms, **labels)
            return sync_wrapper
    return decorator


class TimingContext:
    """Context manager for timing operations."""
    
    def __init__(self, metric_name: str, **labels):
        self.metric_name = metric_name
        self.labels = labels
        self.start_time = None
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time:
            duration_ms = (time.time() - self.start_time) * 1000
            status = "error" if exc_type else "success"
            _metrics_collector.record_timing(self.metric_name, duration_ms, status=status, **self.labels)
            _metrics_collector.increment_counter(f"{self.metric_name}_total", status=status, **self.labels)


def time_operation(metric_name: str, **labels) -> TimingContext:
    """Create a timing context manager."""
    return TimingContext(metric_name, **labels)