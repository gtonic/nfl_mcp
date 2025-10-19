# NFL MCP Server - API Documentation for LLMs

This document provides comprehensive tool documentation optimized for Large Language Model understanding and decision-making.

> NEU (LLM-Optimiert): Dieser Leitfaden priorisiert reale Nutzerszenarien und gibt dir konkrete Aufruf-Sequenzen. Das wichtigste Ziel ist IMMER: Aktuellen Roster (Kader) eines bestimmten Sleeper Users ermitteln und darauf alle weiteren Analysen aufbauen.

## Neuerungen (September 2025)
1. Zentraler Aggregator: `get_fantasy_context` bündelt `league`, `rosters`, `users`, `matchups`, `transactions` (automatische Week-Inferenz wenn nötig).
2. Automatische Week-Erkennung: `get_transactions` akzeptiert fehlende `week` und setzt `auto_week_inferred=true` bei erfolgreicher Inferenz.
3. Param-Validator Infrastruktur: Neues Modul `param_validator.py` für zukünftige vereinheitlichte Eingabe-Prüfung (schrittweise Migration geplant).
4. Robustheits-Layer (Retry + Snapshot Fallback) jetzt aktiv für: `get_rosters`, `get_transactions`, `get_matchups`.
5. Einheitliche Snapshot-Metadaten Felder: `retries_used`, `stale`, `failure_reason`, `snapshot_fetched_at`, `snapshot_age_seconds`.

### get_fantasy_context – Schnelle Gesamtsicht
Parameter:
- league_id (str, required)
- week (int, optional) – falls weggelassen automatische Erkennung über NFL State
- include (str, optional) – CSV Teilmenge, z.B. `league,rosters,matchups` (Standard: alles)

Antwort Felder:
```
{
  success: true,
  context: { league?, rosters?, users?, matchups?, transactions? },
  league_id: "...",
  week: <int>,
  auto_week_inferred: <bool>
}
```
Heuristik: Wenn du ansonsten drei oder mehr Einzel-Calls planen würdest → Aggregator verwenden.

### Aktualisierte Semantik `get_transactions`
- Week optional; Autodetektion setzt Flag `auto_week_inferred`.
- Fehler nur noch bei fehlgeschlagener Inferenz oder ungültigen Grenzen.


Reihenfolge der Informationsgewinnung (nur so viel wie nötig laden):
1. (Optional, falls Username gegeben) `get_user` → user_id ermitteln
2. `get_user_leagues` → relevante Liga pro Saison auswählen (falls mehrere)
3. `get_league` → Metadaten (Scoring / Settings) NUR falls benötigt
4. `get_rosters` → Kernschritt: Spieler des Users (owner_id == user_id) extrahieren
5. Kontext verfeinern (on demand):
  - Wöchentliche Planung: `get_matchups` (aktueller week) & `get_nfl_state`
  - Aktuelle Marktbewegungen: `get_transactions` (week erforderlich), `get_trending_players`
  - Draft / Historie (nur bei Bedarf): `get_league_drafts`, `get_draft_picks`
  - Playoff-Situation: `get_playoff_bracket` (winners | losers)
6. Spieler-Marktanalyse / Waiver Entscheidungen: Roster-Spieler + Trending vergleichen
7. Tiefergehende NFL-Kontextdaten (ESPN): Verletzungen, Stats, Schedules, Standings nur bei erklärungsbedürftigen Antworten laden

Minimaler Pipeline-Kern für schnelle Antworten (Roster-basiert):
```
get_user (optional) → get_user_leagues → get_rosters → (filter owner roster) → (optional Zusatz)
```

---
## 🧠 Entscheidungsbaum (Kurzform)

