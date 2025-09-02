"""
Request/response logging middleware for the NFL MCP Server.

This module provides middleware to log all HTTP requests and responses
with timing information and error tracking.
"""

import time
import json
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from typing import Callable, Dict, Any

from .logging_config import get_logger
from .metrics import get_metrics_collector


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log HTTP requests and responses with metrics."""
    
    def __init__(self, app, exclude_paths: list = None):
        super().__init__(app)
        self.request_logger = get_logger("nfl_mcp.requests")
        self.metrics = get_metrics_collector()
        self.exclude_paths = exclude_paths or []
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and log details."""
        start_time = time.time()
        
        # Extract request details
        method = request.method
        path = request.url.path
        query_params = str(request.query_params) if request.query_params else ""
        user_agent = request.headers.get("user-agent", "")
        client_ip = self._get_client_ip(request)
        
        # Get request size
        request_size = 0
        if "content-length" in request.headers:
            try:
                request_size = int(request.headers["content-length"])
            except (ValueError, TypeError):
                pass
        
        # Skip logging for excluded paths
        should_log = path not in self.exclude_paths
        
        # Process request
        try:
            response = await call_next(request)
            status_code = response.status_code
            
            # Calculate timing
            response_time_ms = (time.time() - start_time) * 1000
            
            # Get response size
            response_size = 0
            if hasattr(response, 'body') and response.body:
                response_size = len(response.body)
            elif "content-length" in response.headers:
                try:
                    response_size = int(response.headers["content-length"])
                except (ValueError, TypeError):
                    pass
            
            # Log request/response if not excluded
            if should_log:
                self._log_request_response(
                    method=method,
                    path=path,
                    query_params=query_params,
                    status_code=status_code,
                    response_time_ms=response_time_ms,
                    user_agent=user_agent,
                    client_ip=client_ip,
                    request_size=request_size,
                    response_size=response_size,
                    error=None
                )
            
            # Record metrics
            self._record_metrics(
                method=method,
                path=path,
                status_code=status_code,
                response_time_ms=response_time_ms,
                request_size=request_size,
                response_size=response_size
            )
            
            return response
            
        except Exception as e:
            # Calculate timing for error case
            response_time_ms = (time.time() - start_time) * 1000
            
            # Log error if not excluded
            if should_log:
                self._log_request_response(
                    method=method,
                    path=path,
                    query_params=query_params,
                    status_code=500,
                    response_time_ms=response_time_ms,
                    user_agent=user_agent,
                    client_ip=client_ip,
                    request_size=request_size,
                    response_size=0,
                    error=str(e)
                )
            
            # Record error metrics
            self._record_metrics(
                method=method,
                path=path,
                status_code=500,
                response_time_ms=response_time_ms,
                request_size=request_size,
                response_size=0
            )
            
            raise
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP address from request."""
        # Check for forwarded headers first
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
        
        # Fall back to direct client IP
        if hasattr(request, "client") and request.client:
            return request.client.host
        
        return "unknown"
    
    def _log_request_response(
        self,
        method: str,
        path: str,
        query_params: str,
        status_code: int,
        response_time_ms: float,
        user_agent: str,
        client_ip: str,
        request_size: int,
        response_size: int,
        error: str = None
    ) -> None:
        """Log request/response details."""
        log_data = {
            "method": method,
            "path": path,
            "query_params": query_params,
            "status_code": status_code,
            "response_time_ms": round(response_time_ms, 2),
            "user_agent": user_agent,
            "client_ip": client_ip,
            "request_size": request_size,
            "response_size": response_size
        }
        
        if error:
            log_data["error"] = error
            log_level = "error"
            message = f"{method} {path} - {status_code} - {response_time_ms:.2f}ms - ERROR: {error}"
        else:
            log_level = "info"
            message = f"{method} {path} - {status_code} - {response_time_ms:.2f}ms"
        
        # Create log record with extra fields
        record = self.request_logger.makeRecord(
            self.request_logger.name,
            getattr(self.request_logger, log_level.upper()),
            "",
            0,
            message,
            (),
            None
        )
        
        # Add all log data as extra fields
        for key, value in log_data.items():
            setattr(record, key, value)
        
        self.request_logger.handle(record)
    
    def _record_metrics(
        self,
        method: str,
        path: str,
        status_code: int,
        response_time_ms: float,
        request_size: int,
        response_size: int
    ) -> None:
        """Record metrics for the request."""
        # Normalize path for metrics (remove dynamic parts)
        metric_path = self._normalize_path_for_metrics(path)
        
        # Increment request counter
        self.metrics.increment_counter(
            "http_requests_total",
            method=method,
            path=metric_path,
            status_code=str(status_code)
        )
        
        # Record response time
        self.metrics.record_timing(
            "http_request_duration",
            response_time_ms,
            method=method,
            path=metric_path
        )
        
        # Record request/response sizes
        if request_size > 0:
            self.metrics.record_histogram(
                "http_request_size_bytes",
                request_size,
                method=method,
                path=metric_path
            )
        
        if response_size > 0:
            self.metrics.record_histogram(
                "http_response_size_bytes",
                response_size,
                method=method,
                path=metric_path
            )
        
        # Track error rates
        if status_code >= 400:
            self.metrics.increment_counter(
                "http_errors_total",
                method=method,
                path=metric_path,
                status_code=str(status_code)
            )
    
    def _normalize_path_for_metrics(self, path: str) -> str:
        """Normalize path for metrics to avoid high cardinality."""
        # Remove query parameters
        if "?" in path:
            path = path.split("?")[0]
        
        # Replace dynamic segments with placeholders
        # This is a simple implementation - in production you might want more sophisticated path normalization
        if path.startswith("/health"):
            return "/health"
        elif path.startswith("/metrics"):
            return "/metrics"
        else:
            return "/api"  # Group all other paths as /api