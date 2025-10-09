# Issue Resolution Summary: Snap%, Targets, Routes, and Touches Availability

## Issue Description

User reported that snap%, targets, routes, and touches were showing as "Unklar" (unavailable) in their reports, and asked:
1. What is the reason for this?
2. Can we fix it?
3. Would it make sense to have stats of the last 3 weeks to identify a trend?

## Investigation Findings

### The Good News
The system **already has comprehensive support** for all these metrics:

✅ **Database Schema**: Tables `player_week_stats` (snap%) and `player_usage_stats` (targets, routes, rz_touches, snap_share)

✅ **Data Fetching**: Prefetch loop fetches data from Sleeper API every 15 minutes

✅ **Enrichment System**: Adds `usage_last_3_weeks` with averages for all metrics

✅ **3-Week Tracking**: Already calculates rolling 3-week averages

### Root Causes of "Unklar"

Data appears as "Unklar" when one or more of these conditions is met:

1. **Environment Variables Not Set**
   - Requires `NFL_MCP_ADVANCED_ENRICH=1` to enable enrichment
   - Requires `NFL_MCP_PREFETCH=1` to enable background data fetching
   - Without these, enrichment is completely disabled

2. **Data Not Yet Available**
   - Current week games not played yet → no snap% data
   - Sleeper API hasn't published stats yet → no usage data
   - System falls back to previous week automatically

3. **Player-Specific Limitations**
   - Position doesn't track metric (e.g., QB doesn't have routes_run)
   - Player didn't play (injured, bye week)
   - Usage too low for Sleeper to record (< 1 target, etc.)

## Solutions Implemented

### 1. New Feature: Usage Trend Analysis ✨

Added automatic trend detection that identifies if a player's usage is increasing, decreasing, or staying flat:

**New Fields:**
- `usage_trend`: Per-metric trends (targets, routes, snap_share)
- `usage_trend_overall`: Overall trend direction ("up"/"down"/"flat")

**Algorithm:**
- Compares most recent week to average of prior weeks
- Threshold: 15% change required to mark as trending
- Returns "up" (↑), "down" (↓), or "flat" (→)

**Example Output:**
```json
{
  "player_id": "4046",
  "full_name": "CeeDee Lamb",
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

### 2. Database Enhancement

Added `get_usage_weekly_breakdown()` method for detailed week-by-week analysis:
- Returns individual week stats (not just averages)
- Ordered by week DESC (most recent first)
- Used for trend calculation

### 3. Bug Fixes

Fixed logging crash when usage values are None/NULL

### 4. Comprehensive Documentation

Created three new documentation files:

1. **USAGE_STATS_TROUBLESHOOTING.md**
   - Step-by-step diagnostic guide
   - How to check environment variables
   - How to verify data availability
   - Common issues and solutions

2. **USAGE_TREND_ANALYSIS.md**
   - How trend calculation works
   - Use cases (waiver wire, trade targets, breakout detection)
   - Interpretation guide for different positions
   - Configuration options

3. **Updated README.md and API_DOCS.md**
   - Document new trend fields
   - Usage examples

### 5. Comprehensive Testing

Added 13 new tests covering:
- Trend calculation algorithm (9 unit tests)
- Database operations (2 integration tests)
- Enrichment with trends (2 integration tests)
- All tests pass ✅

## How to Fix "Unklar" Issues

### Step 1: Set Environment Variables

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

### Step 2: Verify Prefetch is Running

```bash
# Wait 15-20 minutes after startup
docker logs nfl-mcp | grep "Prefetch Cycle" | tail -5
```

Expected output:
```
[Prefetch Cycle #1] Starting at 2025-10-09T17:00:00+00:00
[Prefetch Cycle #1] Schedule: 32 rows inserted
[Prefetch Cycle #1] Snaps (week 6): 1847 rows inserted
[Prefetch Cycle #1] Usage: 2134 rows inserted (week 5)
[Prefetch Cycle #1] Completed in 3.42s
```

### Step 3: Verify Database Has Data

```bash
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db << EOF
SELECT week, COUNT(*) as players 
FROM player_usage_stats 
WHERE season=2025 
GROUP BY week 
ORDER BY week DESC 
LIMIT 3;
EOF
```

Expected output:
```
week|players
5|2134
4|2089
3|1956
```

## What Changed in the Codebase

### Files Modified

1. **nfl_mcp/database.py**
   - Added `get_usage_weekly_breakdown()` method

2. **nfl_mcp/sleeper_tools.py**
   - Added `_calculate_usage_trend()` helper function
   - Updated `_enrich_usage_and_opponent()` to add trend fields
   - Fixed logging bug with None values

3. **API_DOCS.md**
   - Added documentation for trend fields

4. **README.md**
   - Updated enrichment schema with trend fields

### Files Added

1. **tests/test_usage_trend.py** (9 tests)
   - Unit tests for trend calculation

2. **tests/test_usage_integration.py** (4 tests)
   - Integration tests for database and enrichment

3. **USAGE_STATS_TROUBLESHOOTING.md**
   - Diagnostic and troubleshooting guide

4. **USAGE_TREND_ANALYSIS.md**
   - Trend feature documentation

## Impact Assessment

### Breaking Changes
**None** - All changes are additive

### New Features
- ✅ Trend calculation for usage metrics
- ✅ Weekly breakdown database method
- ✅ Comprehensive documentation

### Bug Fixes
- ✅ Logging crash with None values

### Performance Impact
- ✅ Minimal - One additional database query per player when enriching
- ✅ Query is efficient (indexed by player_id, season, week)

## Next Steps for User

1. **Enable Environment Variables** (see Step 1 above)
2. **Wait for Prefetch** (15-20 minutes after startup)
3. **Verify Data** (see Step 3 above)
4. **Use Trend Fields** in reports:
   - Check `usage_trend_overall` for quick assessment
   - Use `usage_trend.targets`, `.routes`, `.snap_share` for details
   - Map "up"→↑, "down"→↓, "flat"→→ in UI

## Common Questions

**Q: Why do some players still show "Unklar" after enabling?**

A: Could be:
- Games not played yet (wait until Monday after games)
- Player position doesn't track that metric (QB routes, etc.)
- Player has insufficient usage (< 1 target per game)
- Player was injured/didn't play that week

**Q: How accurate is the trend calculation?**

A: The 15% threshold is designed to catch meaningful changes while filtering noise. It works best with at least 3 weeks of data. Very early season (weeks 1-2) may not have enough data for reliable trends.

**Q: Can I adjust the trend threshold?**

A: Yes, modify the `THRESHOLD` constant in `_calculate_usage_trend()` function in `sleeper_tools.py`. Lower values (10%) are more sensitive, higher values (20%) are more conservative.

**Q: Does this work for all positions?**

A: Trend calculation works for WR, RB, TE positions. Other positions may not have all metrics tracked (e.g., QB doesn't have routes_run).

## Summary

✅ **Issue Resolved**: System already supports all requested metrics, just needs environment variables set

✅ **New Feature**: Automatic trend analysis to identify rising/falling players

✅ **Comprehensive Documentation**: Three new docs to help diagnose and use features

✅ **Fully Tested**: 13 new tests, all passing, no regressions

✅ **Zero Breaking Changes**: All additions are backward compatible

The system is production-ready and will provide accurate snap%, targets, routes, and touches data once environment variables are configured and prefetch has run.