| Frage | Tools in empfohlener Reihenfolge | Stoppen sobald… |
|-------|----------------------------------|------------------|
| "Welche Spieler habe ich?" | get_user_leagues → get_rosters | Roster gefunden |
| "Wie sieht mein Matchup diese Woche aus?" | get_nfl_state → get_matchups | Matchups geladen |
| "Was sind Waiver Targets?" | get_trending_players → search_athletes (nur falls Name unklar) | Liste erzeugt |
| "Lohnt sich ein Spieler X?" | lookup_athlete → (optional) get_team_player_stats / get_team_injuries | Kontext ausreichend |
| "Playoff-Chancen / Bracket?" | get_playoff_bracket | Bracket vorliegt |
| "Draft Historie?" | get_league_drafts → get_draft_picks | Relevante Picks vorhanden |

---
## 🏎️ Performance & Kosten (Heuristik für dich als LLM)

| Kategorie | Günstig | Mittel | Teuer |
|-----------|---------|--------|-------|
| Kern Roster | get_user_leagues, get_rosters | — | — |
| Waiver & Aktivität | get_trending_players | get_transactions (pro Woche) | fetch_all_players (groß, nur Metadaten) |
| Draft | get_league_drafts | get_draft_picks | get_draft_traded_picks (selten nötig) |
| NFL Kontext | get_nfl_state | get_team_injuries / get_team_player_stats | fetch_athletes (Initial-Seed) |

Vermeide mehrfaches Laden derselben Woche. Nutze vorhandene Felder `*_enriched` statt selbst erneut anzufragen.

---
## 🔑 Roster-Ermittlung Schritt für Schritt

1. Falls nur Username vorhanden: `get_user` → `user.user_id`
2. `get_user_leagues(user_id, season)` → wähle Liga (Heuristik: aktive Saison, gewünschte Scoring- oder Name-Muster)
3. `get_rosters(league_id)` → finde Objekt mit `owner_id == user_id`
4. Nutze Felder:
  - `players` (Original-IDs)
  - `players_enriched` (bereits angereichert: player_id, full_name, position)
  - `starters_enriched` für Startaufstellung
5. OPTIONAL: `get_matchups(league_id, current_week)` zur Gegneranalyse
6. OPTIONAL: `get_transactions(league_id, week)` → prüfe Adds/Drops des Gegners

Code-orientierte Pseudosequenz:
```python
# 1. user_id beschaffen (falls nötig)
u = await call('get_user', {"username": "myName"})
user_id = u['user']['user_id']

# 2. Ligen der Saison
leagues = await call('get_user_leagues', {"user_id": user_id, "season": 2025})
league_id = leagues['leagues'][0]['league_id']  # einfache Wahlstrategie

# 3. Roster laden
rosters = await call('get_rosters', {"league_id": league_id})
my_roster = next(r for r in rosters['rosters'] if r['owner_id'] == user_id)
players = my_roster['players_enriched']  # bevorzugt statt raw IDs
```

---
## 🧩 Enrichment Felder (Automatisch hinzugefügt)

Viele Sleeper-spezifische Antworten enthalten jetzt zusätzliche *_enriched Felder:

| Endpoint | Neue Felder | Beschreibung |
|----------|-------------|--------------|
| get_rosters | players_enriched, starters_enriched | Spieler mit Name & Position |
| get_matchups | players_enriched, starters_enriched | Für matchup-spezifische Auflistungen |
| get_transactions | adds_enriched, drops_enriched | Bewegte Spieler angereichert |
| get_trending_players | trending_players[i].enriched | Waiver-Relevanz + Basisdaten |
| get_draft_picks | player_enriched | Draft Pick Spielerinfo |
| get_traded_picks / get_draft_traded_picks | player_enriched | Falls player_id vorhanden |

Nutze diese zuerst – sie sparen zusätzliche Lookups.

### Erweiterte Enrichment Felder (Schema v7)
Zusätzliche optionale Felder für angereicherte Spielerobjekte:

| Feld | Beschreibung | Quelle | Werte |
|------|--------------|--------|-------|
| snap_pct | Offensive Snap-Quote der aktuellen Woche (eine Dezimalstelle) | player_week_stats Cache oder Heuristik | 0.0–100.0 |
| snap_pct_source | Herkunft von snap_pct | statisch | cached, estimated |
| opponent | Gegner Team Kürzel für DEF | schedule_games Cache | e.g. KC, BUF |
| opponent_source | Herkunft von opponent | statisch | cached, fetched |

