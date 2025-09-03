# Monitoring and Observability

The NFL MCP Server includes comprehensive monitoring and observability features designed to provide deep insights into server performance, health, and operational metrics.

## Overview

The monitoring system provides:

- **Structured Logging**: JSON-formatted logs with contextual information
- **Performance Metrics**: Comprehensive metrics collection with Prometheus export
- **Health Checks**: Multi-level health monitoring for server and dependencies
- **Request Tracking**: Automatic HTTP request/response monitoring
- **Error Tracking**: Structured error logging and metrics

## Features

### 1. Structured Logging

#### Configuration
- JSON-formatted logs for easy parsing
- Contextual information in every log entry
- Configurable log levels and output destinations
- Automatic log rotation with file size limits

#### Log Format
```json
{
  "timestamp": "2025-01-01T12:00:00Z",
  "level": "INFO",
  "logger": "nfl_mcp.server",
  "message": "Processing NFL news request",
  "service": "nfl-mcp-server",
  "version": "0.1.0",
  "module": "server",
  "function": "get_nfl_news",
  "line": 123,
  "thread": 140537025355904,
  "thread_name": "MainThread",
  "user_id": "user_123",
  "request_id": "req_abc_456"
}
```

#### Usage
```python
from nfl_mcp.logging_config import get_logger, log_with_context

logger = get_logger(__name__)

# Basic logging
logger.info("Operation completed successfully")

# Contextual logging
log_with_context(
    logger,
    "info", 
    "User request processed",
    user_id="user_123",
    operation="fetch_nfl_news",
    response_time_ms=150.5
)
```

### 2. Performance Metrics

#### Metric Types
- **Counters**: Monotonic values (request counts, error counts)
- **Gauges**: Current values (active connections, memory usage)
- **Histograms**: Distribution of values (response sizes)
- **Timings**: Duration measurements with percentiles

#### Automatic Collection
The server automatically collects:
- HTTP request counts by method, path, and status code
- Response times with percentile calculations
- Request/response sizes
- Database operation metrics
- Error rates and types

#### Custom Metrics
```python
from nfl_mcp.metrics import get_metrics_collector, timing_decorator, time_operation

metrics = get_metrics_collector()

# Increment counters
metrics.increment_counter("api_calls", endpoint="nfl_news", status="success")

# Set gauges
metrics.set_gauge("active_users", 42)

# Record timing with decorator
@timing_decorator("database_query", table="athletes")
async def fetch_athletes():
    # Database operation
    pass

# Record timing with context manager
with time_operation("external_api_call", service="espn"):
    # API call
    pass
```

#### Metrics Export

**JSON Format** (`/metrics`):
```json
{
  "timestamp": "2025-01-01T12:00:00Z",
  "counters": {
    "http_requests_total|method=GET|status_code=200": 1250
  },
  "gauges": {
    "memory_usage_percent": 68.4
  },
  "timings": {
    "http_request_duration": {
      "count": 1250,
      "p50_ms": 85.5,
      "p90_ms": 150.2,
      "p95_ms": 200.1,
      "p99_ms": 350.8,
      "avg_ms": 95.3
    }
  }
}
```

**Prometheus Format** (`/metrics` with `Accept: text/plain`):
```
http_requests_total{method="GET",status_code="200"} 1250
memory_usage_percent 68.4
http_request_duration{method="GET"} 0.095
```

### 3. Health Checks

#### Basic Health Check (`/health`)
Returns essential server health information:
- Server status
- Basic system information
- Response time

#### Comprehensive Health Check (`/health?include_dependencies=true`)
Includes additional checks:
- Database connectivity and performance
- System resources (CPU, memory, disk)
- External API availability
- Overall system health assessment

#### Health Check Response
```json
{
  "status": "healthy",
  "service": "NFL MCP Server",
  "version": "0.1.0",
  "timestamp": "2025-01-01T12:00:00Z",
  "response_time_ms": 45.2,
  "checks": [
    {
      "name": "server",
      "status": "healthy",
      "message": "Server is running normally",
      "response_time_ms": 1.5,
      "details": {
        "uptime_seconds": 3600,
        "total_requests": 1250
      }
    },
    {
      "name": "database", 
      "status": "healthy",
      "message": "Database is accessible and functioning",
      "response_time_ms": 15.3,
      "details": {
        "athlete_count": 2500,
        "team_count": 32
      }
    }
  ],
  "summary": {
    "total_checks": 6,
    "healthy_checks": 5,
    "degraded_checks": 1,
    "unhealthy_checks": 0
  }
}
```

#### Status Levels
- **healthy**: All systems functioning normally
- **degraded**: Some issues but service still operational
- **unhealthy**: Critical issues affecting service

### 4. Request/Response Monitoring

#### Automatic Tracking
All HTTP requests are automatically logged with:
- Request method, path, and query parameters
- Response status code and timing
- Request/response sizes
- Client IP and User-Agent
- Error information (if applicable)

