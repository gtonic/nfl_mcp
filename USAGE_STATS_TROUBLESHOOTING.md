# Usage Stats Troubleshooting Guide

## Overview

This guide helps diagnose and fix issues with missing snap%, targets, routes, and touches data in player enrichment.

## Common Issue: "Unklar (fehlend: snap_pct)"

When players show `Unklar (fehlend: snap_pct)` or missing usage stats, it typically means the enrichment data is not available.

### Prerequisites

The advanced enrichment system requires **both** environment variables to be set:

```bash
NFL_MCP_ADVANCED_ENRICH=1  # Enables enrichment features
NFL_MCP_PREFETCH=1         # Enables background data fetching
```

### How It Works

1. **Prefetch Loop** (every 15 minutes by default):
   - Fetches schedule (opponent data for DEF)
   - Fetches snap percentages (current + previous week)
   - Fetches injury reports
   - Fetches practice status (Thu-Sat only)
   - Fetches usage stats (targets, routes, RZ touches) for previous week

2. **Enrichment** (on-demand when querying players):
   - Adds `snap_pct` from cached data (falls back to previous week if current unavailable)
   - Adds `usage_last_3_weeks` with averages for targets, routes, RZ touches, snap share
   - Adds `usage_trend` and `usage_trend_overall` based on recent performance
   - Adds injury and practice status

3. **Data Structure**:
   ```json
   {
     "player_id": "4046",
     "full_name": "CeeDee Lamb",
     "position": "WR",
     "snap_pct": 92.3,
     "snap_pct_source": "cached",
     "snap_pct_week": 5,
     "usage_last_3_weeks": {
       "targets_avg": 11.3,
       "routes_avg": 35.7,
       "rz_touches_avg": 2.0,
       "snap_share_avg": 91.5,
       "weeks_sample": 3
     },
     "usage_trend": {
       "targets": "up",
       "routes": "flat",
       "snap_share": "up"
     },
     "usage_trend_overall": "up"
   }
   ```

## Troubleshooting Steps

### Step 1: Check Environment Variables

```bash
# If using Docker
docker exec nfl-mcp env | grep NFL_MCP

# Expected output:
# NFL_MCP_ADVANCED_ENRICH=1
# NFL_MCP_PREFETCH=1
```

**Fix if missing:**
```bash
docker stop nfl-mcp
docker rm nfl-mcp
docker run -d --name nfl-mcp \
  -e NFL_MCP_ADVANCED_ENRICH=1 \
  -e NFL_MCP_PREFETCH=1 \
  -e NFL_MCP_PREFETCH_INTERVAL=900 \
  -p 9000:9000 \
  -v nfl_data:/data \
  gtonic/nfl-mcp-server:latest
```

### Step 2: Check Prefetch Logs

```bash
# Check if prefetch is running
docker logs nfl-mcp | grep "Prefetch Cycle" | tail -5

# Expected output (every 15 min):
# [Prefetch Cycle #1] Starting at 2025-10-09T17:00:00+00:00
# [Prefetch Cycle #1] NFL State: season=2025, week=6
# [Prefetch Cycle #1] Schedule: 32 rows inserted
# [Prefetch Cycle #1] Snaps (week 6): 1847 rows inserted
# [Prefetch Cycle #1] Usage: 2134 rows inserted (week 5)
# [Prefetch Cycle #1] Completed in 3.42s
```

**Common Issues:**
- "Prefetch loop disabled": Environment variables not set
- "Snaps: 0 rows": Current week not played yet (expected before games)
- "Usage: 0 rows": Week 1 or data not available yet
- No prefetch logs: Loop not starting (check environment)

### Step 3: Check Database

```bash
# Check if data is cached
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db << EOF
-- Check snap data availability
SELECT week, COUNT(*) as players 
FROM player_week_stats 
WHERE season=2025 AND snap_pct IS NOT NULL 
GROUP BY week 
ORDER BY week DESC 
LIMIT 5;

-- Check usage data availability  
SELECT week, COUNT(*) as players
FROM player_usage_stats
WHERE season=2025 AND targets IS NOT NULL
GROUP BY week
ORDER BY week DESC
LIMIT 5;
EOF
```

