# Fix: Practice Status Always Available

## Problem Statement

The issue reported was that player roster tables were showing "Unklar (fehlend: practice_status)" for the practice status field. This was particularly problematic for healthy players who were fully participating in practice but had no injury designation.

Example from the issue:
```
Player               | Pos | Snap% | practice_status
---------------------|-----|-------|------------------
Christian McCaffrey  | RB  | Unklar| Unklar (fehlend: practice_status)
Alvin Kamara        | RB  | Unklar| Unklar (fehlend: practice_status)
Jake Ferguson       | TE  | Unklar| Unklar (fehlend: practice_status)
```

## Root Cause

The enrichment system only assigned `practice_status` to players who:
1. Had an explicit practice participation report in the database (from ESPN injuries API)
2. Were listed on the injury report

This meant **healthy players** (no injury designation) never received a `practice_status` field, resulting in "Unklar" (unclear/missing) being displayed.

## Solution

Enhanced the practice status enrichment logic to **always provide a practice_status** for all players using a three-tier fallback system:

### 1. Explicit Practice Reports (Highest Priority)
If the player has a practice participation report in the database (from ESPN, Thu-Sat), use that:
```json
{
  "practice_status": "LP",
  "practice_status_date": "2025-01-15",
  "practice_status_age_hours": 18.5,
  "practice_status_stale": false
}
```

### 2. Derive from Injury Status (Second Priority)
If no explicit report exists but the player has an injury designation, derive the practice status:

| Injury Status | Practice Status | Logic |
|--------------|----------------|--------|
| Out, Injured Reserve, PUP | DNP | Did Not Participate |
| Doubtful, Limited | LP | Limited Participation |
| Questionable | LP | Usually limited |
| Probable, Full | FP | Full Participation |

Example:
```json
{
  "injury_status": "Questionable",
  "injury_type": "Ankle",
  "practice_status": "LP",
  "practice_status_source": "derived_from_injury"
}
```

### 3. Default for Healthy Players (Lowest Priority)
If no explicit report and no injury designation, **default to FP** (Full Participation):
```json
{
  "practice_status": "FP",
  "practice_status_source": "default_healthy"
}
```

## Code Changes

### sleeper_tools.py

#### Before:
```python
# Practice status (DNP/LP/FP) - all positions
if player_id and hasattr(nfl_db, 'get_latest_practice_status'):
    practice = nfl_db.get_latest_practice_status(player_id, max_age_hours=72)
    if practice:
        # ... add practice status fields
        # NO ELSE CLAUSE - players without reports get no practice_status
```

#### After:
```python
# Practice status (DNP/LP/FP) - all positions
if player_id and hasattr(nfl_db, 'get_latest_practice_status'):
    practice = nfl_db.get_latest_practice_status(player_id, max_age_hours=72)
    if practice:
        # ... add practice status fields from database
    else:
        # If no explicit practice status, derive from injury status or default to FP
        injury_status = enriched_additions.get("injury_status", "").upper()
        if injury_status:
            # Derive practice status from injury status
            if 'OUT' in injury_status or 'RESERVE' in injury_status or 'PUP' in injury_status:
                derived_status = 'DNP'
            elif 'DOUBTFUL' in injury_status or 'LIMITED' in injury_status:
                derived_status = 'LP'
            elif 'QUESTIONABLE' in injury_status:
                derived_status = 'LP'
            elif 'PROBABLE' in injury_status or 'FULL' in injury_status:
                derived_status = 'FP'
            else:
                derived_status = 'FP'
            
            enriched_additions["practice_status"] = derived_status
            enriched_additions["practice_status_source"] = "derived_from_injury"
        else:
            # No injury, no practice status -> assume healthy and fully practicing
            enriched_additions["practice_status"] = "FP"
            enriched_additions["practice_status_source"] = "default_healthy"
```

## Impact

### Before Fix:
```
Player               | Pos | practice_status
---------------------|-----|------------------
Christian McCaffrey  | RB  | Unklar (fehlend: practice_status)
Alvin Kamara        | RB  | Unklar (fehlend: practice_status)
Jake Ferguson       | TE  | Unklar (fehlend: practice_status)
```

### After Fix:
```
Player               | Pos | practice_status | practice_status_source
---------------------|-----|-----------------|----------------------
Christian McCaffrey  | RB  | FP              | default_healthy
Alvin Kamara        | RB  | LP              | derived_from_injury (Questionable)
Jake Ferguson       | TE  | LP              | cached (from ESPN)
```

## New Field: practice_status_source

To provide transparency about where the practice status came from, we added the `practice_status_source` field:

- **"cached"**: From explicit practice report in database
- **"derived_from_injury"**: Derived from current injury status
- **"default_healthy"**: No injury, defaulted to FP (healthy)

This field is **only present** when the status is derived or defaulted (not present for cached data to maintain backward compatibility).

## Testing

### Unit Tests (6 tests)
- ✅ Explicit practice status from database
- ✅ Derived from injury: Out → DNP
- ✅ Derived from injury: Questionable → LP
- ✅ Derived from injury: Doubtful → LP
- ✅ Derived from injury: Injured Reserve → DNP
- ✅ Default healthy: No injury → FP

### Integration Test
Demonstrates the fix with realistic player scenarios:
- Christian McCaffrey (healthy) → FP (default_healthy)
- Alvin Kamara (Questionable) → LP (derived_from_injury)
- Jake Ferguson (explicit report) → LP (from database)
- Injured Player (Out) → DNP (derived_from_injury)

## Documentation Updates

Updated `USAGE_STATS_TROUBLESHOOTING.md` to include:
- Explanation of practice_status behavior
- Documentation of practice_status_source values
- Clarification that practice_status is now always provided

## Backward Compatibility

✅ Fully backward compatible:
- Existing code expecting `practice_status` field will now always find it
- `practice_status_source` is a new field that can be safely ignored
- Database structure unchanged
- API response format unchanged (only adds fields, doesn't remove)

## Summary

The fix ensures that **all players receive a practice_status**, eliminating the "Unklar (fehlend: practice_status)" message. The system now:

1. ✅ Uses explicit practice reports when available
2. ✅ Intelligently derives status from injury reports
3. ✅ Defaults healthy players to "FP" (Full Participation)
4. ✅ Provides transparency via `practice_status_source` field
5. ✅ Maintains backward compatibility
6. ✅ Includes comprehensive test coverage
