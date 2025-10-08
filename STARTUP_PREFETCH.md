# Startup Prefetch Strategy

## Overview

The NFL MCP Server now performs an **initial cache warm-up** on startup to ensure immediate data availability without waiting for the first prefetch cycle.

## What Gets Prefetched on Startup

### ✅ Team Schedules (All 32 Teams)

**Function**: `_fetch_all_team_schedules(season)`

- **When**: Once at server startup (before background loop starts)
- **Source**: ESPN Team Schedule API (`/teams/{TEAM}/schedule`)
- **Coverage**: All 32 NFL teams, complete season schedule
- **Data**: Week-by-week matchups, home/away status, kickoff times
- **Storage**: `schedule_games` table (bidirectional rows)
- **Benefit**: Immediate schedule access, no API calls needed for `get_team_schedule()`

**Expected Startup Log**:
```
INFO - [Startup Prefetch] Running initial cache warm-up...
INFO - [Startup Prefetch] Fetching schedules for all 32 teams (season=2025)...
INFO - [Fetch All Schedules] Starting fetch for 32 teams (season=2025)
INFO - [Fetch All Schedules] Completed: 32/32 teams successful, 1088 total game records fetched
INFO - [Startup Prefetch] ✅ Inserted 1088 schedule records for 2025 season
INFO - Background prefetch task started
```

**Why This Matters**:
- **Fantasy Context**: Schedule strength-of-schedule analysis immediately available
- **Matchup Analysis**: No delay when analyzing opponent matchups
- **Bye Week Planning**: All bye weeks instantly accessible
- **Performance**: ~32 API calls upfront vs. 32+ calls spread over time

## Background Prefetch Loop (Continues After Startup)

The background loop still runs every `NFL_MCP_PREFETCH_INTERVAL` seconds (default 15 minutes) and handles:

1. **Weekly Schedule Updates** (opponent matching for current week)
2. **Player Snaps** (current + previous week)
3. **Injury Reports** (all 32 teams, 12h TTL)
4. **Practice Status** (Thu-Sat only, extracted from injuries)
5. **Usage Stats** (targets, routes, RZ touches for previous week)

## Configuration

### Environment Variables

```bash
# Enable prefetch (required for both startup and background)
export NFL_MCP_PREFETCH=1

# Enable advanced enrichment (required for data fetching)
export NFL_MCP_ADVANCED_ENRICH=1

# Background loop interval (default 900s = 15 minutes)
export NFL_MCP_PREFETCH_INTERVAL=900
```

### Docker Run Example

```bash
docker run -d --name nfl-mcp \
  -e NFL_MCP_PREFETCH=1 \
  -e NFL_MCP_ADVANCED_ENRICH=1 \
  -e NFL_MCP_PREFETCH_INTERVAL=900 \
  -e NFL_MCP_LOG_LEVEL=INFO \
  -p 9000:9000 \
  -v nfl_data:/data \
  gtonic/nfl-mcp-server:0.4.6
```

## Performance Impact

### Startup Time
- **Without Startup Prefetch**: ~2-5 seconds
- **With Startup Prefetch**: ~10-15 seconds (32 team schedules)
- **Trade-off**: Slightly slower startup, but immediate data availability

### API Call Reduction
- **Before**: 1 API call per `get_team_schedule()` request (uncached)
- **After**: 0 API calls per `get_team_schedule()` request (100% cache hit rate)
- **Savings**: ~32 API calls eliminated per user session

### Cache Hit Rates (Expected After Startup)
- Team Schedules: **~100%** (all teams pre-loaded)
- Injuries: **~95%** (refreshed every 12h)
- Practice Status: **~90%** (Thu-Sat only)
- Player Snaps: **~85%** (current + previous week)
- Usage Stats: **~80%** (previous week only)

## Monitoring

### Verify Startup Prefetch Success

```bash
# Check Docker logs
docker logs nfl-mcp | grep "Startup Prefetch"

# Expected output
[Startup Prefetch] Running initial cache warm-up...
[Startup Prefetch] Fetching schedules for all 32 teams (season=2025)...
[Startup Prefetch] ✅ Inserted 1088 schedule records for 2025 season
```

### Verify Cache Hits

```bash
# Check team schedule cache hits
docker logs nfl-mcp | grep "Cache Hit.*Team schedule"

# Expected output
[Cache Hit] Team schedule for KC season 2025: 17 games from cache
[Cache Hit] Team schedule for SF season 2025: 17 games from cache
```

### Database Inspection

```bash
# Connect to container
docker exec -it nfl-mcp sh

# Query schedule cache
sqlite3 /data/nfl_data.db "SELECT team, COUNT(*) as games FROM schedule_games WHERE season=2025 GROUP BY team ORDER BY team;"

# Expected: 17 games per team (32 teams × 17 games = 544 rows, doubled for bidirectional = 1088)
```

## Troubleshooting

### Problem: Startup Prefetch Skipped

**Symptom**: Logs show "Prefetch disabled: NFL_MCP_ADVANCED_ENRICH not enabled"

**Solution**: Set environment variables
```bash
docker stop nfl-mcp
docker rm nfl-mcp
docker run -d --name nfl-mcp \
  -e NFL_MCP_PREFETCH=1 \
  -e NFL_MCP_ADVANCED_ENRICH=1 \
  -p 9000:9000 \
  gtonic/nfl-mcp-server:0.4.6
```

### Problem: Startup Prefetch Failed

**Symptom**: Logs show "❌ Failed to fetch team schedules: [error]"

**Causes**:
1. **Network Issue**: ESPN API unreachable
2. **API Rate Limit**: Too many requests
3. **Invalid Season**: Season not available

**Solution**: Check logs for specific error
```bash
docker logs nfl-mcp | grep "Startup Prefetch.*Failed"
```

### Problem: Partial Team Schedule Fetch

**Symptom**: Logs show "Failed teams: ARI, ATL, BAL"

**Solution**: Background loop will retry on next cycle (15 minutes)

**Workaround**: Restart container to re-trigger startup prefetch
```bash
docker restart nfl-mcp
```

## Future Enhancements

### Potential Additions to Startup Prefetch

1. **Historical Snap Data** (last 3 weeks)
   - Pre-load snap percentages for rolling averages
   - Benefit: Immediate usage trend analysis

2. **Team Rankings/Stats** (offensive/defensive rankings)
   - Pre-load matchup difficulty ratings
   - Benefit: Instant strength-of-schedule calculations

3. **Depth Charts** (all teams)
   - Pre-load depth chart positions
   - Benefit: Immediate snap projection estimates

4. **Player Projections** (weekly)
   - Pre-load consensus projections from multiple sources
   - Benefit: Instant waiver wire rankings

## Version History

- **v0.4.6**: Added startup prefetch for all team schedules
- **v0.4.5**: Added injury reports and practice status caching
- **v0.4.4**: Initial prefetch loop implementation
