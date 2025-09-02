#!/usr/bin/env python3
"""
Demo script to showcase the monitoring and observability features
of the NFL MCP Server.

This script demonstrates:
1. Structured logging
2. Metrics collection 
3. Health checking
4. Performance timing
"""

import asyncio
import time
from nfl_mcp.logging_config import get_logger, log_with_context
from nfl_mcp.metrics import get_metrics_collector, timing_decorator, time_operation
from nfl_mcp.monitoring import get_health_checker
from nfl_mcp.server import create_app


async def demo_monitoring_features():
    """Demonstrate monitoring and observability features."""
    print("🔍 NFL MCP Server Monitoring & Observability Demo")
    print("=" * 50)
    
    # 1. Structured Logging Demo
    print("\n1. 📝 Structured Logging Demo")
    logger = get_logger("demo")
    
    logger.info("Starting monitoring demo")
    logger.info("Processing user request", extra={
        "user_id": "demo_user_123",
        "request_id": "req_abc_456",
        "operation": "fetch_nfl_news"
    })
    
    log_with_context(
        logger, 
        "warning", 
        "Rate limit approaching",
        user_id="demo_user_123",
        requests_remaining=5,
        window="60s"
    )
    
    print("✓ Structured logs generated (check console output)")
    
    # 2. Metrics Collection Demo
    print("\n2. 📊 Metrics Collection Demo")
    metrics = get_metrics_collector()
    
    # Simulate some API calls
    for i in range(5):
        metrics.increment_counter("api_requests_total", method="GET", endpoint="/nfl_news")
        metrics.record_timing("api_response_time", 150 + i * 10, endpoint="/nfl_news")
    
    # Simulate database operations
    metrics.increment_counter("database_operations_total", operation="select", table="athletes")
    metrics.record_timing("database_query_time", 25.5, operation="select")
    
    # Set some gauges
    metrics.set_gauge("active_connections", 12)
    metrics.set_gauge("memory_usage_percent", 68.4)
    
    # Record histogram data
    for value in [10, 15, 20, 25, 30, 35, 40]:
        metrics.record_histogram("request_size_bytes", value * 1024)
    
    print("✓ Metrics recorded")
    
    # Display current metrics
    metrics_data = metrics.get_metrics()
    print(f"   - Total counters: {len(metrics_data['counters'])}")
    print(f"   - Total gauges: {len(metrics_data['gauges'])}")
    print(f"   - Total timing metrics: {len(metrics_data['timings'])}")
    print(f"   - Total histograms: {len(metrics_data['histograms'])}")
    
    # 3. Performance Timing Demo
    print("\n3. ⏱️  Performance Timing Demo")
    
    @timing_decorator("demo_operation", component="demo")
    async def simulated_api_call():
        """Simulate an API call with timing."""
        await asyncio.sleep(0.1)  # Simulate work
        return {"status": "success", "data": "NFL teams data"}
    
    # Call the decorated function
    result = await simulated_api_call()
    print(f"✓ Timed operation completed: {result['status']}")
    
    # Use timing context manager
    with time_operation("database_query", table="teams"):
        time.sleep(0.05)  # Simulate database query
    
    print("✓ Context manager timing completed")
    
    # 4. Health Checking Demo
    print("\n4. 🏥 Health Checking Demo")
    health_checker = get_health_checker()
    
    print("   Running basic health check...")
    basic_health = await health_checker.check_health(include_dependencies=False)
    print(f"   - Overall status: {basic_health['status']}")
    print(f"   - Checks performed: {basic_health['summary']['total_checks']}")
    print(f"   - Healthy checks: {basic_health['summary']['healthy_checks']}")
    
    print("\n   Running comprehensive health check (with dependencies)...")
    full_health = await health_checker.check_health(include_dependencies=True)
    print(f"   - Overall status: {full_health['status']}")
    print(f"   - Total checks: {full_health['summary']['total_checks']}")
    print(f"   - Response time: {full_health['response_time_ms']:.2f}ms")
    
    # Display individual check results
    print("\n   Individual check results:")
    for check in full_health['checks']:
        status_emoji = "✅" if check['status'] == "healthy" else "⚠️" if check['status'] == "degraded" else "❌"
        print(f"     {status_emoji} {check['name']}: {check['status']} ({check['response_time_ms']:.2f}ms)")
    
    # 5. Final Metrics Summary
    print("\n5. 📈 Final Metrics Summary")
    final_metrics = metrics.get_metrics()
    
    print(f"   - Total API requests: {final_metrics['counters'].get('api_requests_total|endpoint=/nfl_news|method=GET', 0)}")
    print(f"   - Average API response time: {final_metrics['timings'].get('api_response_time', {}).get('avg_ms', 0):.2f}ms")
    print(f"   - Active connections: {final_metrics['gauges'].get('active_connections', 0)}")
    print(f"   - Memory usage: {final_metrics['gauges'].get('memory_usage_percent', 0)}%")
    
    # 6. Prometheus Format Export
    print("\n6. 📋 Prometheus Metrics Export")
    prometheus_metrics = metrics.get_prometheus_metrics()
    print("   Sample Prometheus metrics:")
    lines = prometheus_metrics.strip().split('\n')[:5]  # Show first 5 lines
    for line in lines:
        print(f"     {line}")
    if len(lines) >= 5:
        print(f"     ... and {len(prometheus_metrics.strip().split())-5} more metrics")
    
    print(f"\n🎉 Demo completed! The NFL MCP Server now includes comprehensive monitoring:")
    print("   ✅ Structured logging with JSON format")
    print("   ✅ Performance metrics collection (counters, gauges, histograms, timings)")
    print("   ✅ Health checks for server, database, and external dependencies")
    print("   ✅ Request/response logging middleware")
    print("   ✅ Prometheus-compatible metrics export")
    print("   ✅ Easy integration with monitoring tools")


def demo_server_creation():
    """Demonstrate server creation with monitoring."""
    print("\n7. 🚀 Server Creation with Monitoring")
    
    try:
        app = create_app()
        print("✅ Server created successfully with monitoring features:")
        print("   - /health - Basic health check")
        print("   - /health?include_dependencies=true - Comprehensive health check")
        print("   - /metrics - Performance metrics (JSON)")
        print("   - /metrics (Accept: text/plain) - Prometheus format")
        print("   - Request/response logging middleware enabled")
        print("   - Structured logging configured")
    except Exception as e:
        print(f"❌ Error creating server: {e}")


if __name__ == "__main__":
    print("Starting NFL MCP Server Monitoring Demo...")
    
    # Run async demo
    asyncio.run(demo_monitoring_features())
    
    # Run sync demo
    demo_server_creation()
    
    print("\n🏁 Demo finished! Check the logs directory for structured log files.")