Heuristik: depth_chart_order 1≈70%, 2≈45%, sonst≈15%. Alle Felder sind additiv und fehlen einfach, wenn keine Daten vorliegen.

### Erweiterte Enrichment Felder (Schema v8)
Praxis-Status & Nutzungs-Metriken (aktiviert via `NFL_MCP_ADVANCED_ENRICH=1`):

| Feld | Beschreibung | Werte |
|------|--------------|-------|
| practice_status | Aktueller Verletzungs-/Praxis-Status | DNP, LP, FP, Full |
| practice_status_date | Datum des Praxis-Reports | ISO (YYYY-MM-DD) |
| practice_status_age_hours | Alter des Reports in Stunden | Float (1 Stelle) |
| practice_status_stale | Report älter als 72h | Boolean |
| usage_last_3_weeks | Durchschnittliche Nutzungs-Metriken (WR/RB/TE) | Objekt (siehe unten) |
| usage_source | Herkunft der Nutzungsdaten | sleeper, estimated |
| usage_trend | Trend-Analyse für einzelne Metriken | Objekt (siehe unten) |
| usage_trend_overall | Gesamttrend basierend auf Hauptmetriken | up, down, flat |

**Usage-Objekt Felder:**
- `targets_avg`: Durchschnitt Targets pro Spiel
- `routes_avg`: Durchschnitt gelaufene Routen pro Spiel
- `rz_touches_avg`: Durchschnitt Redzone-Berührungen
- `snap_share_avg`: Durchschnitt Snap-Anteil (%)
- `weeks_sample`: Anzahl Wochen in Stichprobe (1–3)

**Usage-Trend Objekt Felder:**
- `targets`: Trend für Targets (up/down/flat)
- `routes`: Trend für Routes (up/down/flat)
- `snap_share`: Trend für Snap-Anteil (up/down/flat)

**Hinweise:**
- DNP = hohes Risiko (nicht trainiert), LP = moderat, FP/Full = gering
- Usage-Metriken liefern echte Volume-Indikatoren jenseits der Depth-Chart-Position
- Trend-Berechnung: Vergleicht letzte Woche mit Durchschnitt der vorherigen Wochen (Schwellenwert: 15%)
- Trend "up" = steigende Nutzung (↑), "down" = fallende Nutzung (↓), "flat" = stabile Nutzung (→)
- Prefetch-Job lädt Practice Reports Do–Sa, Usage-Stats wöchentlich

---
## 🛑 Abbruch-Kriterien (Früh stoppen!)

Beende weitere Datenerhebung sobald:
- Roster + positions + aktuelle Woche bekannt → Basisanalyse möglich
- Kein Draft-Kontext angefragt → Draft-Endpunkte überspringen
- Keine Playoff-Frage → `get_playoff_bracket` vermeiden
- Keine Waiver-Frage → `get_trending_players` nur bei Bedarf

---
## ❗ Häufige Fehler / Validation Handling

| Problem | Ursache | Lösung |
|---------|---------|-------|
| Missing week in get_transactions | Woche Pflicht (falls Inferenz scheitert) | Übergib `week` oder alias `round` |
| Falscher bracket_type | Tippfehler | Nur `winners` oder `losers` |
| Leere rosters | Privat / falsche Liga | user_id & league_id prüfen, Access-Hinweis beachten |
| Wenig enrichment | Athleten-Datenbank leer | Vorher einmal `fetch_athletes` (teuer) nur falls wirklich nötig |
| success=false aber Daten vorhanden | Snapshot Fallback aktiv | Daten nutzen, aber Hinweis geben dass sie evtl. veraltet sind |

---
## 🧪 Empfehlung für Analyse-Antworten

