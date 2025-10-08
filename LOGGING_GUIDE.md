# Logging Guide - NFL MCP Advanced Enrichment & Prefetch

## Overview

This document describes the comprehensive logging system for tracking advanced enrichment and prefetch operations controlled by the following environment flags:

- `NFL_MCP_ADVANCED_ENRICH=1` - Enables snap%, opponent, practice status, and usage enrichment
- `NFL_MCP_PREFETCH=1` - Enables background prefetch job
- `NFL_MCP_PREFETCH_INTERVAL=900` - Prefetch cycle interval (seconds)
- `NFL_MCP_PREFETCH_SNAPS_TTL=1800` - Snap data TTL before refetch (seconds)
- `NFL_MCP_LOG_LEVEL=INFO` - Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)

## Default Configuration

**Logging is enabled by default at INFO level.** This provides comprehensive tracking of:
- Prefetch cycle summaries
- Successful operations with row counts
- API errors and warnings
- Feature enablement status

To change the log level:
```bash
# More verbose (includes player-level enrichment details)
export NFL_MCP_LOG_LEVEL=DEBUG

# Less verbose (only warnings and errors)
export NFL_MCP_LOG_LEVEL=WARNING

# Quiet (errors only)
export NFL_MCP_LOG_LEVEL=ERROR
```

## Log Levels

| Level | Usage |
|-------|-------|
| **INFO** | Cycle summaries, successful operations, row counts |
| **WARNING** | Missing data, disabled features, API issues |
| **ERROR** | Failed operations, exceptions (with stack traces) |
| **DEBUG** | Detailed operation flow, player-level enrichment |

## Prefetch Loop Logging

### Startup Logs

```
INFO: Prefetch loop started (interval=900s, snaps_ttl=1800s)
```

### Per-Cycle Logs

Each prefetch cycle logs:

1. **Cycle Start**
```
INFO: [Prefetch Cycle #1] Starting at 2025-10-09T12:00:00Z
INFO: [Prefetch Cycle #1] NFL State: season=2025, week=6
```

2. **Schedule Fetch**
```
DEBUG: [Prefetch Cycle #1] Fetching schedule for season=2025, week=6
INFO: [Prefetch Cycle #1] Schedule: 32 rows inserted (season=2025 week=6)
```
or
```
ERROR: [Prefetch Cycle #1] Schedule fetch failed: HTTPError(404)
```

3. **Snaps Fetch**
```
DEBUG: [Prefetch Cycle #1] Fetching snaps for season=2025, week=6
DEBUG: [Prefetch Cycle #1] Received data for 1847 players
INFO: [Prefetch Cycle #1] Snaps: 1847 rows inserted from 1847 fetched (capped at 2000)
```

4. **Practice Reports** (Thu-Sat only)
```
DEBUG: [Prefetch Cycle #1] Current weekday: 4 (Fri)
DEBUG: [Prefetch Cycle #1] Fetching practice reports for season=2025, week=6
WARNING: [Prefetch Practice] Practice reports fetch not yet implemented (awaiting dedicated API)
```
or on other days:
```
DEBUG: [Prefetch Cycle #1] Practice: Skipped (only runs Thu-Sat)
```

5. **Usage Stats** (week > 1)
```
DEBUG: [Prefetch Cycle #1] Fetching usage stats for season=2025, week=5
DEBUG: [Fetch Usage] Received data for 2134 players
INFO: [Prefetch Cycle #1] Usage: 2134 rows inserted (week 5)
```

6. **Cycle Summary**
```
INFO: [Prefetch Cycle #1] Completed in 3.42s - Schedule: 32 rows, Snaps: 1847 rows, Practice: 0 rows, Usage: 2134 rows
INFO: [Prefetch Cycle #1] Next cycle in 900s
```

7. **Error Summary** (if any errors occurred)
```
WARNING: [Prefetch Cycle #1] Errors occurred - Schedule: OK, Snaps: HTTPError(503), Practice: OK, Usage: OK
```

## Fetch Helper Logging

### Schedule Fetch (`_fetch_week_schedule`)

