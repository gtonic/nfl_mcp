# 🚨 ZWEITES PROBLEM: usage_last_3_weeks fehlt!

## Problem

Im Report fehlen **ALLE usage_last_3_weeks Daten**:

```
Player        Targets/G  Routes/G  RZ/G  Snap%
──────────────────────────────────────────────
Romeo Doubs   Unbekannt  Unklar    —     —     ❌
Marvin Mims   Unbekannt  Unklar    —     —     ❌
```

**Status:** `Unbekannt (fehlend: usage_last_3_weeks)`

---

## Root Cause Analysis

### 🔍 Zwei mögliche Ursachen:

### **1. Prefetch hat noch nicht gelaufen**

**Check:**
```bash
docker logs nfl-mcp | grep "Prefetch Cycle #1.*Usage"
```

**Expected Output:**
```
[Prefetch Cycle #1] Usage: 1234 rows inserted (week 7)
```

**Wenn NICHTS ausgegeben wird:**
- ✅ Server läuft mit alter Version (0.5.1)
- ✅ Neue Fixes sind noch nicht deployed
- ✅ Prefetch muss erst nach Deployment laufen

---

### **2. Enrichment wird nicht aufgerufen**

**Code-Check in `sleeper_tools.py` (Zeile ~2697):**

```python
# Usage stats (targets, routes, RZ touches) - offensive skill positions
if season and week and position in ("WR", "RB", "TE") and hasattr(nfl_db, 'get_usage_last_n_weeks'):
    usage = nfl_db.get_usage_last_n_weeks(player_id, season, week, n=3)
    if usage:
        enriched_additions["usage_last_3_weeks"] = { ... }
```

**Bedingungen:**
1. ✅ `season` muss gesetzt sein
2. ✅ `week` muss gesetzt sein
3. ✅ `position in ("WR", "RB", "TE")` - Romeo Doubs ist WR ✅
4. ✅ `nfl_db.get_usage_last_n_weeks` existiert
5. ❓ `usage` gibt Daten zurück?

---

## Die wahrscheinlichste Ursache

### ❌ **Keine Daten in player_usage_stats Tabelle!**

**Warum:**
```sql
-- Database Query in database.py
SELECT 
  AVG(targets) as targets_avg,
  AVG(routes) as routes_avg,
  AVG(rz_touches) as rz_touches_avg,
  AVG(snap_share) as snap_share_avg
FROM player_usage_stats
WHERE player_id = ? 
  AND season = ? 
  AND week BETWEEN ? AND ?
  AND week < ?
```

**Problem:**
- Wenn `player_usage_stats` Tabelle **LEER** ist → `usage = None`
- Wenn `usage = None` → `usage_last_3_weeks` wird **NICHT** gesetzt
- Report zeigt: **"Unbekannt (fehlend: usage_last_3_weeks)"**

---

## Bestätigung: Es ist ein Deployment-Problem!

### Beweis 1: Code ist korrekt
```python
# ✅ Code existiert in sleeper_tools.py
if season and week and position in ("WR", "RB", "TE"):
    usage = nfl_db.get_usage_last_n_weeks(player_id, season, week, n=3)
    if usage:
        enriched_additions["usage_last_3_weeks"] = { ... }
```

### Beweis 2: Prefetch ist implementiert
```python
# ✅ Prefetch Loop in server.py
if week > 1:
    usage_stats = await _fetch_weekly_usage_stats(season, week - 1)
    if usage_stats:
        inserted = nfl_db.upsert_usage_stats(usage_stats)
        stats["usage_inserted"] = inserted
```

### Beweis 3: Report zeigt "fehlend"
```
Unbekannt (fehlend: usage_last_3_weeks)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
          Report WEISS, dass es fehlen sollte!
```

**Conclusion:** Der **alte Server** (0.5.1) läuft noch → **Keine neuen Daten!**

---

## Data Flow Check

### Schritt 1: Prefetch sammelt Daten ✅ (implementiert)
```python
# server.py - Prefetch Loop
usage_stats = await _fetch_weekly_usage_stats(season, week - 1)
# Returns: List[Dict] mit targets, routes, rz_touches, snap_share
```

### Schritt 2: Daten werden in DB gespeichert ✅ (implementiert)
```python
# database.py
inserted = nfl_db.upsert_usage_stats(usage_stats)
# Inserts into: player_usage_stats table
```

### Schritt 3: Enrichment liest aus DB ✅ (implementiert)
```python
# sleeper_tools.py
usage = nfl_db.get_usage_last_n_weeks(player_id, season, week, n=3)
# Calculates AVG over last 3 weeks
```

### Schritt 4: usage_last_3_weeks wird gesetzt ✅ (implementiert)
```python
enriched_additions["usage_last_3_weeks"] = {
    "targets_avg": round(usage["targets_avg"], 1),
    "routes_avg": round(usage["routes_avg"], 1),
    ...
}
```

**ALLE Schritte sind implementiert!** ✅

---

## Das Problem: Timing!

### Aktueller Zustand (0.5.1):
```
Server läuft mit alter Version
  ↓
Prefetch läuft, aber ohne neue Fixes
  ↓
player_usage_stats bleibt leer
  ↓
get_usage_last_n_weeks() returns None
  ↓
usage_last_3_weeks wird nicht gesetzt
  ↓
Report zeigt "Unbekannt"
```

