# 🐛 Washington Team Code Bug Fix

## Problem

ESPN API gibt **400 Bad Request** für "WAS":

```
HTTP Request: GET https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/WAS/injuries?limit=50 
"HTTP/1.1 400 Bad Request"
```

---

## Root Cause

**ESPN API hat Team-Code geändert:**
- **Alt:** `WAS` (Washington Football Team / Redskins)
- **Neu:** `WSH` (Washington Commanders)

**Grund:** Team-Rebranding 2022 → "Washington Commanders"

---

## The Fix

### File: `nfl_mcp/sleeper_tools.py`

**Zeile ~2240 in `_fetch_injuries()`:**

**Vorher:**
```python
teams = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WAS"  # ❌ 400 Error!
]
```

**Nachher:**
```python
teams = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WSH"  # ✅ Fixed!
]
```

---

## Impact

### Before Fix:
- ❌ Washington injury data: NEVER fetched (400 error)
- ❌ Prefetch logs: Shows 400 error every cycle
- ❌ Washington players: No injury data available

### After Fix:
- ✅ Washington injury data: Fetched successfully
- ✅ Prefetch logs: Clean (no 400 errors)
- ✅ Washington players: Full injury data available

---

## Potential Cross-API Issue

### ⚠️ Important Note:

**Different APIs use different codes:**

| API | Washington Code |
|-----|----------------|
| **ESPN API** | `WSH` ✅ |
| **Sleeper API** | `WAS` (?) |
| **NFL.com** | `WAS` or `WSH` (?) |

**Problem:** Wenn Sleeper API noch `WAS` verwendet, müssen wir **mappen**!

---

## Mapping Strategy (If Needed)

### Scenario: Sleeper uses "WAS", ESPN uses "WSH"

**Solution:** Add team code mapping

```python
# Team code mapping for cross-API compatibility
ESPN_TEAM_MAPPING = {
    "WAS": "WSH",  # Sleeper uses WAS, ESPN uses WSH
    # Add more if needed
}

def get_espn_team_code(sleeper_team: str) -> str:
    """Convert Sleeper team code to ESPN team code."""
    return ESPN_TEAM_MAPPING.get(sleeper_team, sleeper_team)

# Usage in _fetch_injuries():
for team in teams:
    espn_team = get_espn_team_code(team)
    url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{espn_team}/injuries"
```

---

## Testing

### Validate Fix:

```bash
# Check logs after deployment
docker logs nfl-mcp | grep "WAS"

# Before fix:
# HTTP/1.1 400 Bad Request  ❌

# After fix:
# (no error - should not appear) ✅
```

### Check Washington Injury Data:

```bash
docker logs nfl-mcp | grep "WSH"

# Should see:
# HTTP Request: GET ...teams/WSH/injuries "HTTP/1.1 200 OK" ✅
```

### Database Check:

```bash
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db

SELECT COUNT(*) 
FROM player_injuries 
WHERE team_id = 'WSH' OR team_id = 'WAS';

-- Expected: Some rows (Washington players with injuries)
```

---

## Version

**Fixed in:** v0.5.4 (next patch)  
**Priority:** Medium (non-critical, but causes log noise)  
**Impact:** Washington injury data now available

---

## Future-Proofing

### Watch for other team rebrands:
- **Las Vegas Raiders** (was Oakland) - Code: `LV` (not `OAK`)
- **Los Angeles Chargers** (was San Diego) - Code: `LAC` (not `SD`)
- **Los Angeles Rams** (was St. Louis) - Code: `LAR` (not `STL`)

All these are already correct in our code ✅

---

## Summary

**The Bug:**
```python
"WAS"  # ❌ ESPN doesn't recognize this anymore
```

**The Fix:**
```python
"WSH"  # ✅ Washington Commanders official code
```

**Result:** Injury data for Washington players now available! 🎯

---

**Created:** October 25, 2025  
**Fixed:** v0.5.4 (pending)  
**Type:** Minor bug (API compatibility)
