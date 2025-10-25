# Strategie: Umgang mit fehlenden RZ Touches

## Aktuelle Implementierung ✅

### Bereits implementiert in `_fetch_weekly_usage_stats()`:

```python
# Calculate RZ touches from multiple sources
rz_tgt = player_stats.get("rec_tgt_rz", 0) or 0      # RZ Targets (Pass-Catching)
rz_rush = player_stats.get("rush_att_rz", 0) or 0    # RZ Rush Attempts
rz_touches = rz_tgt + rz_rush

# If no explicit RZ data, estimate from TDs (TDs often happen in RZ)
if rz_touches == 0:
    rec_td = player_stats.get("rec_td", 0) or 0
    rush_td = player_stats.get("rush_td", 0) or 0
    rz_touches = rec_td + rush_td
```

**Status:** ✅ Gute Basis-Implementierung

---

## Problem-Analyse

### Wann fehlen RZ Touches?

1. **API hat keine Daten**
   - Sleeper API liefert `rec_tgt_rz` und `rush_att_rz` nicht für alle Spieler
   - Besonders bei Backups, Special Teamers, oder bestimmten Positionen

2. **Spieler hatte keine RZ Opportunities**
   - Team kam nicht in die Red Zone
   - Spieler war nicht auf dem Feld in RZ Situations
   - → **Das ist valide 0, kein "fehlend"!**

3. **Schätzung aus TDs ist ungenau**
   - TDs passieren nicht nur in Red Zone (z.B. 40-yard TD Pass)
   - Viele RZ Touches führen nicht zu TDs
   - → Unterestimation bei RZ-heavy Playern ohne TD

### Was zeigt die Datenbank?

```python
# In database.py - get_usage_last_n_weeks()
# Berechnet AVG(rz_touches) über 3 Wochen

# Problem: Wenn alle 3 Wochen = 0:
# - Ist das wirklich 0 RZ Touches?
# - Oder fehlen die Daten in der API?
```

---

## Verbesserungsvorschläge

### Option 1: Erweiterte API-Feldsuche ⭐ (Empfohlen)

**Verbesserung:** Mehr Sleeper API Felder versuchen

```python
# Current implementation
rz_tgt = player_stats.get("rec_tgt_rz", 0) or 0
rz_rush = player_stats.get("rush_att_rz", 0) or 0

# IMPROVED: Try multiple field names
rz_tgt = (
    player_stats.get("rec_tgt_rz") or 
    player_stats.get("rec_targets_rz") or 
    player_stats.get("redzone_targets") or
    0
)
rz_rush = (
    player_stats.get("rush_att_rz") or 
    player_stats.get("rush_attempts_rz") or 
    player_stats.get("redzone_rushes") or
    0
)
```

**Vorteil:** Mehr API-Kompatibilität  
**Aufwand:** Minimal  
**Risiko:** Niedrig

---

### Option 2: Intelligentere TD-basierte Schätzung ⭐⭐

**Problem:** Nicht alle TDs passieren in Red Zone

**Verbesserung:** Gewichtete Schätzung basierend auf Position

```python
if rz_touches == 0:
    rec_td = player_stats.get("rec_td", 0) or 0
    rush_td = player_stats.get("rush_td", 0) or 0
    total_tds = rec_td + rush_td
    
    if total_tds > 0:
        # Estimate RZ touches based on position
        # RBs have higher RZ touch-to-TD ratio than WRs
        position = player_stats.get("position", "")
        
        if position == "RB":
            # RBs: ~3-4 RZ touches per TD (more opportunities)
            rz_touches = total_tds * 3.5
        elif position in ["WR", "TE"]:
            # WR/TE: ~2-3 RZ touches per TD (more efficient)
            rz_touches = total_tds * 2.5
        else:
            # Default: Use TDs as minimum estimate
            rz_touches = total_tds
```

**Vorteil:** Realistischere Schätzungen  
**Aufwand:** Mittel  
**Risiko:** Mittel (Annahmen könnten falsch sein)

---

### Option 3: Markiere geschätzte vs echte Werte ⭐⭐⭐ (Beste Lösung)

**Problem:** User kann nicht unterscheiden zwischen:
- Echte 0 RZ Touches (Spieler hatte keine Opportunities)
- Geschätzte Werte aus TDs
- Fehlende Daten

**Lösung:** Zusätzliches Feld für Datenqualität

