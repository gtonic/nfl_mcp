# Strategie: Fehlende Snap Counts fixen

## Aktuelle Situation - Analyse ✅

### Was wir bereits haben:

**1. In `_fetch_weekly_usage_stats()` (Zeile ~2436):**
```python
# Get snap percentage - try multiple field names
snap_share = player_stats.get("snap_pct") or player_stats.get("off_snp_pct") or player_stats.get("snap_share")
```
✅ **Gut:** Bereits 3 Feldnamen werden geprüft

**2. In `_enrich_usage_and_opponent()` (Zeile ~2550+):**
```python
# Snap pct (non-DEF) - try current week, fallback to previous week
if season and week and position not in (None, "DEF"):
    row = nfl_db.get_player_snap_pct(player_id, season, week)
    
    # Fallback zu vorheriger Woche
    if (not row or row.get("snap_pct") is None) and week > 1:
        row = nfl_db.get_player_snap_pct(player_id, season, week - 1)
    
    if row and row.get("snap_pct") is not None:
        enriched_additions["snap_pct"] = row.get("snap_pct")
        enriched_additions["snap_pct_source"] = "cached"
    else:
        # Schätzung basierend auf Depth Chart
        est = _estimate_snap_pct(depth_rank)
        if est is not None:
            enriched_additions["snap_pct"] = est
            enriched_additions["snap_pct_source"] = "estimated"
```

✅ **Gut:** Fallback-Kette existiert bereits:
1. Aktuelle Woche aus DB
2. Vorherige Woche aus DB
3. Schätzung aus Depth Chart

**3. Schätzungsfunktion `_estimate_snap_pct()`:**
```python
def _estimate_snap_pct(depth_rank: Optional[int]) -> Optional[float]:
    if depth_rank is None:
        return None
    if depth_rank == 1:
        return 70.0  # Starter
    if depth_rank == 2:
        return 45.0  # Backup
    return 15.0      # Third string
```

✅ **Vernünftig:** Position-basierte Defaults

---

## Probleme identifiziert 🔍

### Problem 1: Keine Snap Count Extraktion aus Weekly Stats ❌

**Aktuell in `_fetch_weekly_usage_stats()`:**
```python
snap_share = player_stats.get("snap_pct") or player_stats.get("off_snp_pct") or player_stats.get("snap_share")
```

**Aber:** Sleeper API liefert oft auch **absolute Snap Counts**:
- `off_snp` = Offensive snaps (absolute Zahl)
- `team_snp` = Team snaps total
- **snap_share = off_snp / team_snp * 100**

**→ Wir könnten snap_share selbst berechnen!**

---

### Problem 2: Keine Datenquellen-Transparenz ⚠️

Aktuell gibt es `snap_pct_source`:
- `"cached"` = Aus Datenbank
- `"estimated"` = Geschätzt aus Depth Chart

**Aber fehlt:**
- `"calculated"` = Selbst berechnet aus off_snp / team_snp
- `"api"` = Direkt von API

---

### Problem 3: Keine Team-Snaps Nutzung ⚠️

Sleeper API liefert manchmal:
```json
{
  "off_snp": 45,      // Spieler Snaps
  "team_snp": 65,     // Team Total Snaps
  "snap_pct": null    // Manchmal nicht berechnet!
}
```

**→ Wir könnten `snap_pct` selbst berechnen: 45/65 = 69.2%**

---

### Problem 4: Positions-spezifische Schätzungen zu grob 📊

Aktuelle Schätzung:
- Depth 1 = 70%
- Depth 2 = 45%
- Depth 3 = 15%

**Problem:** Unterschiede zwischen Positionen:
- **RB:** Starters oft nur 50-60% (Rotation)
- **WR:** #1 WR oft 90%+
- **TE:** Blocking TEs nur 40-50%
- **QB:** Starter 95%+

---

## Verbesserungsvorschläge 💡

### Option 1: Berechne snap_share aus off_snp ⭐⭐⭐ (EMPFOHLEN)

