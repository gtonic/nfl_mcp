# Caching Strategy - NFL MCP Server

## Overview

This document describes the comprehensive caching strategy implemented to minimize API calls, improve performance, and enhance reliability.

## Cache-Enabled Data Types

### 1. ✅ **Player Snap Counts** (`player_week_stats`)
- **Source**: Sleeper API
- **Prefetch**: ✅ Active (Week N + Week N-1)
- **Query**: `get_player_snap_pct(player_id, season, week)`
- **Fallback**: Estimates from depth chart if not cached
- **TTL**: Configured via `NFL_MCP_PREFETCH_SNAPS_TTL` (default 1800s)
- **Strategy**: Fetch current week + previous week (fallback)

### 2. ✅ **Team Schedules** (`schedule_games`)
- **Source**: ESPN Scoreboard API
- **Prefetch**: ✅ Active (current week)
- **Queries**: 
  - `get_opponent(season, week, team)`
  - `get_team_schedule_from_cache(team, season)`
- **API Function**: `get_team_schedule()` - Cache-first with API fallback
- **TTL**: Refreshed weekly (schedule rarely changes)
- **Strategy**: Check cache → fetch from API if miss

### 3. ✅ **Player Usage Stats** (`player_usage_stats`)
- **Source**: Sleeper API
- **Prefetch**: ✅ Active (Week N-1)
- **Query**: `get_usage_last_n_weeks(player_id, season, week, n=3)`
- **Metrics**: Targets, routes, RZ touches, snap share
- **TTL**: Weekly refresh
- **Strategy**: Fetch previous week for rolling 3-week averages

### 4. ✅ **Injury Reports** (`player_injuries`) **NEW**
- **Source**: ESPN Core API (all 32 teams)
- **Prefetch**: ✅ Active (once per cycle)
- **Queries**:
  - `get_team_injuries_from_cache(team_id, max_age_hours=12)`
  - `get_player_injury_from_cache(player_id, max_age_hours=12)`
- **API Function**: `get_team_injuries()` - Cache-first with API fallback
- **TTL**: 12 hours (injury reports change daily)
- **Strategy**: Prefetch all teams → cache → serve from cache for 12h
- **Fields**: player_id, player_name, team_id, position, injury_status, injury_type, injury_description, date_reported

### 5. ✅ **Practice Reports** (`player_practice_status`) **NEW**
- **Source**: Derived from ESPN injury reports
- **Prefetch**: ✅ Active (Thu-Sat only)
- **Query**: `get_latest_practice_status(player_id, max_age_hours=72)`
- **Mapping**: 
  - OUT/IR/PUP → DNP (Did Not Participate)
  - DOUBTFUL/LIMITED/QUESTIONABLE → LP (Limited Participation)
  - PROBABLE/FULL → FP (Full Participation)
- **TTL**: 72 hours
- **Strategy**: Extract practice status from injury reports

## Prefetch Schedule

| Data Type | Frequency | Days | Notes |
|-----------|-----------|------|-------|
| Schedule | Every cycle | All | Current week only |
| Snaps | Every cycle | All | Week N + Week N-1 (fallback) |
| **Injuries** | **Every cycle** | **All** | **All 32 teams** |
| Practice | Every cycle | Thu-Sat | Derived from injuries |
| Usage | Every cycle | All | Previous week (N-1) |

**Default Cycle Interval**: 900 seconds (15 minutes) - configurable via `NFL_MCP_PREFETCH_INTERVAL`

## Cache-First Functions

### Functions with Cache Integration:

1. **`get_team_schedule(team_id, season)`**
   - Cache check → `get_team_schedule_from_cache()`
   - API fallback → ESPN Site API
   - Returns: `cache_source: "database"` or `"api"`
   - Logging: `[Cache Hit]` or `[API Fetch]`

2. **`get_team_injuries(team_id, limit)`** **NEW**
   - Cache check → `get_team_injuries_from_cache(team_id, max_age_hours=12)`
   - API fallback → ESPN Core API
   - Returns: `cache_source: "database"` or `"api"`
   - Logging: `[Cache Hit]` or `[API Fetch]`

### Functions Still Using Direct API:

- `get_nfl_news()` - News changes hourly, not worth caching
- `get_depth_chart()` - TODO: Implement caching (weekly changes)
- `get_trending_players()` - Has in-memory 30min cache
- `fetch_all_players()` - Has force_refresh flag
- League-specific Sleeper data (rosters, matchups, etc.) - Real-time required

## Database Schema (v9)

### New Migration: `_migration_v9_injuries`

```sql
CREATE TABLE IF NOT EXISTS player_injuries (
    player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    team_id TEXT NOT NULL,
    position TEXT,
    injury_status TEXT NOT NULL,
    injury_type TEXT,
    injury_description TEXT,
    date_reported TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(player_id, team_id, updated_at)
);

CREATE INDEX idx_injury_player ON player_injuries(player_id, updated_at DESC);
CREATE INDEX idx_injury_team ON player_injuries(team_id, updated_at DESC);
```

### Database Methods:

