# 🚨 0 Injury Records Problem - Root Cause Analysis

## Problem

ESPN API gibt **200 OK** zurück, aber **0 injury records**:

```
HTTP Request: GET .../teams/KC/injuries?limit=50 "HTTP/1.1 200 OK"  ✅
HTTP Request: GET .../teams/BUF/injuries?limit=50 "HTTP/1.1 200 OK" ✅
...
[Fetch Injuries] Successfully fetched 0 injury records across 32 teams  ❌
```

**Alle Teams:** 200 OK  
**Alle Responses:** Leer (keine items)

---

## Mögliche Ursachen

### 1. ❌ ESPN API Struktur geändert

**Problem:** ESPN hat API Response-Format geändert

**Aktueller Code erwartet:**
```python
data = resp.json()
injuries_data = data.get('items', [])  # ← Erwartet 'items' key
```

**Mögliche neue Struktur:**
```json
{
  "injuries": [...],     // Statt 'items'
  "data": [...],         // Statt 'items'  
  "results": [...]       // Statt 'items'
}
```

---

### 2. ❌ ESPN API antwortet mit leeren Listen

**Mögliche Gründe:**
- **Timing:** Noch keine Injury Reports für aktuelle Woche (Week 8)
- **Season:** 2025 ist "Zukunft" (heute ist 25. Okt 2025, Season startet Sept)
- **Data lag:** ESPN aktualisiert nur bis zu bestimmten Zeiten

---

### 3. ✅ ESPN API braucht Authentication/Rate Limiting

**Unwahrscheinlich:**
- Status ist 200 OK (nicht 401/403)
- Alle Teams geben 200 OK
- Keine Rate-Limit Errors

---

### 4. ❌ Response ist paginiert und leer

**ESPN Pagination:**
```json
{
  "count": 0,           // Total items
  "pageIndex": 1,
  "pageSize": 50,
  "pageCount": 0,
  "items": []           // Leere Liste!
}
```

**Bedeutung:** ESPN hat tatsächlich **KEINE** Injury-Daten für diese Teams!

---

## Debugging Plan

### Schritt 1: Inspect Real Response

**Was wir sehen müssen:**
```bash
# Im Container
docker exec -it nfl-mcp python3 -c "
import asyncio
import httpx

async def test():
    async with httpx.AsyncClient() as client:
        resp = await client.get('https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/KC/injuries?limit=50')
        print('Status:', resp.status_code)
        print('Body:', resp.text[:1000])

asyncio.run(test())
"
```

---

### Schritt 2: Check ESPN Website

**Vergleich mit öffentlicher ESPN Seite:**
```
https://www.espn.com/nfl/team/injuries/_/name/kc
```

**Wenn Seite auch leer:** ESPN hat keine Injury-Daten (off-season?)  
**Wenn Seite voll:** API-Struktur geändert!

---

### Schritt 3: Alternative API Endpoint

**ESPN bietet mehrere Endpoints:**

**Option 1: Team Roster mit Injury Status**
```
https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team}/roster
```

**Option 2: Depthchart mit Injury Notes**
```
https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/2025/teams/{team}/depthcharts
```

**Option 3: Players Endpoint mit Injury Filter**
```
https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/athletes?active=true&injured=true
```

---

## Wahrscheinlichste Ursache

### 🎯 **Hypothesis: ESPN Injury API ist zeitlich begrenzt**

**Theorie:**
- ESPN publishes injury reports nur **während der Spielwoche**
- Typischerweise: **Donnerstag bis Sonntag**
- Heute ist **Freitag** (25. Oktober 2025)

**Aber:** Week 8 Games sind wahrscheinlich **erst am Wochenende**

**Check:**
```bash
# Sind Week 8 Games schon gespielt?
# Logs zeigen:
[Fetch Schedule] 13 events (Week 8)  ← Games existieren
[Fetch Snaps] Week 8: 0.0% coverage  ← Games NICHT gespielt!
```

**Conclusion:** Week 8 Games sind **noch nicht gespielt** → **ESPN hat noch keine Injury Reports veröffentlicht!**

---

## Die Lösung

### Option 1: Warten auf aktuelle Woche

**Problem:** Injury data nur verfügbar während Spielwoche  
**Timing:** Do-Sa vor Games (Fr 25. Okt ist in der Zeit!)

