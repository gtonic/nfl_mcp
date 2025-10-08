# Snap Count Caching Strategy

## Overview

The NFL MCP Server uses a **multi-week fallback strategy** for snap count data to ensure data availability even when the current week's games haven't been played yet.

## Caching Strategy

### Data Source
- **Primary**: Sleeper API (`/stats/nfl/{season}/{week}`)
- **Storage**: `player_week_stats` table (player_id, season, week, snaps_offense, snap_pct)
- **Prefetch**: Current week + previous week (every prefetch cycle)

### Fallback Logic

When enriching roster data with snap percentages, the system follows this priority:

```
1. Try current week (Week N)
   ├─ Cache hit? → Use snap_pct from Week N ✅
   └─ Cache miss? → Fetch from API, retry cache
       ├─ Now cached? → Use snap_pct from Week N ✅
       └─ Still no data?
           └─ Try previous week (Week N-1)
               ├─ Cache hit? → Use snap_pct from Week N-1 ✅
               └─ Cache miss? → Fetch from API, retry cache
                   ├─ Now cached? → Use snap_pct from Week N-1 ✅
                   └─ Still no data?
                       └─ Estimate from depth chart (70%/45%/15%) 📊
```

### Week Tracking

The enriched data includes a `snap_pct_week` field to indicate which week's data was used:

```json
{
  "player_id": "4881",
  "full_name": "Rachaad White",
  "position": "RB",
  "snap_pct": 68.5,
  "snap_pct_week": 5,
  "snap_pct_source": "cached"
}
```

**Possible values for `snap_pct_source`**:
- `"cached"`: Data from database cache (Week N or N-1)
- `"estimated"`: Estimated from depth chart position (fallback)

## Implementation

### Roster Enrichment (`get_sleeper_league_rosters`)

**Location**: `sleeper_tools.py` lines 237-256

```python
# Snap pct enrichment with fallback
snap_row = nfl_db.get_player_snap_pct(pid, season, current_week)
snap_week_used = current_week

if not snap_row:
    await fetch_stats_if_needed(season, current_week)
    snap_row = nfl_db.get_player_snap_pct(pid, season, current_week)

# Fallback to previous week if current week has no data
if (not snap_row or snap_row.get("snap_pct") is None) and current_week > 1:
    snap_row = nfl_db.get_player_snap_pct(pid, season, current_week - 1)
    snap_week_used = current_week - 1
    if not snap_row:
        await fetch_stats_if_needed(season, current_week - 1)
        snap_row = nfl_db.get_player_snap_pct(pid, season, current_week - 1)

if snap_row and snap_row.get("snap_pct") is not None:
    obj["snap_pct"] = snap_row.get("snap_pct")
    obj["snap_pct_week"] = snap_week_used  # Track which week was used
    snap_source = "cached"
```

### General Enrichment (`_enrich_usage_and_opponent`)

**Location**: `sleeper_tools.py` lines 2377-2402

```python
# Snap pct (non-DEF) - try current week, fallback to previous week
if season and week and position not in (None, "DEF") and hasattr(nfl_db, 'get_player_snap_pct'):
    row = nfl_db.get_player_snap_pct(player_id, season, week)
    snap_week_used = week
    
    # If current week has no data, try previous week (games may not have been played yet)
    if (not row or row.get("snap_pct") is None) and week > 1:
        row = nfl_db.get_player_snap_pct(player_id, season, week - 1)
        snap_week_used = week - 1
        logger.debug(f"[Enrichment] {player_name}: Current week {week} has no snaps, trying week {week - 1}")
    
    if row and row.get("snap_pct") is not None:
        enriched_additions["snap_pct"] = row.get("snap_pct")
        enriched_additions["snap_pct_source"] = "cached"
        enriched_additions["snap_pct_week"] = snap_week_used  # Track which week was used
        logger.debug(f"[Enrichment] {player_name}: snap_pct={row.get('snap_pct')}% (cached from week {snap_week_used})")
```

## Prefetch Behavior

### Background Loop (`server.py` lines 137-150)

The prefetch loop fetches snap data for **both current and previous week**:

```python
# Snaps prefetch (current week + previous week as fallback)
snap_weeks_to_fetch = [week]
if week > 1:
    snap_weeks_to_fetch.append(week - 1)  # Add previous week

total_snap_rows_inserted = 0
for snap_week in snap_weeks_to_fetch:
    snap_rows = await _fetch_week_player_snaps(season, snap_week)
    if snap_rows:
        subset = snap_rows[:2000]
        inserted = nfl_db.upsert_player_week_stats(subset)
        total_snap_rows_inserted += inserted
```

**Expected Log Output**:
```
INFO - [Prefetch Cycle #1] Snaps (week 6): 0 rows inserted from 0 fetched
INFO - [Prefetch Cycle #1] Snaps (week 5): 487 rows inserted from 487 fetched
INFO - [Prefetch Cycle #1] Snaps total: 487 rows inserted across 2 weeks
```

## Why This Matters

### Current Week Scenario (Week 6, Games Not Played)