Wenn du eine textuelle Analyse erzeugst:
1. Liste erst die Starter (starters_enriched)
2. Hebe Positionsknappheit hervor (z.B. TE, QB Tiefe)
3. Prüfe Waiver-Hebel (trending vs. schwächste Bench-Spieler)
4. Optional: Verletzungen via `get_team_injuries` NUR für relevante Teams der Roster-Spieler

---
## 📦 Zusammenfassung der Kern-Pipeline (Merksatz)

"User → Ligen → Roster → (Matchup / Waiver / Playoffs / Draft) nur wenn gefragt."  (Bei Fehlern: Snapshot Felder prüfen.)

---

## Quick Reference - Tool Categories

### 🏈 NFL Information Tools
- **get_nfl_news** - Get latest NFL news and headlines
- **get_teams** - Get all NFL team information  
- **fetch_teams** - Cache NFL teams in database
- **get_depth_chart** - Get team depth chart/roster
- **get_team_injuries** - Get injury reports by team
- **get_team_player_stats** - Get team player statistics
- **get_nfl_standings** - Get current NFL standings
- **get_team_schedule** - Get team schedule with fantasy implications

### 👥 Player/Athlete Tools
- **fetch_athletes** - Import all NFL players into database (expensive operation)
- **lookup_athlete** - Find specific player by ID
- **search_athletes** - Search players by name
- **get_athletes_by_team** - Get all players for a team

### 🔄 Trade Analysis Tools
- **analyze_trade** - Analyze fantasy football trade fairness and fit

### 🌐 Web Scraping Tools
- **crawl_url** - Extract text content from any webpage

### 🏆 Fantasy League Tools (Sleeper API)
- **get_league** - Get league information
- **get_rosters** - Get league rosters
- **get_league_users** - Get league members
- **get_matchups** - Get weekly matchups
- **get_playoff_bracket** - Get winners or losers playoff bracket (bracket_type)
- **get_transactions** - Get league transactions (week required)
- **get_traded_picks** - Get traded draft picks
- **get_league_drafts** - List drafts for league
- **get_draft** - Draft metadata
- **get_draft_picks** - All picks for draft
- **get_draft_traded_picks** - Traded picks within draft
- **fetch_all_players** - Cached full players dataset metadata
- **get_nfl_state** - Get current NFL week/season state
- **get_trending_players** - Get trending waiver wire players with counts & enrichment

---

## Tool Selection Guide for LLMs

### When to Use What Tool

#### For NFL News and Information:
- **Current events/news** → `get_nfl_news`
- **Team information** → `get_teams` (fast) or `fetch_teams` (cache for repeated use)
- **Player roster/positions** → `get_depth_chart`
- **Injury information** → `get_team_injuries`
- **League standings** → `get_nfl_standings`

#### For Player Information:
- **Find specific player** → `lookup_athlete` (if you have ID) or `search_athletes` (by name)
- **Team roster** → `get_athletes_by_team`
- **First time setup** → `fetch_athletes` (WARNING: Large download, use sparingly)

#### For Fantasy Football:
- **League management** → `get_league`, `get_rosters`, `get_league_users`
- **Weekly planning** → `get_matchups`, `get_nfl_state`
- **Waiver wire** → `get_trending_players`
- **Trade analysis** → `analyze_trade` (evaluate trade fairness), `get_transactions`, `get_traded_picks`

#### For Web Content:
- **Extract article text** → `crawl_url`

---

## 🔄 Trade Analyzer Tool - Detailed Documentation

### analyze_trade

**Purpose**: Analyze fantasy football trades for fairness and strategic fit between two teams.

**When to Use**:
- Evaluating a proposed trade before accepting/rejecting
- Assessing if a trade is fair or lopsided
- Understanding how a trade affects positional depth
- Getting objective recommendations on trade value

**Parameters**:
```
league_id (str, required): Unique league identifier
team1_roster_id (int, required): Roster ID for team 1
team2_roster_id (int, required): Roster ID for team 2
team1_gives (list[str], required): Player IDs team 1 is trading away
team2_gives (list[str], required): Player IDs team 2 is trading away
include_trending (bool, optional, default=True): Include trending data in analysis
```