**Expected Output:**
```
week|players
6|1847
5|2134
4|2089
```

**If empty:**
- Prefetch hasn't run yet (wait 15 min or restart container)
- Sleeper API hasn't published data for that week yet
- Network issues preventing API calls

### Step 4: Check Specific Player

```bash
# Check raw data for a player
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db << EOF
SELECT * FROM player_usage_stats 
WHERE player_id='4046'  -- CeeDee Lamb
AND season=2025 
ORDER BY week DESC 
LIMIT 3;
EOF
```

### Step 5: Force Refresh

If data is stale or missing:

```bash
# Restart container to trigger immediate prefetch
docker restart nfl-mcp

# Watch logs for prefetch completion
docker logs -f nfl-mcp | grep "Prefetch Cycle"
```

## Understanding the Data

### Snap Percentage
- **Source**: ESPN/Sleeper player snap counts
- **Fallback**: If current week unavailable, uses previous week
- **Estimation**: If no data, estimates from depth chart (starter=70%, backup=45%, third=15%)

### Usage Stats (Targets, Routes, RZ Touches)
- **Source**: Sleeper weekly stats API
- **Coverage**: WR, RB, TE positions only
- **Timing**: Fetches previous week (current week typically not available until games played)
- **Calculation**: Rolling 3-week average

### Trend Calculation
- **Method**: Compares most recent week to average of prior weeks
- **Threshold**: 15% change required to mark as "up" or "down"
- **Values**: "up" (↑), "down" (↓), "flat" (→)
- **Priority**: Targets > Snap Share > Routes for overall trend

## Data Availability Timeline

| Time | Data Available |
|------|---------------|
| Monday | Previous week complete (Week N-1) |
| Tuesday-Thursday | Week N schedule, Week N-1 snaps/usage |
| Thursday-Saturday | Practice reports (DNP/LP/FP) |
| Sunday (during games) | Real-time injury updates |
| Sunday (after games) | Week N snaps start appearing |
| Monday morning | Week N usage stats finalized |

## Why Some Fields Show "Unklar"

1. **snap_pct**: 
   - Games not played yet this week
   - Player didn't play last week (injured/bye)
   - Not a skill position (QB, OL, etc.)

2. **targets_avg**:
   - Not a pass-catching position (non-WR/RB/TE)
   - Player hasn't received targets in last 3 weeks
   - Rookie with limited data

3. **routes_avg**:
   - Not a receiver (RB may have NULL routes)
   - Sleeper API doesn't track for this player
   - Usage too low to register

4. **rz_touches_avg**:
   - No red zone opportunities in last 3 weeks
   - Defense-focused player
   - Limited usage

## Best Practices

1. **Set environment variables** before starting container
2. **Wait 15-20 minutes** after startup for initial prefetch
3. **Check logs regularly** to ensure prefetch is working
4. **Monitor database size** to avoid disk space issues
5. **Use cached data** - don't request fresh data too frequently

## Advanced Configuration

```bash
# Prefetch interval (seconds, default 900 = 15 min)
NFL_MCP_PREFETCH_INTERVAL=600

# Snap data TTL (seconds, default 900 = 15 min)
NFL_MCP_PREFETCH_SNAPS_TTL=1800

# Log level for debugging
NFL_MCP_LOG_LEVEL=DEBUG
```

## Getting Help

If issues persist:

1. **Collect logs**: `docker logs nfl-mcp > debug.log`
2. **Check database**: Run SQL queries above
3. **Verify API access**: Ensure Sleeper API is reachable
4. **Report issue**: Include logs, environment, and specific player examples

## Related Documentation

- `SNAP_COUNT_CACHING.md` - Snap percentage caching strategy
- `STARTUP_PREFETCH.md` - Initial cache warming on startup
- `LOGGING_GUIDE.md` - Comprehensive logging documentation
- `API_DOCS.md` - Enrichment field specifications