**Aber warum leer?**
- ESPN publishes nur für **diese Woche's Games**
- Games könnten erst **Sonntag/Montag** sein
- Injury Reports kommen **48h vor Game**

**Check Game Schedule:**
```
Week 8 Games: Wann sind sie?
- Sonntag 27. Okt? → Injury reports kommen Fr/Sa
- Donnerstag 24. Okt? → Injury reports bereits veröffentlicht
```

---

### Option 2: ESPN API changed structure

**Debug benötigt:**
```python
# Add logging in _fetch_injuries()
data = resp.json()
logger.info(f"[DEBUG] Team {team} response keys: {list(data.keys())}")
logger.info(f"[DEBUG] Team {team} count: {data.get('count', 'N/A')}")
logger.info(f"[DEBUG] Team {team} items length: {len(data.get('items', []))}")

if not data.get('items'):
    logger.info(f"[DEBUG] Team {team} full response: {json.dumps(data, indent=2)[:500]}")
```

---

### Option 3: Use alternative endpoint

**Fallback to Roster API:**
```python
# If injuries endpoint is empty, try roster
url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team}/roster"
resp = await client.get(url)
data = resp.json()

# Extract injuries from roster
for athlete in data.get('athletes', []):
    if athlete.get('injuries'):
        # Process injury data
        ...
```

---

## Immediate Action

### 1. Add Debug Logging

**File:** `nfl_mcp/sleeper_tools.py`  
**Function:** `_fetch_injuries()`  
**Line:** ~2260

**Add:**
```python
data = resp.json()

# DEBUG: Log response structure for first team
if team == "KC":  # Just log Kansas City as example
    logger.info(f"[DEBUG Injuries] KC response keys: {list(data.keys())}")
    logger.info(f"[DEBUG Injuries] KC count: {data.get('count')}")
    logger.info(f"[DEBUG Injuries] KC pageCount: {data.get('pageCount')}")
    logger.info(f"[DEBUG Injuries] KC items: {len(data.get('items', []))}")

injuries_data = data.get('items', [])
```

---

### 2. Check ESPN Website

**Manual Validation:**
```
https://www.espn.com/nfl/team/injuries/_/name/kc
https://www.espn.com/nfl/team/injuries/_/name/buf
https://www.espn.com/nfl/team/injuries/_/name/sf
```

**Wenn Daten sichtbar:**  
→ API structure changed, fix needed

**Wenn auch leer:**  
→ ESPN hat keine Injury-Daten (off-week/timing issue)

---

### 3. Implement Fallback

**Strategy:**
```python
async def _fetch_injuries():
    # Try Core API first
    injuries = await _fetch_injuries_core_api()
    
    if not injuries or len(injuries) == 0:
        # Fallback to Site API
        logger.info("[Fetch Injuries] Core API empty, trying Site API fallback")
        injuries = await _fetch_injuries_site_api()
    
    return injuries
```

---

## Expected Behavior

### Normal Operation (During Game Week):
```
[Fetch Injuries] Starting fetch for all teams
[Fetch Injuries] Successfully fetched 234 injury records across 32 teams
```

### Current Issue:
```
[Fetch Injuries] Starting fetch for all teams
[Fetch Injuries] Successfully fetched 0 injury records across 32 teams
```

### With Debug Logging:
```
[Fetch Injuries] Starting fetch for all teams
[DEBUG Injuries] KC response keys: ['count', 'pageIndex', 'pageSize', 'pageCount', 'items']
[DEBUG Injuries] KC count: 0
[DEBUG Injuries] KC pageCount: 0
[DEBUG Injuries] KC items: 0
[Fetch Injuries] Successfully fetched 0 injury records across 32 teams
```

---

## Summary

**Problem:** ESPN Injury API gibt 0 records zurück  
**Likely Cause:** Timing - Week 8 games noch nicht nah genug / Injury reports noch nicht publiziert  
**Alternative Cause:** API structure changed

**Next Steps:**
1. Add debug logging
2. Check ESPN website manually
3. Implement fallback to roster API if needed

**Impact:**
- Practice status: Relies on injury data → Will use default "FP"
- Injury enrichment: No data → Will show no injury info
- Overall: Not critical, system has fallbacks

---

**Created:** October 25, 2025  
**Status:** Investigation needed  
**Priority:** Medium (has fallbacks)  
**Type:** Data availability issue
