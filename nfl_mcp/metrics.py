"""Lightweight metrics utilities (restored).

Provides: MetricsCollector, get_metrics_collector, timing_decorator.
"""
from __future__ import annotations
import time, threading, asyncio
from functools import wraps
from dataclasses import dataclass
from collections import defaultdict, deque
from datetime import datetime, UTC
from typing import Dict, Any, Callable

@dataclass
class MetricPoint:
    timestamp: float
    value: float
    labels: Dict[str, str]

@dataclass
class MetricSummary:
    count: int = 0
    total: float = 0.0
    min_value: float = float('inf')
    max_value: float = float('-inf')
    avg_value: float = 0.0
    last_updated: datetime | None = None

class MetricsCollector:
    def __init__(self, retention_hours: int = 24):
        self._lock = threading.RLock()
        self._retention_seconds = retention_hours * 3600
        self._counters: Dict[str, int] = defaultdict(int)
        self._gauges: Dict[str, float] = defaultdict(float)
        self._histograms: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10000))
        self._timings: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10000))
        self._summaries: Dict[str, MetricSummary] = defaultdict(MetricSummary)
        self._labels: Dict[str, Dict[str, str]] = defaultdict(dict)

    def _make_key(self, name: str, labels: Dict[str, str]) -> str:
        if not labels:
            return name
        label_str = '|'.join(f'{k}={v}' for k, v in sorted(labels.items()))
        return f'{name}|{label_str}'

    def _update_summary(self, key: str, value: float) -> None:
        summary = self._summaries[key]
        summary.count += 1
        summary.total += value
        summary.min_value = min(summary.min_value, value)
        summary.max_value = max(summary.max_value, value)
        summary.avg_value = summary.total / summary.count
        summary.last_updated = datetime.now(UTC)

    def _cleanup_old(self, key: str) -> None:
        cutoff = time.time() - self._retention_seconds
        if key in self._histograms:
            dq = self._histograms[key]
            while dq and dq[0].timestamp < cutoff:
                dq.popleft()
        if key in self._timings:
            dq = self._timings[key]
            while dq and dq[0].timestamp < cutoff:
                dq.popleft()

    def increment_counter(self, name: str, value: int = 1, **labels) -> None:
        with self._lock:
            key = self._make_key(name, labels)
            self._counters[key] += value
            self._labels[key] = labels
            self._update_summary(key, value)

    def set_gauge(self, name: str, value: float, **labels) -> None:
        with self._lock:
            key = self._make_key(name, labels)
            self._gauges[key] = value
            self._labels[key] = labels
            self._update_summary(key, value)

    def record_histogram(self, name: str, value: float, **labels) -> None:
        with self._lock:
            key = self._make_key(name, labels)
            mp = MetricPoint(time.time(), value, labels)
            self._histograms[key].append(mp)
            self._labels[key] = labels
            self._update_summary(key, value)
            self._cleanup_old(key)

    def record_timing(self, name: str, duration_ms: float, **labels) -> None:
        with self._lock:
            key = self._make_key(name, labels)
            mp = MetricPoint(time.time(), duration_ms, labels)
            self._timings[key].append(mp)
            self._labels[key] = labels
            self._update_summary(key, duration_ms)
            self._cleanup_old(key)

    def get_metrics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'timestamp': datetime.now(UTC).isoformat(),
                'counters': dict(self._counters),
                'gauges': dict(self._gauges),
                'summaries': {
                    name: {
                        'count': s.count,
                        'total': s.total,
                        'min': 0 if s.min_value == float('inf') else s.min_value,
                        'max': 0 if s.max_value == float('-inf') else s.max_value,
                        'avg': s.avg_value,
                        'last_updated': s.last_updated.isoformat() if s.last_updated else None
                    } for name, s in self._summaries.items()
                }
            }

    def get_prometheus_metrics(self) -> str:
        with self._lock:
            lines = []
            for name, value in self._counters.items():
                metric_name = name.split('|')[0]
                labels = self._labels.get(name, {})
                label_str = ','.join(f'{k}="{v}"' for k, v in labels.items())
                lines.append(f'{metric_name}{{{label_str}}} {value}' if label_str else f'{metric_name} {value}')
            for name, value in self._gauges.items():
                metric_name = name.split('|')[0]
                labels = self._labels.get(name, {})
                label_str = ','.join(f'{k}="{v}"' for k, v in labels.items())
                lines.append(f'{metric_name}{{{label_str}}} {value}' if label_str else f'{metric_name} {value}')
            return '\n'.join(lines) + '\n'

_metrics = MetricsCollector()

def get_metrics_collector() -> MetricsCollector:
    return _metrics

def timing_decorator(metric_name: str, **labels):
    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                start = time.time()
                try:
                    res = await func(*args, **kwargs)
                    _metrics.increment_counter(f"{metric_name}_total", status="success", **labels)
                    return res
                except Exception:
                    _metrics.increment_counter(f"{metric_name}_total", status="error", **labels)
                    raise
                finally:
                    dur = (time.time() - start) * 1000
                    _metrics.record_timing(f"{metric_name}_duration", dur, **labels)
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                start = time.time()
                try:
                    res = func(*args, **kwargs)
                    _metrics.increment_counter(f"{metric_name}_total", status="success", **labels)
                    return res
                except Exception:
                    _metrics.increment_counter(f"{metric_name}_total", status="error", **labels)
                    raise
                finally:
                    dur = (time.time() - start) * 1000
                    _metrics.record_timing(f"{metric_name}_duration", dur, **labels)
            return sync_wrapper
    return decorator
