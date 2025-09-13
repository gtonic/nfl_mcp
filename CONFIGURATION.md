# Configuration Guide

The NFL MCP Server supports flexible configuration through environment variables and configuration files.

## Configuration Priority (highest to lowest)
1. **Environment Variables** 
2. **Configuration Files** (YAML/JSON)
3. **Default Values**

## Configuration Files

The server automatically searches for config files in this order:
1. `config.yml` / `config.yaml` 
2. `config.json`
3. `/etc/nfl-mcp/config.yml` / `/etc/nfl-mcp/config.yaml`
4. `/etc/nfl-mcp/config.json`

## Configuration Options

### Environment Variables
```bash
# HTTP Timeouts
export NFL_MCP_TIMEOUT_TOTAL=30.0        # Total request timeout
export NFL_MCP_TIMEOUT_CONNECT=10.0      # Connection timeout

# Server Settings  
export NFL_MCP_SERVER_VERSION="1.0.0"    # Server version
export NFL_MCP_SERVER_PORT=9000          # Server port

# Request Limits
export NFL_MCP_NFL_NEWS_MAX=50           # Max news articles
export NFL_MCP_ATHLETES_SEARCH_MAX=100   # Max athlete search results

# Rate Limiting
export NFL_MCP_RATE_LIMIT_DEFAULT=60     # Requests per minute
export NFL_MCP_RATE_LIMIT_HEAVY=20       # Heavy operations per minute

# Security
export NFL_MCP_MAX_STRING_LENGTH=1000    # Max input string length
```

### YAML Configuration Example
```yaml
# config.yml
timeout:
  total: 30.0
  connect: 10.0

server:
  version: "1.0.0"
  port: 9000

limits:
  nfl_news_max: 50
  athletes_search_max: 100

rate_limits:
  default_requests_per_minute: 60
  heavy_requests_per_minute: 20

security:
  max_string_length: 1000
```

### JSON Configuration Example  
```json
{
  "timeout": {
    "total": 30.0,
    "connect": 10.0
  },
  "limits": {
    "nfl_news_max": 50,
    "athletes_search_max": 100
  },
  "rate_limits": {
    "default_requests_per_minute": 60
  }
}
```

## Docker Configuration

### Environment Variables
```bash
docker run --rm -p 9000:9000 \
  -e NFL_MCP_TIMEOUT_TOTAL=45.0 \
  -e NFL_MCP_NFL_NEWS_MAX=75 \
  gtonic/nfl-mcp-server
```

### Config File Mount
```bash  
docker run --rm -p 9000:9000 \
  -v ./config.yml:/app/config.yml \
  gtonic/nfl-mcp-server
```

## Complete Configuration Reference

| Setting | Environment Variable | Default | Description |
|---------|---------------------|---------|-------------|
| Total Timeout | `NFL_MCP_TIMEOUT_TOTAL` | 30.0 | HTTP request timeout (seconds) |
| Connect Timeout | `NFL_MCP_TIMEOUT_CONNECT` | 10.0 | Connection timeout (seconds) |
| Server Port | `NFL_MCP_SERVER_PORT` | 9000 | HTTP server port |
| NFL News Limit | `NFL_MCP_NFL_NEWS_MAX` | 50 | Max news articles returned |
| Athlete Search Limit | `NFL_MCP_ATHLETES_SEARCH_MAX` | 100 | Max athlete search results |
| Default Rate Limit | `NFL_MCP_RATE_LIMIT_DEFAULT` | 60 | Requests per minute |
| Heavy Rate Limit | `NFL_MCP_RATE_LIMIT_HEAVY` | 20 | Heavy operations per minute |
| Max String Length | `NFL_MCP_MAX_STRING_LENGTH` | 1000 | Max input string length |

## Configuration Validation

The server validates configuration on startup and provides clear error messages for invalid values. All configuration changes require a server restart to take effect.

For development, you can use the configuration manager programmatically:

```python
from nfl_mcp.config_manager import ConfigManager

config = ConfigManager()
config.load_configuration()  # Auto-discovers config files
print(config.get_timeout_total())  # Access configuration values
```