# 🔍 ESPN Injury API Debug - Next Steps

## Was haben wir gemacht?

✅ **Debug-Logging hinzugefügt** zu `_fetch_injuries()`

**Was wird jetzt geloggt:**
```python
[DEBUG Injuries] ARI response keys: ['count', 'pageIndex', 'items', ...]
[DEBUG Injuries] ARI count: 0
[DEBUG Injuries] ARI pageCount: 0
[DEBUG Injuries] ARI items length: 0
[DEBUG Injuries] ARI empty response sample: {...}
```

---

## Was passiert als nächstes?

### ⏱️ Ohne Rebuild (Aktueller Server 0.5.3):

**Status:** Läuft ohne Debug-Logs  
**Problem:** Sehen nicht warum ESPN leer ist  
**Nächster Cycle:** In ~60 Sekunden (PREFETCH_INTERVAL=60)

**Logs zeigen:**
```
[Fetch Injuries] Successfully fetched 0 injury records  ❌
```

---

### 🔄 Nach Rebuild (Version 0.5.4):

**Benötigt:**
1. Docker Build (gtonic/nfl-mcp-server:0.5.4)
2. Container Restart
3. Warten auf Prefetch Cycle

**Logs werden zeigen:**
```
[DEBUG Injuries] ARI response keys: [...]
[DEBUG Injuries] ARI count: X
[DEBUG Injuries] ARI items length: Y
[DEBUG Injuries] ARI empty response sample: {...}
```

**Dann wissen wir:**
- ✅ Ist ESPN API leer? (count=0, items=[])
- ✅ Hat ESPN structure geändert? (different keys)
- ✅ Gibt es versteckte Daten? (count>0 aber items=[])

---

## 🎯 Was wir herausfinden werden

### Scenario 1: ESPN ist tatsächlich leer

**Debug Output:**
```
[DEBUG Injuries] ARI response keys: ['count', 'pageIndex', 'pageSize', 'pageCount', 'items']
[DEBUG Injuries] ARI count: 0
[DEBUG Injuries] ARI pageCount: 0
[DEBUG Injuries] ARI items length: 0
[DEBUG Injuries] ARI empty response sample: {
  "count": 0,
  "pageIndex": 1,
  "pageSize": 50,
  "pageCount": 0,
  "items": []
}
```

**Bedeutung:** ESPN hat **keine Injury-Daten** (Timing/Off-Week Issue)  
**Action:** Nichts zu fixen, warten bis Spielwoche

---

### Scenario 2: ESPN structure hat sich geändert

**Debug Output:**
```
[DEBUG Injuries] ARI response keys: ['injuries', 'metadata', 'timestamp']
[DEBUG Injuries] ARI count: N/A
[DEBUG Injuries] ARI pageCount: N/A
[DEBUG Injuries] ARI items length: 0
[DEBUG Injuries] ARI empty response sample: {
  "injuries": [
    {"athlete": {...}, "status": "Out", ...},
    {"athlete": {...}, "status": "Questionable", ...}
  ],
  "metadata": {...}
}
```

**Bedeutung:** API Response-Format geändert (`injuries` statt `items`)  
**Action:** Code anpassen auf neues Format

---

### Scenario 3: ESPN braucht andere Parameter

**Debug Output:**
```
[DEBUG Injuries] ARI response keys: ['message', 'error']
[DEBUG Injuries] ARI count: N/A
[DEBUG Injuries] ARI empty response sample: {
  "message": "Invalid season or missing parameter"
}
```

**Bedeutung:** API erwartet zusätzliche Parameter  
**Action:** Query-String anpassen

---

## 📊 Manuelle Validation (Parallel)

Während wir auf Logs warten, können Sie manuell testen:

### Check 1: ESPN Website
```
https://www.espn.com/nfl/team/injuries/_/name/kc
https://www.espn.com/nfl/team/injuries/_/name/buf
https://www.espn.com/nfl/team/injuries/_/name/sf
```

**Wenn Website Daten zeigt:**  
→ API problem, muss gefixed werden

**Wenn Website auch leer:**  
→ ESPN hat keine Daten, kein Code-Problem

---

### Check 2: Direct API Test

**Terminal:**
```bash
curl -s "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/KC/injuries?limit=50" | jq '.'
```

**Erwartete Response:**
```json
{
  "count": 0,
  "pageIndex": 1,
  "pageSize": 50,
  "pageCount": 0,
  "items": []
}
```

**Wenn anders:**  
→ Zeigt neues Format

---

### Check 3: Alternative ESPN Endpoints

**Roster API (hat manchmal Injury-Info):**
```bash
curl -s "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/KC/roster" | jq '.athletes[].injuries'
```

**Wenn das Daten hat:**  
→ Können wir als Fallback nutzen

---

## 🔄 Decision Tree

```
ESPN Injury API gibt 0 records
          │
          ├─→ Manual Check: ESPN Website hat Daten?
          │   ├─→ JA: API structure changed
          │   │   └─→ Fix code to match new structure
          │   │
          │   └─→ NEIN: ESPN hat keine Daten
          │       └─→ Normal, warten auf Spielwoche
          │
          └─→ Debug Logs: Was zeigen sie?
              ├─→ count=0, items=[]: Legitimately empty
              │   └─→ Keine Action nötig
              │
              ├─→ Different keys: Structure changed
              │   └─→ Fix parsing logic
              │
              └─→ Error message: API parameter issue
                  └─→ Fix query parameters
```

---

## 📝 Summary

**Current Status:**
- ✅ Debug logging added (committed)
- 🔄 Waiting for rebuild OR next prefetch cycle
- 🔍 Manual validation possible now

**Next Steps:**
1. **Option A:** Wait for next prefetch cycle (60s) → See logs
2. **Option B:** Rebuild to 0.5.4 → Fresh logs immediately
3. **Option C:** Manual curl test → Immediate insight

**Expected Outcome:**
- We'll know if ESPN API changed OR data is legitimately empty
- Can implement fix OR confirm no action needed

---

**Created:** October 25, 2025  
**Status:** Debug logging ready, waiting for data  
**Action Required:** Monitor logs OR manual test