```
INFO: [Fetch Schedule] Starting fetch for season=2025, week=6
DEBUG: [Fetch Schedule] Received 16 events from ESPN
INFO: [Fetch Schedule] Successfully fetched 32 game records (16 events, season=2025, week=6)
```

**Errors:**
```
WARNING: [Fetch Schedule] ESPN API returned status 404
ERROR: [Fetch Schedule] Failed for season=2025, week=6: ConnectionError('timeout')
```

### Snaps Fetch (`_fetch_week_player_snaps`)

```
INFO: [Fetch Snaps] Starting fetch for season=2025, week=6
DEBUG: [Fetch Snaps] Received data for 1847 players
INFO: [Fetch Snaps] Successfully fetched 1847 snap records (season=2025, week=6)
```

**Errors:**
```
WARNING: [Fetch Snaps] API returned status 503
ERROR: [Fetch Snaps] Failed for season=2025, week=6: JSONDecodeError()
```

### Practice Reports (`_fetch_practice_reports`)

```
INFO: [Fetch Practice] Starting fetch for season=2025, week=6
WARNING: [Fetch Practice] Practice reports fetch not yet implemented (awaiting dedicated API)
```

### Usage Stats (`_fetch_weekly_usage_stats`)

```
INFO: [Fetch Usage] Starting fetch for season=2025, week=5
DEBUG: [Fetch Usage] Received data for 2134 players
INFO: [Fetch Usage] Successfully fetched 2134 usage records (season=2025, week=5)
```

**Errors:**
```
WARNING: [Fetch Usage] Sleeper API returned status 503
WARNING: [Fetch Usage] No valid usage stats found in response
ERROR: [Fetch Usage] Sleeper API failed: Timeout()
WARNING: [Fetch Usage] No usage stats available from any source for season=2025, week=5
```

## Enrichment Logging

### Player-Level Enrichment (`_enrich_usage_and_opponent`)

Each enriched player logs:

```
DEBUG: [Enrichment] Processing CeeDee Lamb (id=4046, pos=WR, season=2025, week=6)
DEBUG: [Enrichment] CeeDee Lamb: snap_pct=92.3% (cached)
DEBUG: [Enrichment] CeeDee Lamb: practice_status=FP (age=18.5h)
DEBUG: [Enrichment] CeeDee Lamb: usage_last_3wks=tgt=11.3, routes=35.7, rz=2.0 (n=3)
INFO: [Enrichment] CeeDee Lamb: Added 7 enrichment fields
```

For DEF players:
```
DEBUG: [Enrichment] Cowboys DEF (id=DAL, pos=DEF, season=2025, week=6)
DEBUG: [Enrichment] Cowboys DEF (DEF): opponent=PHI (cached)
INFO: [Enrichment] Cowboys DEF: Added 2 enrichment fields
```

For estimated snap %:
```
DEBUG: [Enrichment] Brandin Cooks: snap_pct=45.0% (estimated from depth=2)
```

## Disabled Features Logging

When flags are not enabled:

```
INFO: Prefetch loop disabled: NFL_MCP_PREFETCH not set to 1
WARNING: Prefetch loop disabled: NFL_MCP_ADVANCED_ENRICH not set to 1
DEBUG: [Fetch Snaps] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled
DEBUG: [Fetch Schedule] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled
```

## Log Analysis Examples

### Count Successful Cycles
```bash
docker logs nfl-mcp-1 | grep "Prefetch Cycle.*Completed" | wc -l
```

### Check for Errors
```bash
docker logs nfl-mcp-1 | grep "ERROR.*Prefetch\|Fetch"
```

### Track Row Insertions
```bash
docker logs nfl-mcp-1 | grep "rows inserted"
```

### Monitor Schedule Fetches
```bash
docker logs nfl-mcp-1 | grep "\[Fetch Schedule\]"
```

### Monitor Snap Fetches
```bash
docker logs nfl-mcp-1 | grep "\[Fetch Snaps\]"
```

### Monitor Usage Stats
```bash
docker logs nfl-mcp-1 | grep "\[Fetch Usage\]"
```