**Returns**:
```json
{
  "success": true,
  "recommendation": "fair" | "slightly_favors_team_1" | "slightly_favors_team_2" | "needs_adjustment" | "unfair",
  "fairness_score": 85.5,
  "team1_analysis": {
    "roster_id": 1,
    "gives": [{"player_id": "4034", "name": "Patrick Mahomes", "position": "QB", "value": 70.5}],
    "receives": [{"player_id": "4036", "name": "Justin Jefferson", "position": "WR", "value": 75.0}],
    "positional_needs": {"QB": 5, "RB": 8, "WR": 3, "TE": 6}
  },
  "team2_analysis": {
    "roster_id": 2,
    "gives": [{"player_id": "4036", "name": "Justin Jefferson", "position": "WR", "value": 75.0}],
    "receives": [{"player_id": "4034", "name": "Patrick Mahomes", "position": "QB", "value": 70.5}],
    "positional_needs": {"QB": 8, "RB": 4, "WR": 6, "TE": 5}
  },
  "trade_details": {
    "team1_gives_value": 70.5,
    "team2_gives_value": 75.0,
    "team1_receives_adjusted_value": 81.0,
    "team2_receives_adjusted_value": 86.5,
    "team1_need_bonus": 6.0,
    "team2_need_bonus": 16.0,
    "value_difference": 5.5
  },
  "warnings": [
    "Player X has DNP status (injury concern)"
  ],
  "league_id": "12345"
}
```

**Fairness Score Interpretation**:
- **90-100**: Perfectly balanced trade, both teams benefit equally
- **75-89**: Fair trade with slight advantage to one team
- **60-74**: Trade needs adjustment, moderate imbalance
- **Below 60**: Significantly unfair trade

**Value Calculation Factors**:
1. **Position Scarcity**: RBs valued higher (1.3x), TEs (1.2x), WRs (1.1x), QBs (1.0x)
2. **Practice Status**: DNP (-30%), LP (-15%), Full Practice (+5%)
3. **Usage Trends**: Upward trend (+15%), Downward trend (-15%)
4. **Snap Percentage**: >80% (+10%), <30% (-20%)
5. **Trending Status**: Each add count worth +2 value (capped at +20)
6. **Positional Needs**: Adds bonus value for positions of high need

**Example Usage**:

Simple 1-for-1 trade:
```python
analyze_trade(
    league_id="123456789",
    team1_roster_id=1,
    team2_roster_id=3,
    team1_gives=["4034"],      # Patrick Mahomes
    team2_gives=["4036"],      # Justin Jefferson
    include_trending=True
)
```

Multi-player trade (2-for-2):
```python
analyze_trade(
    league_id="123456789",
    team1_roster_id=1,
    team2_roster_id=5,
    team1_gives=["4034", "4035"],  # Mahomes + McCaffrey
    team2_gives=["4036", "4037"],  # Jefferson + Kelce
    include_trending=True
)
```

**Common Use Cases**:

1. **Pre-Trade Evaluation**: Check fairness before proposing
2. **Trade Review**: Validate if counter-offer is reasonable
3. **League Commissioner**: Verify trades aren't collusion
4. **Strategic Planning**: Understand positional impact of trades

**Best Practices**:
- Always run analysis before accepting trades
- Consider warnings (injuries, depth issues) seriously
- Compare fairness_score across multiple trade scenarios
- Check positional_needs to ensure trade improves roster balance
- Use with `include_trending=True` for most accurate current values

**Limitations**:
- Values are estimates based on available data, not guarantees
- Does not account for schedule strength or playoff matchups
- Player values may not reflect league-specific scoring settings
- Historical performance may not predict future outcomes

**Related Tools**:
- `get_rosters` - View current team rosters
- `get_trending_players` - See hot waiver pickups
- `get_transactions` - Review past trades in league
- `get_traded_picks` - Check draft pick trades
