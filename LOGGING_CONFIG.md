# Logging Configuration Summary

## Default Settings

✅ **INFO level logging is now enabled by default**

The NFL MCP Server automatically logs:
- Prefetch cycle operations and statistics
- Successful API calls with row counts  
- Enrichment activity summaries
- Warnings for missing features or data
- Errors with full stack traces

## Configuration

### Log Level (Optional)

Control verbosity via environment variable:

```bash
# Default: INFO (recommended for production)
export NFL_MCP_LOG_LEVEL=INFO

# Debug: Includes player-level enrichment details
export NFL_MCP_LOG_LEVEL=DEBUG

# Warning: Only warnings and errors
export NFL_MCP_LOG_LEVEL=WARNING

# Error: Only errors
export NFL_MCP_LOG_LEVEL=ERROR
```

### Log Format

All logs use this consistent format:
```
2025-10-09 12:00:00 - nfl_mcp.server - INFO - [Prefetch Cycle #1] Starting at 2025-10-09T12:00:00Z
│                    │                 │      │
│                    │                 │      └─ Message
│                    │                 └─ Level (INFO, WARNING, ERROR, DEBUG)
│                    └─ Module name
└─ Timestamp (YYYY-MM-DD HH:MM:SS)
```

## What Gets Logged at Each Level

### DEBUG
- Player-by-player enrichment details
- API request/response details
- Weekday detection logic
- Empty response reasons
- Cache hit/miss details

### INFO (Default)
- ✅ Prefetch cycle start/completion
- ✅ Row counts for inserted data
- ✅ NFL season/week detection
- ✅ Successful API calls
- ✅ Enrichment field additions
- ✅ Feature enablement status

### WARNING
- ⚠️ Missing or unavailable data
- ⚠️ API errors (4xx/5xx responses)
- ⚠️ Disabled features
- ⚠️ Stale practice status
- ⚠️ Empty API responses

### ERROR
- ❌ Failed operations
- ❌ Exceptions with stack traces
- ❌ API connection failures
- ❌ Database errors

## Quick Examples

### View Last 50 Logs
```bash
docker logs nfl-mcp-1 --tail 50
```

### Follow Logs in Real-Time
```bash
docker logs -f nfl-mcp-1
```

### Filter INFO Logs Only
```bash
docker logs nfl-mcp-1 | grep " INFO "
```

### Count Prefetch Cycles
```bash
docker logs nfl-mcp-1 | grep "Prefetch Cycle.*Completed" | wc -l
```

### Check for Errors
```bash
docker logs nfl-mcp-1 | grep " ERROR "
```

## Advanced Analysis

See [LOGGING_GUIDE.md](LOGGING_GUIDE.md) for:
- Detailed log format descriptions
- Example log outputs for each operation
- Performance metrics tracking
- Troubleshooting guides
- Log analysis commands

## Related Environment Variables

```bash
# Enable advanced enrichment (required for prefetch)
NFL_MCP_ADVANCED_ENRICH=1

# Enable prefetch background job
NFL_MCP_PREFETCH=1

# Prefetch interval (seconds)
NFL_MCP_PREFETCH_INTERVAL=900

# Snap data TTL (seconds)
NFL_MCP_PREFETCH_SNAPS_TTL=1800

# Log level
NFL_MCP_LOG_LEVEL=INFO
```

## Production Recommendations

**Recommended for Production:**
```bash
NFL_MCP_LOG_LEVEL=INFO
```

**For Development/Debugging:**
```bash
NFL_MCP_LOG_LEVEL=DEBUG
```

**For High-Traffic Production (minimal logging):**
```bash
NFL_MCP_LOG_LEVEL=WARNING
```

## Log Output Examples

### INFO Level (Default)
```
2025-10-09 12:00:00 - nfl_mcp.server - INFO - Logging initialized at INFO level
2025-10-09 12:00:01 - nfl_mcp.server - INFO - Prefetch loop started (interval=900s, snaps_ttl=1800s)
2025-10-09 12:00:02 - nfl_mcp.server - INFO - [Prefetch Cycle #1] Starting at 2025-10-09T12:00:02Z
2025-10-09 12:00:02 - nfl_mcp.server - INFO - [Prefetch Cycle #1] NFL State: season=2025, week=6
2025-10-09 12:00:03 - nfl_mcp.sleeper_tools - INFO - [Fetch Schedule] Starting fetch for season=2025, week=6
2025-10-09 12:00:04 - nfl_mcp.sleeper_tools - INFO - [Fetch Schedule] Successfully fetched 32 game records (16 events, season=2025, week=6)
2025-10-09 12:00:04 - nfl_mcp.server - INFO - [Prefetch Cycle #1] Schedule: 32 rows inserted (season=2025 week=6)
2025-10-09 12:00:05 - nfl_mcp.sleeper_tools - INFO - [Fetch Snaps] Starting fetch for season=2025, week=6
2025-10-09 12:00:06 - nfl_mcp.sleeper_tools - INFO - [Fetch Snaps] Successfully fetched 1847 snap records (season=2025, week=6)
2025-10-09 12:00:06 - nfl_mcp.server - INFO - [Prefetch Cycle #1] Snaps: 1847 rows inserted from 1847 fetched (capped at 2000)
2025-10-09 12:00:08 - nfl_mcp.server - INFO - [Prefetch Cycle #1] Completed in 6.12s - Schedule: 32 rows, Snaps: 1847 rows, Practice: 0 rows, Usage: 2134 rows
```

### DEBUG Level
Includes all INFO logs plus:
```
2025-10-09 12:00:03 - nfl_mcp.sleeper_tools - DEBUG - [Fetch Schedule] Received 16 events from ESPN
2025-10-09 12:00:05 - nfl_mcp.sleeper_tools - DEBUG - [Fetch Snaps] Received data for 1847 players
2025-10-09 12:00:10 - nfl_mcp.sleeper_tools - DEBUG - [Enrichment] Processing CeeDee Lamb (id=4046, pos=WR, season=2025, week=6)
2025-10-09 12:00:10 - nfl_mcp.sleeper_tools - DEBUG - [Enrichment] CeeDee Lamb: snap_pct=92.3% (cached)
2025-10-09 12:00:10 - nfl_mcp.sleeper_tools - DEBUG - [Enrichment] CeeDee Lamb: usage_last_3wks=tgt=11.3, routes=35.7, rz=2.0 (n=3)
```

## Notes

- Logs are output to stdout (captured by Docker logs)
- No log rotation configured (rely on Docker log rotation)
- Timestamps are in server local time
- All prefetch operations are tagged with `[Prefetch Cycle #N]` for easy filtering
- All fetch operations are tagged with `[Fetch Type]` (Schedule, Snaps, Practice, Usage)
- All enrichment operations are tagged with `[Enrichment]`
