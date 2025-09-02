# Configuration Management

The NFL MCP Server now supports flexible configuration management through environment variables and configuration files.

## Configuration Priority

Configuration is loaded in the following order (higher priority overrides lower):

1. **Environment Variables** (highest priority)
2. **Configuration Files** (YAML/JSON)
3. **Default Values** (lowest priority)

## Configuration Files

The server automatically looks for configuration files in these locations (in order):

1. `config.yml`
2. `config.yaml`
3. `config.json`
4. `/etc/nfl-mcp/config.yml`
5. `/etc/nfl-mcp/config.yaml`
6. `/etc/nfl-mcp/config.json`

You can also specify a custom configuration file when initializing the ConfigManager.

### YAML Configuration Example

```yaml
# HTTP timeouts
timeout:
  total: 30.0      # Total timeout in seconds
  connect: 10.0    # Connection timeout in seconds

# Server configuration
server:
  version: "1.0.0"

# Parameter validation limits
limits:
  nfl_news_max: 75
  athletes_search_max: 150

# Rate limiting
rate_limits:
  default_requests_per_minute: 120
  heavy_requests_per_minute: 20

# Security settings
security:
  max_string_length: 2000
  enable_injection_detection: true
  allowed_url_schemes:
    - "https://"
    - "http://"
```

### JSON Configuration Example

```json
{
  "timeout": {
    "total": 30.0,
    "connect": 10.0
  },
  "server": {
    "version": "1.0.0"
  },
  "limits": {
    "nfl_news_max": 75
  },
  "security": {
    "max_string_length": 2000
  }
}
```

## Environment Variables

All configuration options can be overridden using environment variables with the `NFL_MCP_` prefix:

### HTTP Timeouts
- `NFL_MCP_TIMEOUT_TOTAL` - Total HTTP timeout in seconds (default: 30.0)
- `NFL_MCP_TIMEOUT_CONNECT` - Connection timeout in seconds (default: 10.0)
- `NFL_MCP_LONG_TIMEOUT_TOTAL` - Long operation timeout in seconds (default: 60.0)
- `NFL_MCP_LONG_TIMEOUT_CONNECT` - Long operation connection timeout (default: 15.0)

### Server Configuration
- `NFL_MCP_SERVER_VERSION` - Server version string (default: "0.1.0")

### Validation Limits
- `NFL_MCP_NFL_NEWS_MAX` - Maximum NFL news items (default: 50)
- `NFL_MCP_NFL_NEWS_MIN` - Minimum NFL news items (default: 1)
- `NFL_MCP_ATHLETES_SEARCH_MAX` - Maximum athletes in search (default: 100)
- `NFL_MCP_ATHLETES_SEARCH_MIN` - Minimum athletes in search (default: 1)
- `NFL_MCP_ATHLETES_SEARCH_DEFAULT` - Default athletes to return (default: 10)
- `NFL_MCP_WEEK_MIN` - Minimum week number (default: 1)
- `NFL_MCP_WEEK_MAX` - Maximum week number (default: 22)
- `NFL_MCP_ROUND_MIN` - Minimum round number (default: 1)
- `NFL_MCP_ROUND_MAX` - Maximum round number (default: 18)
- `NFL_MCP_TRENDING_LOOKBACK_MIN` - Minimum trending lookback hours (default: 1)
- `NFL_MCP_TRENDING_LOOKBACK_MAX` - Maximum trending lookback hours (default: 168)
- `NFL_MCP_TRENDING_LIMIT_MIN` - Minimum trending players limit (default: 1)
- `NFL_MCP_TRENDING_LIMIT_MAX` - Maximum trending players limit (default: 100)

### Rate Limiting
- `NFL_MCP_RATE_LIMIT_DEFAULT` - Default requests per minute (default: 60)
- `NFL_MCP_RATE_LIMIT_HEAVY` - Heavy operations requests per minute (default: 10)
- `NFL_MCP_RATE_LIMIT_BURST` - Burst limit for consecutive requests (default: 5)

### Security Configuration
- `NFL_MCP_MAX_STRING_LENGTH` - Maximum string input length (default: 1000)
- `NFL_MCP_ENABLE_INJECTION_DETECTION` - Enable injection detection (default: true)
- `NFL_MCP_ALLOWED_URL_SCHEMES` - Comma-separated list of allowed URL schemes (default: "https://,http://")