**Neu:** Wenn snap_share fehlt, aber off_snp vorhanden:

```python
# Get snap percentage - try multiple sources
snap_share = (
    player_stats.get("snap_pct") or 
    player_stats.get("off_snp_pct") or 
    player_stats.get("snap_share")
)
snap_share_source = "api" if snap_share else None

# NEW: Calculate from absolute snaps if not provided
if not snap_share:
    off_snp = player_stats.get("off_snp")
    team_snp = player_stats.get("team_snp") or player_stats.get("tm_off_snp")
    
    if off_snp is not None and team_snp is not None and team_snp > 0:
        snap_share = round((off_snp / team_snp) * 100, 1)
        snap_share_source = "calculated_from_snaps"
```

**Vorteile:**
- ✅ Mehr Daten verfügbar (auch wenn snap_pct fehlt)
- ✅ Genauer als Schätzung
- ✅ Nutzt vorhandene API-Daten besser

**Aufwand:** Mittel (30 Min)
**Risiko:** Niedrig

---

### Option 2: Positions-spezifische Schätzungen ⭐⭐

**Verbessere `_estimate_snap_pct()`:**

```python
def _estimate_snap_pct(depth_rank: Optional[int], position: Optional[str]) -> Optional[float]:
    """Estimate snap percentage based on depth chart and position."""
    if depth_rank is None:
        return None
    
    # Position-specific estimates
    estimates = {
        "QB": {1: 95.0, 2: 5.0, 3: 0.0},      # QBs rarely rotate
        "RB": {1: 55.0, 2: 35.0, 3: 10.0},    # RBs rotate heavily
        "WR": {1: 85.0, 2: 50.0, 3: 20.0},    # Top WRs play most snaps
        "TE": {1: 65.0, 2: 40.0, 3: 15.0},    # TEs vary by role
        "DEFAULT": {1: 70.0, 2: 45.0, 3: 15.0}
    }
    
    pos_estimates = estimates.get(position, estimates["DEFAULT"])
    return pos_estimates.get(depth_rank, 10.0)  # 10% default for depth 4+
```

**Vorteile:**
- ✅ Realistischere Schätzungen
- ✅ Position-aware

**Aufwand:** Klein (15 Min)
**Risiko:** Niedrig

---

### Option 3: Snap Count aus vorherigen Wochen mitteln ⭐

**Idee:** Wenn aktuelle Woche fehlt, nutze 3-Wochen-Durchschnitt:

```python
# In _enrich_usage_and_opponent()
if not enriched_additions.get("snap_pct"):
    # Try to calculate from usage_last_3_weeks
    usage = nfl_db.get_usage_last_n_weeks(player_id, season, week, n=3)
    if usage and usage.get("snap_share_avg"):
        enriched_additions["snap_pct"] = round(usage["snap_share_avg"], 1)
        enriched_additions["snap_pct_source"] = "avg_last_3_weeks"
```

**Vorteile:**
- ✅ Nutzt historische Daten
- ✅ Glättet Ausreißer

**Aufwand:** Klein (10 Min)
**Risiko:** Niedrig

---

### Option 4: Erweiterte Feldsuche ⭐

**Mehr Sleeper API Feldnamen:**

```python
snap_share = (
    player_stats.get("snap_pct") or 
    player_stats.get("off_snp_pct") or 
    player_stats.get("snap_share") or
    player_stats.get("snap_percentage") or
    player_stats.get("snaps_pct") or
    player_stats.get("offensive_snap_pct")
)
```

**Vorteile:**
- ✅ Bessere API-Kompatibilität

**Aufwand:** Minimal (5 Min)
**Risiko:** Niedrig

---

## Empfohlene Implementierung 🎯

### Phase 1: Quick Wins (15 Min) ⚡

**1.1 Erweiterte Feldsuche:**
```python
snap_share = (
    player_stats.get("snap_pct") or 
    player_stats.get("off_snp_pct") or 
    player_stats.get("snap_share") or
    player_stats.get("snap_percentage") or
    player_stats.get("snaps_pct")
)
```

