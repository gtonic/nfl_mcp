# Practice Status Fix - Quick Reference

## Problem
Players were showing "Unklar (fehlend: practice_status)" in roster tables.

## Solution
Enhanced enrichment to **always provide practice_status** for all players.

## Three-Tier Fallback System

### 1. Explicit Practice Reports (Highest Priority)
From database (ESPN injuries API, Thu-Sat)
```json
{"practice_status": "LP", "practice_status_date": "2025-01-15"}
```

### 2. Derive from Injury Status
| Injury Status | → | Practice Status |
|--------------|---|-----------------|
| Out, Reserve, PUP | → | DNP |
| Doubtful, Limited | → | LP |
| Questionable | → | LP |
| Probable, Full | → | FP |

```json
{"injury_status": "Questionable", "practice_status": "LP", "practice_status_source": "derived_from_injury"}
```

### 3. Default for Healthy Players
No injury = Fully practicing
```json
{"practice_status": "FP", "practice_status_source": "default_healthy"}
```

## Results

### Before
```
Player              | practice_status
--------------------|------------------
Christian McCaffrey | Unklar (fehlend)
Alvin Kamara       | Unklar (fehlend)
Jake Ferguson      | Unklar (fehlend)
```

### After
```
Player              | practice_status | Source
--------------------|-----------------|------------------
Christian McCaffrey | FP              | default_healthy
Alvin Kamara       | FP              | default_healthy
Jake Ferguson      | LP              | cached
```

## Files Changed
- `nfl_mcp/sleeper_tools.py` (+26 lines)
- `USAGE_STATS_TROUBLESHOOTING.md` (updated)
- `PRACTICE_STATUS_FIX.md` (detailed docs)
- `tests/test_practice_status.py` (6 unit tests)
- `tests/test_practice_status_integration.py` (integration test)

## Test Results
✅ 68 tests pass, 0 failures

## Impact
✅ No more "Unklar (fehlend: practice_status)"  
✅ All players have practice_status  
✅ Backward compatible  
✅ Transparent source tracking