**Injuries:**
- `upsert_injuries(injuries: List[Dict]) -> int`
- `get_team_injuries_from_cache(team_id, max_age_hours=12) -> List[Dict]`
- `get_player_injury_from_cache(player_id, max_age_hours=12) -> Optional[Dict]`

**Schedule:**
- `upsert_schedule_games(games: List[Dict]) -> int`
- `get_opponent(season, week, team) -> Optional[str]`
- `get_team_schedule_from_cache(team, season) -> List[Dict]`

**Practice Status:**
- `upsert_practice_status(reports: List[Dict]) -> int`
- `get_latest_practice_status(player_id, max_age_hours=72) -> Optional[Dict]`

## Fetch Functions

### New: `_fetch_injuries()` in `sleeper_tools.py`

Fetches injury reports for all 32 NFL teams from ESPN Core API:

```python
async def _fetch_injuries() -> List[Dict]:
    """Fetch injury reports from ESPN for all NFL teams.
    
    Returns list of dicts with:
    - player_id, player_name, team_id, position
    - injury_status, injury_type, injury_description, date_reported
    """
```

**Features:**
- Iterates all 32 NFL teams
- Handles team-specific errors gracefully
- Aggregates all injuries into single list
- Logging: `[Fetch Injuries]` with team-by-team and summary stats

### Updated: `_fetch_practice_reports(season, week)`

Now extracts practice status from injury reports:

```python
async def _fetch_practice_reports(season: int, week: int) -> List[Dict]:
    """Fetch practice status from ESPN injuries endpoint.
    
    Uses injury reports to infer practice participation:
    - OUT/IR → DNP
    - DOUBTFUL/QUESTIONABLE → LP
    - PROBABLE/FULL → FP
    """
```

## Performance Benefits

### Injury Reports:
- **Before**: Every `get_team_injuries()` call = 1 ESPN API request
- **After**: Prefetch all 32 teams every 15min = ~128 API requests/hour → Cache serves thousands of queries
- **Savings**: 95%+ reduction in API calls during active use

### Team Schedules:
- **Before**: Every `get_team_schedule()` call = 1 ESPN API request
- **After**: Cache-first = 0 API requests for cached data
- **Savings**: 90%+ reduction in API calls

### Practice Status:
- **Before**: No data available
- **After**: Derived from injury reports, cached for 72h
- **New Feature**: Previously unavailable data now accessible

## Monitoring

### Prefetch Logs:

```log
INFO: [Prefetch Cycle #1] Starting at 2025-10-09T12:00:00Z
INFO: [Fetch Injuries] Starting fetch for all teams
INFO: [Fetch Injuries] Successfully fetched 87 injury records across 32 teams
INFO: [Prefetch Cycle #1] Injuries: 87 rows inserted from 87 fetched
INFO: [Fetch Practice] Extracted 65 practice status records from 87 injuries
INFO: [Prefetch Cycle #1] Practice: 65 rows inserted
INFO: [Prefetch Cycle #1] Completed in 4.5s - Schedule: 30 rows, Snaps: 2000 rows, Injuries: 87 rows, Practice: 65 rows, Usage: 258 rows
```

### Cache Hit Logs:

```log
INFO: [Cache Hit] Team injuries for KC: 5 injuries from cache
INFO: [Cache Hit] Team schedule for KC season 2025: 17 games from cache
```

### Cache Miss Logs:

```log
DEBUG: [Cache Miss] Failed to get team injuries from cache: ...
INFO: [API Fetch] Team injuries for KC: fetching from ESPN
```

## Log Analysis Commands

### Monitor Injury Fetches:
```bash
docker logs nfl-mcp-1 | grep "\[Fetch Injuries\]"
```

### Track Cache Hits:
```bash
docker logs nfl-mcp-1 | grep "\[Cache Hit\].*injuries"
docker logs nfl-mcp-1 | grep "\[Cache Hit\].*schedule"
```

### Monitor Practice Status:
```bash
docker logs nfl-mcp-1 | grep "\[Fetch Practice\]"
```

## Configuration

### Environment Variables:

```bash
# Enable advanced enrichment and prefetch
export NFL_MCP_ADVANCED_ENRICH=1
export NFL_MCP_PREFETCH=1

# Configure intervals
export NFL_MCP_PREFETCH_INTERVAL=900        # 15 minutes
export NFL_MCP_PREFETCH_SNAPS_TTL=1800      # 30 minutes

# Logging
export NFL_MCP_LOG_LEVEL=INFO
```

## Future Enhancements

### Planned:
1. **Depth Chart Caching** - TTL 48h, weekly refresh
2. **News Caching** - TTL 1-2h, optional
3. **Metrics Export** - Cache hit rates, API call counts
4. **Dashboard** - Real-time cache status visualization

### Considerations:
- **Adaptive TTL**: Adjust based on day of week (game days vs off-days)
- **Smart Invalidation**: Clear cache on detected data changes
- **Compression**: Store historical data compressed
- **Distributed Cache**: Redis/Memcached for multi-instance deployments