**1.2 Berechnung aus off_snp:**
```python
if not snap_share:
    off_snp = player_stats.get("off_snp")
    team_snp = player_stats.get("team_snp") or player_stats.get("tm_off_snp")
    
    if off_snp is not None and team_snp is not None and team_snp > 0:
        snap_share = round((off_snp / team_snp) * 100, 1)
```

---

### Phase 2: Bessere Schätzungen (15 Min) 🎯

**2.1 Positions-spezifische Defaults:**
```python
def _estimate_snap_pct(depth_rank: Optional[int], position: Optional[str] = None) -> Optional[float]:
    if depth_rank is None:
        return None
    
    # Position-based estimates
    if position == "QB" and depth_rank == 1:
        return 95.0
    elif position == "RB" and depth_rank == 1:
        return 55.0
    elif position == "WR" and depth_rank == 1:
        return 85.0
    elif position == "TE" and depth_rank == 1:
        return 65.0
    
    # Default fallbacks
    if depth_rank == 1:
        return 70.0
    elif depth_rank == 2:
        return 45.0
    else:
        return 15.0
```

---

### Phase 3: Datenquellen-Tracking (10 Min) 📊

**In Database speichern:**
```python
stats.append({
    "player_id": str(pid),
    "season": season,
    "week": week,
    "snap_share": snap_share,
    "snap_share_source": snap_share_source,  # NEW
    # ... other fields
})
```

**Mögliche Werte:**
- `"api"` - Direkt von Sleeper API
- `"calculated_from_snaps"` - Berechnet aus off_snp / team_snp
- `"avg_last_3_weeks"` - Durchschnitt letzte 3 Wochen
- `"estimated_from_depth"` - Geschätzt aus Depth Chart
- `"zero_or_missing"` - Keine Daten verfügbar

---

## Code-Änderungen

### Änderung 1: _fetch_weekly_usage_stats()

**Datei:** `/nfl_mcp/sleeper_tools.py`  
**Zeile:** ~2436

```python
# VORHER:
snap_share = player_stats.get("snap_pct") or player_stats.get("off_snp_pct") or player_stats.get("snap_share")

# NACHHER:
# Try multiple field names first
snap_share = (
    player_stats.get("snap_pct") or 
    player_stats.get("off_snp_pct") or 
    player_stats.get("snap_share") or
    player_stats.get("snap_percentage") or
    player_stats.get("snaps_pct")
)
snap_share_source = "api" if snap_share else None

# Calculate from absolute snaps if not provided
if not snap_share:
    off_snp = player_stats.get("off_snp")
    team_snp = player_stats.get("team_snp") or player_stats.get("tm_off_snp")
    
    if off_snp is not None and team_snp is not None and team_snp > 0:
        snap_share = round((off_snp / team_snp) * 100, 1)
        snap_share_source = "calculated_from_snaps"
    else:
        snap_share_source = "zero_or_missing"
```

---

### Änderung 2: _estimate_snap_pct()

**Datei:** `/nfl_mcp/sleeper_tools.py`  
**Zeile:** ~2488

```python
# VORHER:
def _estimate_snap_pct(depth_rank: Optional[int]) -> Optional[float]:
    if depth_rank is None:
        return None
    if depth_rank == 1:
        return 70.0
    if depth_rank == 2:
        return 45.0
    return 15.0

# NACHHER:
def _estimate_snap_pct(depth_rank: Optional[int], position: Optional[str] = None) -> Optional[float]:
    """Estimate snap percentage based on depth chart and position.
    
    Args:
        depth_rank: Depth chart position (1=starter, 2=backup, etc.)
        position: Player position (QB, RB, WR, TE, etc.)
    
    Returns:
        Estimated snap percentage or None if cannot estimate
    """
    if depth_rank is None:
        return None
    
    # Position-specific estimates for starters
    if depth_rank == 1:
        position_estimates = {
            "QB": 95.0,  # QBs rarely rotate
            "RB": 55.0,  # RBs in committees
            "WR": 85.0,  # #1 WRs play most snaps
            "TE": 65.0,  # TEs vary by blocking role
        }
        return position_estimates.get(position, 70.0)
    
    # Backups (depth 2)
    elif depth_rank == 2:
        position_estimates = {
            "QB": 5.0,   # Backup QBs rarely play
            "RB": 35.0,  # Backup RBs get carries
            "WR": 50.0,  # #2 WRs decent snaps
            "TE": 40.0,  # Backup TEs situational
        }
        return position_estimates.get(position, 45.0)
    
    # Third string or lower
    else:
        return 15.0  # Limited snaps for depth pieces
```