### Track Enrichment Activity
```bash
docker logs nfl-mcp-1 | grep "\[Enrichment\].*Added.*fields"
```

### Get Cycle Summary Stats
```bash
docker logs nfl-mcp-1 | grep "Prefetch Cycle.*Completed in"
```

## Metrics to Track

Based on logs, you can track:

1. **Prefetch Performance**
   - Cycles completed
   - Average cycle duration
   - Rows inserted per data type
   - Error rate per fetch type

2. **Data Freshness**
   - Last successful schedule fetch
   - Last successful snaps fetch
   - Last successful usage stats fetch
   - Practice status age (hours)

3. **Enrichment Coverage**
   - Players enriched per request
   - Fields added per player
   - Cache hit rate (cached vs estimated)
   - DEF opponent resolution rate

4. **Reliability**
   - API errors per endpoint
   - Empty responses
   - Retry attempts
   - Timeout occurrences

## Troubleshooting

### No Prefetch Logs?
Check flags:
```bash
docker exec nfl-mcp-1 printenv | grep NFL_MCP
```

### High Error Rate?
Check API connectivity:
```bash
docker exec nfl-mcp-1 curl -I https://api.sleeper.app/v1/state/nfl
docker exec nfl-mcp-1 curl -I https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard
```

### Low Row Counts?
- Snaps/usage unavailable before games start
- Schedule only populated once per week
- Practice reports not yet implemented

### Stale Practice Status?
```bash
docker logs nfl-mcp-1 | grep "practice_status_stale.*true"
```

## Future Enhancements

1. **Structured Metrics Export**
   - Prometheus endpoint
   - InfluxDB integration
   - JSON metrics API

2. **Alerting**
   - Cycle failure notifications
   - High error rate alerts
   - Stale data warnings

3. **Dashboard**
   - Grafana visualization
   - Real-time prefetch status
   - Historical trend analysis

## Example Full Cycle Log

```
INFO: [Prefetch Cycle #1] Starting at 2025-10-09T12:00:00Z
INFO: [Prefetch Cycle #1] NFL State: season=2025, week=6
DEBUG: [Prefetch Cycle #1] Fetching schedule for season=2025, week=6
INFO: [Fetch Schedule] Starting fetch for season=2025, week=6
DEBUG: [Fetch Schedule] Received 16 events from ESPN
INFO: [Fetch Schedule] Successfully fetched 32 game records (16 events, season=2025, week=6)
INFO: [Prefetch Cycle #1] Schedule: 32 rows inserted (season=2025 week=6)
DEBUG: [Prefetch Cycle #1] Fetching snaps for season=2025, week=6
INFO: [Fetch Snaps] Starting fetch for season=2025, week=6
DEBUG: [Fetch Snaps] Received data for 1847 players
INFO: [Fetch Snaps] Successfully fetched 1847 snap records (season=2025, week=6)
INFO: [Prefetch Cycle #1] Snaps: 1847 rows inserted from 1847 fetched (capped at 2000)
DEBUG: [Prefetch Cycle #1] Current weekday: 4 (Fri)
DEBUG: [Prefetch Cycle #1] Fetching practice reports for season=2025, week=6
INFO: [Fetch Practice] Starting fetch for season=2025, week=6
WARNING: [Fetch Practice] Practice reports fetch not yet implemented (awaiting dedicated API)
INFO: [Prefetch Cycle #1] Practice: No rows returned (implementation pending)
DEBUG: [Prefetch Cycle #1] Fetching usage stats for season=2025, week=5
INFO: [Fetch Usage] Starting fetch for season=2025, week=5
DEBUG: [Fetch Usage] Received data for 2134 players
INFO: [Fetch Usage] Successfully fetched 2134 usage records (season=2025, week=5)
INFO: [Prefetch Cycle #1] Usage: 2134 rows inserted (week 5)
INFO: [Prefetch Cycle #1] Completed in 3.42s - Schedule: 32 rows, Snaps: 1847 rows, Practice: 0 rows, Usage: 2134 rows
INFO: [Prefetch Cycle #1] Next cycle in 900s
```