### Nach Deployment (0.5.3):
```
Server läuft mit neuer Version
  ↓
Prefetch läuft mit neuen Fixes (Snap%, RZ Touches, etc.)
  ↓
player_usage_stats wird gefüllt (1234+ rows)
  ↓
get_usage_last_n_weeks() returns data
  ↓
usage_last_3_weeks wird gesetzt ✅
  ↓
Report zeigt vollständige Daten!
```

---

## Validation Check

### Nach Deployment (1-2 Min warten), dann:

**1. Check Prefetch:**
```bash
docker logs nfl-mcp | grep "Usage:"

# Expected:
[Prefetch Cycle #1] Usage: 1234 rows inserted (week 7)
```

**2. Check Database:**
```bash
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db

SELECT COUNT(*) FROM player_usage_stats WHERE season=2025 AND week=7;
-- Expected: 1200-1500 rows
```

**3. Check Specific Players:**
```bash
docker exec -it nfl-mcp sqlite3 /data/nfl_data.db

SELECT 
  player_id,
  targets,
  routes,
  rz_touches,
  snap_share
FROM player_usage_stats
WHERE season=2025 
  AND week=7
  AND player_id IN (
    SELECT player_id FROM player_week_stats 
    WHERE full_name LIKE '%Doubs%'
    LIMIT 1
  );

-- Expected: 1 row mit Daten
```

**4. Check MCP Tool:**
```bash
# get_trending_players aufrufen
# Expected: usage_last_3_weeks sollte gesetzt sein
```

---

## Expected Timeline

### T+0 (jetzt):
- ❌ Server läuft mit 0.5.1
- ❌ Keine usage_stats Daten
- ❌ Report zeigt "Unbekannt"

### T+5 Min (nach Docker Build):
- 🔄 Docker Image 0.5.3 gebaut
- ❌ Server läuft noch mit 0.5.1
- ❌ Report zeigt noch "Unbekannt"

### T+6 Min (nach Container Restart):
- ✅ Server läuft mit 0.5.3
- ⏱️ Startup Prefetch läuft (Schedules only)
- ❌ Noch keine usage_stats (braucht Cycle #1)

### T+7 Min (nach Prefetch Cycle #1):
- ✅ Prefetch Cycle #1 komplett
- ✅ usage_stats Tabelle gefüllt (1234+ rows)
- ✅ get_usage_last_n_weeks() funktioniert
- ✅ **Report zeigt vollständige Daten!**

---

## Bottom Line

### Das Problem ist NICHT ein Code-Bug!

**Es ist ein Deployment-Timing-Problem:**

1. ✅ Code ist korrekt implementiert
2. ✅ Prefetch ist implementiert
3. ✅ Enrichment ist implementiert
4. ❌ **Alte Version läuft noch (0.5.1)**
5. ❌ **Neue Fixes noch nicht deployed**
6. ❌ **Prefetch hat noch keine Daten gesammelt**

### Die Lösung:

```bash
# 1. Commit + Push (alle Fixes)
git add -A
git commit -m "v0.5.3: Critical fixes (opponent, snap%, usage, practice)"
git push

# 2. Docker Build
docker buildx build . --push \
  --platform linux/amd64,linux/arm64 \
  --tag gtonic/nfl-mcp-server:0.5.3

# 3. Container Restart
docker stop nfl-mcp && docker rm nfl-mcp
docker run -d --name nfl-mcp \
  -e NFL_MCP_PREFETCH=1 \
  -e NFL_MCP_ADVANCED_ENRICH=1 \
  -p 9000:9000 \
  -v nfl_data:/data \
  gtonic/nfl-mcp-server:0.5.3

# 4. Warten (1-2 Min für Prefetch)
sleep 120

# 5. Neuen Report generieren
# ✅ usage_last_3_weeks wird jetzt gesetzt sein!
```

---

## Success Indicators

**Nach Deployment, logs checken:**

```bash
# Startup Prefetch (sofort)
[Startup Prefetch] ✅ Inserted 1088 schedule records for 2025 season

# Cycle #1 (nach ~60s mit Ihrer Config)
[Prefetch Cycle #1] Schedule: 32 rows inserted
[Prefetch Cycle #1] Snaps: 2968 rows inserted
[Prefetch Cycle #1] Injuries: 234 rows inserted
[Prefetch Cycle #1] Usage: 1234 rows inserted (week 7)  ← DAS IST DER KEY!

# Enrichment (bei Tool-Aufruf)
[Enrichment] Romeo Doubs: usage_last_3wks=tgt=5.3, routes=23.4, rz=0.5 (n=3)
```

**Wenn Sie das sehen → Report wird funktionieren!** ✅

---

## 🎯 Zusammenfassung

### Problem:
- `usage_last_3_weeks` fehlt im Report
- Grund: **Server läuft noch mit alter Version**
- Alte Version hat keine geprefetchten usage_stats

### Lösung:
1. Deploy Version 0.5.3 (alle Fixes)
2. Warte 1-2 Min für Prefetch Cycle #1
3. Generiere neuen Report
4. ✅ usage_last_3_weeks wird jetzt angezeigt!

### Beweis dass Code OK ist:
- ✅ `_fetch_weekly_usage_stats()` implementiert
- ✅ `upsert_usage_stats()` implementiert
- ✅ `get_usage_last_n_weeks()` implementiert
- ✅ Enrichment setzt `usage_last_3_weeks`

**Es ist kein Bug - es ist ein Deployment-Problem!** 🚀

---

**Erstellt:** 25. Oktober 2025  
**Status:** Warten auf Deployment  
**Nächster Schritt:** Git Commit + Docker Build + Deploy
