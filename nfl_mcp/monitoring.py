"""
Monitoring and health check utilities for the NFL MCP Server.

This module provides comprehensive health checks for the server and its dependencies,
including database connectivity, external API availability, and system resources.
"""

import asyncio
import time
import psutil
import httpx
from datetime import datetime, UTC
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from .database import NFLDatabase
from .metrics import get_metrics_collector, time_operation
from .logging_config import get_logger


logger = get_logger(__name__)


@dataclass
class HealthCheckResult:
    """Result of a health check operation."""
    name: str
    status: str  # "healthy", "unhealthy", "degraded"
    message: str
    response_time_ms: float
    timestamp: str
    details: Optional[Dict[str, Any]] = None


class HealthChecker:
    """Comprehensive health checking for the NFL MCP Server."""
    
    def __init__(self, nfl_db: Optional[NFLDatabase] = None):
        self.nfl_db = nfl_db or NFLDatabase()
        self.metrics = get_metrics_collector()
        self._external_apis = {
            "espn_news": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",
            "espn_teams": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams",
            "sleeper_players": "https://api.sleeper.app/v1/players/nfl"
        }
    
    async def check_health(self, include_dependencies: bool = False) -> Dict[str, Any]:
        """
        Perform comprehensive health check.
        
        Args:
            include_dependencies: Whether to check external dependencies
            
        Returns:
            Health check results including status and details
        """
        logger.info("Starting health check", extra={"include_dependencies": include_dependencies})
        
        with time_operation("health_check"):
            checks = []
            overall_status = "healthy"
            
            # Basic server health
            server_check = await self._check_server_health()
            checks.append(server_check)
            
            # Database health
            db_check = await self._check_database_health()
            checks.append(db_check)
            if db_check.status != "healthy":
                overall_status = "degraded"
            
            # System resources
            system_check = await self._check_system_resources()
            checks.append(system_check)
            if system_check.status != "healthy" and overall_status == "healthy":
                overall_status = "degraded"
            
            # External dependencies (if requested)
            if include_dependencies:
                for api_name, api_url in self._external_apis.items():
                    api_check = await self._check_external_api(api_name, api_url)
                    checks.append(api_check)
                    if api_check.status == "unhealthy":
                        overall_status = "degraded"
            
            # Calculate overall response time
            total_response_time = sum(check.response_time_ms for check in checks)
            
            result = {
                "status": overall_status,
                "service": "NFL MCP Server",
                "version": "0.1.0",
                "timestamp": datetime.now(UTC).isoformat(),
                "response_time_ms": total_response_time,
                "checks": [
                    {
                        "name": check.name,
                        "status": check.status,
                        "message": check.message,
                        "response_time_ms": check.response_time_ms,
                        "details": check.details
                    }
                    for check in checks
                ],
                "summary": {
                    "total_checks": len(checks),
                    "healthy_checks": len([c for c in checks if c.status == "healthy"]),
                    "degraded_checks": len([c for c in checks if c.status == "degraded"]),
                    "unhealthy_checks": len([c for c in checks if c.status == "unhealthy"])
                }
            }
            
            # Record metrics
            self.metrics.set_gauge("health_check_status", 1 if overall_status == "healthy" else 0)
            self.metrics.record_timing("health_check_duration", total_response_time)
            
            logger.info("Health check completed", extra={
                "status": overall_status,
                "total_checks": len(checks),
                "response_time_ms": total_response_time
            })
            
            return result
    
    async def _check_server_health(self) -> HealthCheckResult:
        """Check basic server health."""
        start_time = time.time()
        
        try:
            # Get current metrics for basic validation
            metrics = self.metrics.get_metrics()
            uptime = time.time() - self._get_start_time()
            
            details = {
                "uptime_seconds": uptime,
                "metrics_available": bool(metrics),
                "total_requests": metrics.get("counters", {}).get("http_requests_total", 0)
            }
            
            response_time = (time.time() - start_time) * 1000
            
            return HealthCheckResult(
                name="server",
                status="healthy",
                message="Server is running normally",
                response_time_ms=response_time,
                timestamp=datetime.now(UTC).isoformat(),
                details=details
            )
            
        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            logger.error("Server health check failed", extra={"error": str(e)})
            
            return HealthCheckResult(
                name="server",
                status="unhealthy",
                message=f"Server health check failed: {str(e)}",
                response_time_ms=response_time,
                timestamp=datetime.now(UTC).isoformat()
            )
    
    async def _check_database_health(self) -> HealthCheckResult:
        """Check database connectivity and health."""
        start_time = time.time()
        
        try:
            # Use existing database health check method
            health_result = self.nfl_db.health_check()
            response_time = (time.time() - start_time) * 1000
            
            if health_result["healthy"]:
                status = "healthy"
                message = "Database is accessible and functioning"
            else:
                status = "unhealthy"
                message = "Database health check failed"
            
            return HealthCheckResult(
                name="database",
                status=status,
                message=message,
                response_time_ms=response_time,
                timestamp=datetime.now(UTC).isoformat(),
                details=health_result
            )
            
        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            logger.error("Database health check failed", extra={"error": str(e)})
            
            return HealthCheckResult(
                name="database",
                status="unhealthy",
                message=f"Database check failed: {str(e)}",
                response_time_ms=response_time,
                timestamp=datetime.now(UTC).isoformat()
            )
    
    async def _check_system_resources(self) -> HealthCheckResult:
        """Check system resource usage."""
        start_time = time.time()
        
        try:
            # Get system metrics
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            details = {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_mb": memory.available / (1024 * 1024),
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free / (1024 * 1024 * 1024)
            }
            
            # Determine status based on thresholds
            status = "healthy"
            message = "System resources are within normal limits"
            
            if cpu_percent > 90 or memory.percent > 90 or disk.percent > 90:
                status = "unhealthy"
                message = "System resources are critically high"
            elif cpu_percent > 70 or memory.percent > 70 or disk.percent > 80:
                status = "degraded"
                message = "System resources are elevated but acceptable"
            
            response_time = (time.time() - start_time) * 1000
            
            # Record resource metrics
            self.metrics.set_gauge("system_cpu_percent", cpu_percent)
            self.metrics.set_gauge("system_memory_percent", memory.percent)
            self.metrics.set_gauge("system_disk_percent", disk.percent)
            
            return HealthCheckResult(
                name="system_resources",
                status=status,
                message=message,
                response_time_ms=response_time,
                timestamp=datetime.now(UTC).isoformat(),
                details=details
            )
            
        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            logger.error("System resources check failed", extra={"error": str(e)})
            
            return HealthCheckResult(
                name="system_resources",
                status="unhealthy",
                message=f"System check failed: {str(e)}",
                response_time_ms=response_time,
                timestamp=datetime.now(UTC).isoformat()
            )
    
    async def _check_external_api(self, api_name: str, api_url: str) -> HealthCheckResult:
        """Check external API availability."""
        start_time = time.time()
        
        try:
            timeout = httpx.Timeout(10.0, connect=5.0)
            headers = {"User-Agent": "NFL-MCP-Server/0.1.0 (Health Check)"}
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.head(api_url, follow_redirects=True)
                response_time = (time.time() - start_time) * 1000
                
                details = {
                    "url": api_url,
                    "status_code": response.status_code,
                    "response_headers": dict(response.headers)
                }
                
                if response.status_code < 400:
                    status = "healthy"
                    message = f"{api_name} API is accessible"
                elif response.status_code < 500:
                    status = "degraded"
                    message = f"{api_name} API returned client error"
                else:
                    status = "unhealthy"
                    message = f"{api_name} API returned server error"
                
                return HealthCheckResult(
                    name=f"external_api_{api_name}",
                    status=status,
                    message=message,
                    response_time_ms=response_time,
                    timestamp=datetime.now(UTC).isoformat(),
                    details=details
                )
                
        except httpx.TimeoutException:
            response_time = (time.time() - start_time) * 1000
            logger.warning(f"External API timeout: {api_name}", extra={"url": api_url})
            
            return HealthCheckResult(
                name=f"external_api_{api_name}",
                status="unhealthy",
                message=f"{api_name} API timed out",
                response_time_ms=response_time,
                timestamp=datetime.now(UTC).isoformat(),
                details={"url": api_url, "error": "timeout"}
            )
            
        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            logger.error(f"External API check failed: {api_name}", extra={"url": api_url, "error": str(e)})
            
            return HealthCheckResult(
                name=f"external_api_{api_name}",
                status="unhealthy",
                message=f"{api_name} API check failed: {str(e)}",
                response_time_ms=response_time,
                timestamp=datetime.now(UTC).isoformat(),
                details={"url": api_url, "error": str(e)}
            )
    
    def _get_start_time(self) -> float:
        """Get server start time (simplified implementation)."""
        # In a real implementation, this would track actual server start time
        return time.time() - 300  # Assume 5 minutes for demo


# Global health checker instance
_health_checker = None


def get_health_checker(nfl_db: Optional[NFLDatabase] = None) -> HealthChecker:
    """Get the global health checker instance."""
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker(nfl_db)
    return _health_checker