**Without Fallback**:
- ❌ Week 6 data: Not available (games not played)
- ❌ Snap percentages: Empty or estimated
- ❌ User sees: "Unklar (fehlend: snap_pct)"

**With Fallback**:
- ✅ Week 6 data: Not available (games not played)
- ✅ Falls back to Week 5 data automatically
- ✅ Snap percentages: Recent historical data (5-7 days old)
- ✅ User sees: `snap_pct: 68.5%` with `snap_pct_week: 5`

### Benefits

1. **Better Data Availability**: Always shows recent snap data, even mid-week
2. **Transparent Tracking**: `snap_pct_week` field shows data freshness
3. **Graceful Degradation**: Falls back to estimation only if both weeks fail
4. **Fantasy Context**: Recent snap trends are more valuable than no data

## Monitoring

### Check Snap Data Availability

```bash
# Check if snap data exists for current week
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db \
  "SELECT COUNT(*) FROM player_week_stats WHERE season=2025 AND week=6 AND snap_pct IS NOT NULL;"

# Check if snap data exists for previous week
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db \
  "SELECT COUNT(*) FROM player_week_stats WHERE season=2025 AND week=5 AND snap_pct IS NOT NULL;"
```

### Verify Fallback Behavior

```bash
# Check enrichment logs for fallback messages
docker logs nfl-mcp | grep "Current week.*has no snaps, trying week"

# Expected output during Week 6 (games not played)
[Enrichment] Rachaad White: Current week 6 has no snaps, trying week 5
[Enrichment] Rachaad White: snap_pct=68.5% (cached from week 5)
```

### Check Snap Data for Specific Player

```bash
# Query snap percentages for a player
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db \
  "SELECT player_id, season, week, snap_pct, updated_at 
   FROM player_week_stats 
   WHERE player_id='4881' 
   ORDER BY season DESC, week DESC 
   LIMIT 5;"
```

## Troubleshooting

### Problem: Snap percentages still showing "Unklar"

**Symptom**: Reports show "Unklar (fehlend: snap_pct)" even with fallback

**Possible Causes**:
1. **Prefetch not running**: `NFL_MCP_PREFETCH=1` not set
2. **Advanced enrichment disabled**: `NFL_MCP_ADVANCED_ENRICH=1` not set
3. **Both weeks empty**: Week 6 and Week 5 have no data

**Solution**:
```bash
# 1. Check environment variables
docker exec nfl-mcp env | grep NFL_MCP

# 2. Check prefetch logs
docker logs nfl-mcp | grep "Prefetch Cycle.*Snaps"

# 3. Check database
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db \
  "SELECT week, COUNT(*) as players 
   FROM player_week_stats 
   WHERE season=2025 AND snap_pct IS NOT NULL 
   GROUP BY week 
   ORDER BY week DESC;"
```

### Problem: Only seeing estimated snap percentages

**Symptom**: `snap_pct_source: "estimated"` instead of `"cached"`

**Cause**: No snap data available for current OR previous week

**Solution**: Wait for prefetch cycle to complete
```bash
# Check prefetch status
docker logs nfl-mcp --tail 100 | grep "Prefetch Cycle.*Completed"

# Expected: 
# [Prefetch Cycle #1] Snaps total: 487 rows inserted across 2 weeks
```

### Problem: Snap data for Week N-1 but still estimated

**Symptom**: Database has Week 5 data, but player shows estimated snap_pct

**Cause**: Player-specific data missing (didn't play, depth chart position)

**Solution**: Check specific player data
```bash
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db \
  "SELECT * FROM player_week_stats WHERE player_id='4881' AND week IN (5,6);"
```

## Estimation Fallback

If both Week N and Week N-1 have no data, the system estimates based on depth chart:

```python
def _estimate_snap_pct(depth_rank: Optional[int]) -> Optional[float]:
    if depth_rank is None:
        return None
    if depth_rank == 1:
        return 70.0  # Starter
    if depth_rank == 2:
        return 45.0  # Backup
    return 15.0      # Deep depth
```

**Example**:
```json
{
  "player_id": "9999",
  "full_name": "Backup Player",
  "position": "RB",
  "snap_pct": 45.0,
  "snap_pct_source": "estimated"
}
```

**Note**: No `snap_pct_week` field when estimated (not from historical data).

## Performance Impact

### API Calls Reduced
- **Before**: 1 API call per week per player (no cache)
- **After**: 0 API calls per week per player (prefetch + cache)
- **Savings**: ~500 API calls per prefetch cycle

### Cache Hit Rates (Expected)
- **Week N (games played)**: ~95% cache hit rate
- **Week N (games not played)**: ~5% cache hit rate (falls back to Week N-1)
- **Week N-1 (always available)**: ~95% cache hit rate
- **Combined**: ~90-95% cache hit rate with fallback

### Response Time
- **Cache Hit**: ~5-10ms per player
- **Cache Miss + API**: ~200-500ms per player
- **Estimation**: ~1ms per player

## Version History

- **v0.4.6**: Added fallback to Week N-1 for roster enrichment snap data
- **v0.4.5**: Added multi-week snap prefetch (Week N + Week N-1)
- **v0.4.4**: Initial snap count caching implementation