---

## Testing

### Test Case 1: API hat snap_pct ✅
```python
player_stats = {"snap_pct": 85.5}
# Expected: snap_share=85.5, source="api"
```

### Test Case 2: Berechnung aus off_snp ✅
```python
player_stats = {"off_snp": 45, "team_snp": 65}
# Expected: snap_share=69.2, source="calculated_from_snaps"
```

### Test Case 3: Positions-spezifische Schätzung ✅
```python
depth_rank = 1, position = "QB"
# Expected: 95.0 (not 70.0)

depth_rank = 1, position = "RB"
# Expected: 55.0 (not 70.0)
```

### Test Case 4: Keine Daten verfügbar ⚠️
```python
player_stats = {}
# Expected: snap_share=None oder 0, source="zero_or_missing"
# Then fallback to estimated if depth_rank available
```

---

## Datenquellen-Priorität

```
1. API snap_pct ✅ (Direkt von Sleeper)
   ↓ Falls nicht verfügbar
2. Berechne aus off_snp/team_snp ✅ (Selbst berechnet)
   ↓ Falls nicht verfügbar
3. Durchschnitt letzte 3 Wochen ⚠️ (Historisch)
   ↓ Falls nicht verfügbar
4. Schätze aus Depth Chart + Position 📊 (Estimated)
   ↓ Falls nicht verfügbar
5. Default 0 oder None ❌ (Keine Daten)
```

---

## Erwartete Verbesserung

### Vorher:
- ~60% der Spieler haben snap_share Daten
- ~30% werden geschätzt (grobe Schätzung)
- ~10% haben keine Daten

### Nachher:
- ~60% API Daten (unverändert)
- ~25% berechnet aus off_snp/team_snp (**NEU**)
- ~10% positions-spezifisch geschätzt (verbessert)
- ~5% haben keine Daten

**Gesamt:** +20-25% mehr Daten verfügbar! 📈

---

## Database Schema (Optional)

### Aktuell:
```sql
CREATE TABLE player_usage_stats (
    ...
    snap_share REAL,
    ...
);
```

### Erweitert (optional):
```sql
ALTER TABLE player_usage_stats 
ADD COLUMN snap_share_source TEXT;

-- Values: 
-- "api", "calculated_from_snaps", "avg_last_3_weeks", 
-- "estimated_from_depth", "zero_or_missing"
```

---

## Zusammenfassung

### Aktuelle Situation:
✅ Grundlegende Fallback-Mechanismen vorhanden  
⚠️ Keine Berechnung aus off_snp/team_snp  
⚠️ Grobe positions-unabhängige Schätzungen

### Empfohlene Fixes:
1. ✅ **Erweiterte Feldsuche** (5 Min) - Mehr API-Feldnamen
2. ✅ **Berechnung aus Snaps** (15 Min) - off_snp / team_snp
3. ✅ **Positions-spezifisch** (15 Min) - QB vs RB vs WR
4. 📊 **Datenquellen-Tracking** (10 Min) - Transparenz

**Total:** ~45 Minuten Aufwand  
**Ergebnis:** +20-25% mehr Snap Count Daten! 🎯

---

**Datum:** 25. Oktober 2025  
**Status:** Bereit zur Implementierung