```python
# In database schema: Add rz_touches_source field
stats.append({
    "player_id": str(pid),
    "season": season,
    "week": week,
    "rz_touches": rz_touches,
    "rz_touches_source": "api" if (rz_tgt + rz_rush) > 0 else "estimated_from_tds",
    # ... other fields
})

# In enrichment: Show data quality
enriched_additions["usage_last_3_weeks"] = {
    "rz_touches_avg": round(usage["rz_touches_avg"], 1),
    "rz_touches_source": "api",  # or "estimated" or "mixed"
    "rz_touches_confidence": "high"  # or "medium" or "low"
}
```

**Vorteil:** Transparenz für den User  
**Aufwand:** Mittel  
**Risiko:** Niedrig (keine breaking changes, nur additive)

---

### Option 4: Null vs Zero Unterscheidung ⭐

**Problem:** `0` und `None` bedeuten unterschiedliches:
- `0` = Spieler hatte keine RZ Touches (valide Daten)
- `None` = Daten fehlen (API hat keine Info)

**Aktuell:**
```python
rz_tgt = player_stats.get("rec_tgt_rz", 0) or 0  # Defaultet zu 0
```

**Verbessert:**
```python
# Unterscheide zwischen "keine Daten" und "wirklich 0"
rz_tgt = player_stats.get("rec_tgt_rz")  # Kann None sein
rz_rush = player_stats.get("rush_att_rz")  # Kann None sein

if rz_tgt is not None and rz_rush is not None:
    # API hat explizite Daten
    rz_touches = rz_tgt + rz_rush
    rz_touches_source = "api"
else:
    # API hat keine Daten, schätze aus TDs
    rec_td = player_stats.get("rec_td", 0) or 0
    rush_td = player_stats.get("rush_td", 0) or 0
    rz_touches = rec_td + rush_td if (rec_td + rush_td) > 0 else None
    rz_touches_source = "estimated" if rz_touches else None
```

**Vorteil:** Echte 0 vs fehlende Daten unterscheidbar  
**Aufwand:** Mittel  
**Risiko:** Mittel (könnte downstream Code brechen der 0 erwartet)

---

## Empfohlene Implementierung

### Phase 1: Quick Win (5 Min) ⚡

Erweitere Feldsuche für bessere API-Kompatibilität:

```python
rz_tgt = (
    player_stats.get("rec_tgt_rz") or 
    player_stats.get("rec_targets_rz") or 
    player_stats.get("redzone_targets") or
    0
)
rz_rush = (
    player_stats.get("rush_att_rz") or 
    player_stats.get("rush_attempts_rz") or 
    player_stats.get("redzone_rushes") or
    0
)
```

### Phase 2: Transparenz (30 Min) 🎯

Füge `rz_touches_source` Feld hinzu:

```python
# Track how we got the value
rz_touches_source = "unknown"

rz_tgt = player_stats.get("rec_tgt_rz", 0) or 0
rz_rush = player_stats.get("rush_att_rz", 0) or 0
rz_touches = rz_tgt + rz_rush

if rz_touches > 0:
    rz_touches_source = "api"
else:
    # Fallback to TDs
    rec_td = player_stats.get("rec_td", 0) or 0
    rush_td = player_stats.get("rush_td", 0) or 0
    rz_touches = rec_td + rush_td
    if rz_touches > 0:
        rz_touches_source = "estimated_from_tds"
    else:
        rz_touches_source = "zero_or_missing"

# Store in DB
stats.append({
    "rz_touches": rz_touches,
    "rz_touches_source": rz_touches_source,
    # ... other fields
})
```

### Phase 3: Bessere Schätzung (Optional, 1h) 🔬

Position-basierte TD-Multiplikatoren:

```python
if rz_touches == 0 and (rec_td + rush_td) > 0:
    position = get_player_position(pid)  # From athlete data
    
    td_multipliers = {
        "RB": 3.5,   # RBs get more RZ opportunities per TD
        "WR": 2.5,   # WRs more efficient
        "TE": 2.5,   # TEs similar to WRs
        "QB": 1.0    # QBs: 1 TD ≈ 1 RZ play
    }
    
    multiplier = td_multipliers.get(position, 2.0)
    rz_touches = round((rec_td + rush_td) * multiplier, 1)
    rz_touches_source = "estimated_from_tds_position_adjusted"
```

---

## Datenbank-Schema Änderung

### Aktuelles Schema:
```sql
CREATE TABLE player_usage_stats (
    player_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    targets INTEGER,
    routes INTEGER,
    rz_touches INTEGER,  -- Nur der Wert
    ...
);
```