#### Request Log Format
```json
{
  "timestamp": "2025-01-01T12:00:00Z",
  "level": "INFO",
  "service": "nfl-mcp-server",
  "type": "http_request",
  "message": "GET /health - 200 - 45.2ms",
  "method": "GET",
  "path": "/health",
  "status_code": 200,
  "response_time_ms": 45.2,
  "user_agent": "curl/7.68.0",
  "client_ip": "192.168.1.100",
  "request_size": 0,
  "response_size": 256
}
```

### 5. Error Tracking

#### Structured Error Information
Errors are logged with:
- Error type and classification
- Stack traces and context
- Request information
- Impact assessment

#### Error Metrics
- Error counts by type and endpoint
- Error rates and trends
- Recovery time measurements

## Configuration

### Environment Variables

Configure monitoring behavior with environment variables:

```bash
# Logging configuration
export NFL_MCP_LOG_LEVEL=INFO
export NFL_MCP_LOG_FILE=/var/log/nfl_mcp.log

# Metrics retention
export NFL_MCP_METRICS_RETENTION_HOURS=24

# Health check intervals
export NFL_MCP_HEALTH_CHECK_INTERVAL=60
```

### Programmatic Configuration

```python
from nfl_mcp.logging_config import setup_logging
from nfl_mcp.metrics import get_metrics_collector

# Configure logging
setup_logging(
    log_level="INFO",
    service_name="nfl-mcp-server",
    enable_file_logging=True,
    log_file_path="/var/log/nfl_mcp.log"
)

# Configure metrics
metrics = get_metrics_collector()
```

## Integration with Monitoring Tools

### Prometheus + Grafana

1. Configure Prometheus to scrape metrics:
```yaml
scrape_configs:
  - job_name: 'nfl-mcp-server'
    static_configs:
      - targets: ['localhost:9000']
    metrics_path: '/metrics'
    scrape_interval: 15s
```

2. Import Grafana dashboard templates for visualization

### ELK Stack (Elasticsearch, Logstash, Kibana)

1. Configure Logstash to parse JSON logs
2. Set up Elasticsearch indices for log storage
3. Create Kibana dashboards for log analysis

### Alert Management

Set up alerts based on:
- Health check failures
- Error rate thresholds
- Performance degradation
- Resource usage limits

## Production Recommendations

### Monitoring Setup
1. Deploy centralized logging (ELK Stack, Fluentd)
2. Set up metrics collection (Prometheus/Grafana)
3. Configure alerting (AlertManager, PagerDuty)
4. Implement log retention policies

### Performance Optimization
1. Use log sampling for high-traffic environments
2. Configure metrics retention based on storage capacity
3. Implement efficient log shipping
4. Monitor monitoring system performance

### Security Considerations
1. Sanitize sensitive data in logs
2. Secure metrics endpoints
3. Implement log access controls
4. Regular monitoring system updates

## Troubleshooting

### Common Issues

**High Memory Usage**
- Check metrics retention settings
- Monitor log file growth
- Verify log rotation configuration

**Missing Metrics**
- Verify metrics collector initialization
- Check for middleware configuration
- Ensure proper timing decorator usage

**Health Check Failures**
- Review dependency connectivity
- Check system resource thresholds
- Verify external API accessibility

### Debug Mode

Enable detailed debugging:
```python
from nfl_mcp.logging_config import setup_logging

setup_logging(log_level="DEBUG")
```

## API Reference

### Logging Module (`nfl_mcp.logging_config`)
- `setup_logging()` - Configure structured logging
- `get_logger()` - Get logger instance
- `log_with_context()` - Log with additional context

### Metrics Module (`nfl_mcp.metrics`)
- `get_metrics_collector()` - Get metrics collector instance
- `timing_decorator()` - Decorator for timing functions
- `time_operation()` - Context manager for timing

### Monitoring Module (`nfl_mcp.monitoring`)
- `get_health_checker()` - Get health checker instance
- `HealthChecker.check_health()` - Perform health checks

### Middleware Module (`nfl_mcp.middleware`)
- `RequestLoggingMiddleware` - HTTP request/response logging

## Example: Complete Monitoring Setup

```python
from nfl_mcp.server import create_app
from nfl_mcp.logging_config import setup_logging, get_logger
from nfl_mcp.metrics import get_metrics_collector, timing_decorator

# Configure monitoring
setup_logging(
    log_level="INFO",
    enable_file_logging=True
)

logger = get_logger(__name__)
metrics = get_metrics_collector()

# Create monitored application
app = create_app()

# Example monitored function
@timing_decorator("business_operation", service="nfl_mcp")
async def monitored_operation():
    logger.info("Starting business operation")
    
    try:
        # Business logic here
        result = await some_operation()
        
        metrics.increment_counter("operations_success", operation="business")
        logger.info("Operation completed successfully", extra={"result_count": len(result)})
        
        return result
        
    except Exception as e:
        metrics.increment_counter("operations_error", operation="business")
        logger.error("Operation failed", extra={"error": str(e)})
        raise

# Run the server
if __name__ == "__main__":
    logger.info("Starting NFL MCP Server with monitoring")
    app.run(transport="http", port=9000, host="0.0.0.0")
```