## Environment Variable Examples

```bash
# Set custom timeouts
export NFL_MCP_TIMEOUT_TOTAL=45.0
export NFL_MCP_TIMEOUT_CONNECT=15.0

# Set server version
export NFL_MCP_SERVER_VERSION="1.2.0"

# Increase rate limits for production
export NFL_MCP_RATE_LIMIT_DEFAULT=120
export NFL_MCP_RATE_LIMIT_HEAVY=30

# Security settings
export NFL_MCP_MAX_STRING_LENGTH=2000
export NFL_MCP_ALLOWED_URL_SCHEMES="https://"
export NFL_MCP_ENABLE_INJECTION_DETECTION=true

# Run the server
python -m nfl_mcp.server
```

## Hot-Reloading

Configuration files support hot-reloading by default. When a configuration file is modified, the server will automatically reload the configuration. This feature can be disabled by setting `enable_hot_reload=False` when creating the ConfigManager.

### Hot-Reload Behavior
- File changes are detected automatically
- Configuration is validated before applying changes
- If validation fails, the old configuration is kept
- Invalid configurations are logged but don't crash the server

## Programmatic Usage

```python
from nfl_mcp.config_manager import ConfigManager, get_config_manager

# Use default configuration manager
config_manager = get_config_manager()

# Get configuration values
timeout = config_manager.get_http_timeout()
user_agent = config_manager.get_user_agent("nfl_news")
limits = config_manager.get_limits_dict()

# Create custom configuration manager
custom_manager = ConfigManager(
    config_file="./my-config.yml",
    enable_hot_reload=True
)

# Access configuration directly
config = custom_manager.config
print(f"Server version: {config.server.version}")
print(f"Max news items: {config.limits.nfl_news_max}")
```

## Deployment Examples

### Docker with Environment Variables

```dockerfile
FROM python:3.11
# ... (other Docker setup)

# Set configuration via environment variables
ENV NFL_MCP_TIMEOUT_TOTAL=45.0
ENV NFL_MCP_RATE_LIMIT_DEFAULT=120
ENV NFL_MCP_SERVER_VERSION=1.0.0

CMD ["python", "-m", "nfl_mcp.server"]
```

### Kubernetes ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: nfl-mcp-config
data:
  config.yml: |
    timeout:
      total: 45.0
      connect: 15.0
    rate_limits:
      default_requests_per_minute: 120
    security:
      max_string_length: 2000
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nfl-mcp-server
spec:
  template:
    spec:
      containers:
      - name: nfl-mcp
        env:
        - name: NFL_MCP_SERVER_VERSION
          value: "1.0.0"
        volumeMounts:
        - name: config
          mountPath: /app/config.yml
          subPath: config.yml
      volumes:
      - name: config
        configMap:
          name: nfl-mcp-config
```

### systemd Service with Configuration File

```ini
[Unit]
Description=NFL MCP Server
After=network.target

[Service]
Type=simple
User=nfl-mcp
WorkingDirectory=/opt/nfl-mcp
Environment=NFL_MCP_RATE_LIMIT_DEFAULT=120
Environment=NFL_MCP_SERVER_VERSION=1.0.0
ExecStart=/opt/nfl-mcp/venv/bin/python -m nfl_mcp.server
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Configuration Validation

All configuration values are validated using Pydantic models. Invalid configurations will raise detailed error messages:

```python
# Example validation error
ValueError: Configuration validation failed: 
  timeout.total: ensure this value is greater than 0
  limits.nfl_news_max: ensure this value is less than or equal to 1000
```

## Backward Compatibility

The new configuration system maintains full backward compatibility with existing code. All existing constants and functions in `nfl_mcp.config` continue to work as before:

```python
from nfl_mcp import config

# These still work exactly as before
timeout = config.DEFAULT_TIMEOUT
user_agents = config.USER_AGENTS
limits = config.LIMITS
rate_limits = config.RATE_LIMITS

# Functions continue to work
headers = config.get_http_headers("nfl_news")
client = config.create_http_client()
validated = config.validate_string_input("test")
```