### Erweitertes Schema (Optional):
```sql
ALTER TABLE player_usage_stats 
ADD COLUMN rz_touches_source TEXT;  -- "api", "estimated_from_tds", "zero_or_missing"

-- Mögliche Werte:
-- "api" = Direkt von Sleeper API
-- "estimated_from_tds" = Geschätzt aus Touchdowns
-- "zero_or_missing" = Entweder wirklich 0 oder Daten fehlen
-- "estimated_position_adjusted" = Position-basierte Schätzung
```

---

## UI/Reporting Empfehlungen

### Wie den User informieren?

**Option A: Icon/Badge System**
```
RZ Touches/G: 2.3 ✓      (API data, high confidence)
RZ Touches/G: ~1.5 ⚠️     (Estimated from TDs)
RZ Touches/G: 0.0 ?      (Zero or missing data)
```

**Option B: Tooltip**
```
RZ Touches/G: 2.3
[i] Based on official Red Zone stats from Sleeper API
```

**Option C: Separate Confidence Field**
```json
{
  "rz_touches_avg": 2.3,
  "rz_touches_confidence": "high",
  "rz_touches_source": "api"
}
```

---

## Testing-Szenarien

### Test 1: Spieler mit expliziten RZ-Daten
```
Input: rec_tgt_rz=3, rush_att_rz=2
Expected: rz_touches=5, source="api"
```

### Test 2: Spieler ohne RZ-Daten aber mit TDs
```
Input: rec_tgt_rz=0, rush_att_rz=0, rec_td=1, rush_td=1
Expected: rz_touches=2, source="estimated_from_tds"
```

### Test 3: Spieler ohne Daten
```
Input: rec_tgt_rz=0, rush_att_rz=0, rec_td=0, rush_td=0
Expected: rz_touches=0, source="zero_or_missing"
```

### Test 4: Position-adjusted (wenn implementiert)
```
Input: position="RB", rec_td=0, rush_td=2
Expected: rz_touches=7.0, source="estimated_position_adjusted"
(2 TDs * 3.5 multiplier)
```

---

## Entscheidungsbaum

```
                     RZ Data verfügbar?
                           /    \
                         Ja      Nein
                         /          \
                  Verwende API    Hat TDs?
                   source="api"     /  \
                                  Ja   Nein
                                  /      \
                         Schätze aus TDs  0 oder Missing
                         source="est"    source="unknown"
                              |
                    Position bekannt?
                         /    \
                       Ja      Nein
                       /         \
              Multiplier     Einfach
              verwenden      TD = RZ
```

---

## Empfehlung: Was jetzt tun? 🎯

### Sofort (Quick Win):
1. ✅ **Erweitere API-Feldsuche** (5 Min, kein Risiko)
   - Mehr Feldnamen für `rz_tgt` und `rz_rush`

### Kurzfristig (Diese Woche):
2. ✅ **Füge `rz_touches_source` hinzu** (30 Min)
   - Transparenz für den User
   - Debugging wird einfacher

### Mittelfristig (Nächste Iteration):
3. ⚠️ **Position-basierte Schätzung** (Optional, 1h)
   - Nur wenn TD-basierte Schätzung zu ungenau ist
   - Erst nach Analyse der realen Daten

### NICHT empfohlen:
- ❌ **Null vs Zero** - Zu komplex, breaking changes
- ❌ **Komplexe ML-Modelle** - Overkill für dieses Problem

---

## Code-Änderung: Phase 1 (Quick Win)

```python
# In _fetch_weekly_usage_stats(), Zeile ~2400
# VORHER:
rz_tgt = player_stats.get("rec_tgt_rz", 0) or 0
rz_rush = player_stats.get("rush_att_rz", 0) or 0

# NACHHER:
rz_tgt = (
    player_stats.get("rec_tgt_rz") or 
    player_stats.get("rec_targets_rz") or 
    player_stats.get("redzone_targets") or
    0
)
rz_rush = (
    player_stats.get("rush_att_rz") or 
    player_stats.get("rush_attempts_rz") or 
    player_stats.get("redzone_rushes") or 
    player_stats.get("redzone_rush_attempts") or
    0
)
```

**Status:** Kann sofort implementiert werden ✅

---

**Zusammenfassung:**  
Die aktuelle Implementierung ist bereits gut (TD-Fallback). Empfehlung: Erweitere API-Feldsuche und füge `rz_touches_source` für Transparenz hinzu.

**Datum:** 25. Oktober 2025
