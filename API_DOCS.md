# NFL MCP Server - API Documentation for LLMs

This document provides comprehensive tool documentation optimized for Large Language Model understanding and decision-making.

> NEU (LLM-Optimiert): Dieser Leitfaden priorisiert reale Nutzerszenarien und gibt dir konkrete Aufruf-Sequenzen. Das wichtigste Ziel ist IMMER: Aktuellen Roster (Kader) eines bestimmten Sleeper Users ermitteln und darauf alle weiteren Analysen aufbauen.

---
## 🚦 Strategische Schnellübersicht (Prioritäten für das LLM)

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
| Missing week in get_transactions | Woche Pflicht | Übergib `week` oder alias `round` |
| Falscher bracket_type | Tippfehler | Nur `winners` oder `losers` |
| Leere rosters | Privat / falsche Liga | user_id & league_id prüfen, Access-Hinweis beachten |
| Wenig enrichment | Athleten-Datenbank leer | Vorher einmal `fetch_athletes` (teuer) nur falls wirklich nötig |

---
## 🧪 Empfehlung für Analyse-Antworten

Wenn du eine textuelle Analyse erzeugst:
1. Liste erst die Starter (starters_enriched)
2. Hebe Positionsknappheit hervor (z.B. TE, QB Tiefe)
3. Prüfe Waiver-Hebel (trending vs. schwächste Bench-Spieler)
4. Optional: Verletzungen via `get_team_injuries` NUR für relevante Teams der Roster-Spieler

---
## 📦 Zusammenfassung der Kern-Pipeline (Merksatz)

"User → Ligen → Roster → (Matchup / Waiver / Playoffs / Draft) nur wenn gefragt."

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
- **Trade analysis** → `get_transactions`, `get_traded_picks`

#### For Web Content:
- **Extract article text** → `crawl_url`

---

## Detailed Tool Documentation

### 🏈 NFL Information Tools

#### get_nfl_news
**Purpose:** Get the latest NFL news and headlines from ESPN

**Parameters:**
- `limit` (int, optional): Max articles (1-50, default: 50)

**Returns:**
```json
{
  "articles": [
    {
      "headline": "String - Article title",
      "description": "String - Article summary", 
      "published": "String - Publication date",
      "type": "String - Article type",
      "story": "String - Full content",
      "categories": ["String - Category tags"],
      "links": {"web": "URL", "mobile": "URL"}
    }
  ],
  "total_articles": "Number - Count of articles",
  "success": "Boolean - Success status",
  "error": "String|null - Error message if failed"
}
```

**Use Cases:**
- Getting current NFL news for analysis
- Finding recent developments about teams/players
- Staying updated on league developments

**Example:**
```python
# Get latest 10 NFL news articles
result = await client.call_tool("get_nfl_news", {"limit": 10})
```

---

#### get_teams
**Purpose:** Get all NFL team information from ESPN API

**Parameters:** None

**Returns:**
```json
{
  "teams": [
    {
      "id": "String - Team ID",
      "name": "String - Team name",
      "abbreviation": "String - Team abbreviation",
      "displayName": "String - Display name",
      "location": "String - City/location",
      "color": "String - Primary color",
      "alternateColor": "String - Secondary color"
    }
  ],
  "total_teams": "Number - Count of teams (usually 32)",
  "success": "Boolean - Success status", 
  "error": "String|null - Error message if failed"
}
```

**Use Cases:**
- Getting team IDs for other API calls
- Reference information about NFL teams
- Building team-based interfaces

**Example:**
```python
# Get all NFL teams
teams = await client.call_tool("get_teams", {})
```

---

#### get_depth_chart
**Purpose:** Get team depth chart showing player positions and depth ordering

**Parameters:**
- `team_id` (str, required): Team abbreviation (e.g., 'KC', 'TB', 'NE')

**Returns:**
```json
{
  "team_id": "String - Team abbreviation used",
  "team_name": "String - Full team name",
  "depth_chart": [
    {
      "position": "String - Position name (QB, RB, WR, etc.)",
      "players": ["String - Player names in depth order"]
    }
  ],
  "success": "Boolean - Success status",
  "error": "String|null - Error message if failed"
}
```

**Use Cases:**
- Understanding team roster structure
- Identifying starters vs backups
- Fantasy football depth analysis

**Validation:**
- `team_id` must be valid NFL team abbreviation (2-4 characters)
- Common team IDs: KC, TB, NE, DAL, GB, etc.

**Example:**
```python
# Get Kansas City Chiefs depth chart
depth = await client.call_tool("get_depth_chart", {"team_id": "KC"})
```

---

### 👥 Player/Athlete Tools

#### lookup_athlete
**Purpose:** Find a specific player by their unique Sleeper ID

**Parameters:**
- `athlete_id` (str, required): Sleeper player ID

**Returns:**
```json
{
  "athlete": {
    "player_id": "String - Unique player ID",
    "first_name": "String - First name",
    "last_name": "String - Last name", 
    "full_name": "String - Full name",
    "team": "String - Current team abbreviation",
    "position": "String - Position (QB, RB, WR, etc.)",
    "age": "Number - Player age",
    "years_exp": "Number - Years of experience"
  },
  "found": "Boolean - Whether player was found",
  "error": "String|null - Error message if failed"
}
```

**Use Cases:**
- Getting detailed player information
- Verifying player identity
- Fantasy roster analysis

**Prerequisites:**
- Database must be initialized with `fetch_athletes` first

**Example:**
```python
# Look up specific player
player = await client.call_tool("lookup_athlete", {"athlete_id": "4046"})
```

---

#### search_athletes
**Purpose:** Search for players by name (partial matching supported)

**Parameters:**
- `name` (str, required): Player name or partial name to search for
- `limit` (int, optional): Max results (1-100, default: 10)

**Returns:**
```json
{
  "athletes": [
    {
      "player_id": "String - Unique player ID",
      "first_name": "String - First name",
      "last_name": "String - Last name",
      "full_name": "String - Full name", 
      "team": "String - Current team abbreviation",
      "position": "String - Position"
    }
  ],
  "count": "Number - Number of results found",
  "search_term": "String - Original search term",
  "error": "String|null - Error message if failed"
}
```

**Use Cases:**
- Finding players when you don't know exact ID
- Searching for players by partial name
- Building player selection interfaces

**Search Tips:**
- Case-insensitive matching
- Supports partial names (e.g., "Mahomes" finds "Patrick Mahomes")
- Searches both first and last names

**Example:**
```python
# Search for players named Mahomes
players = await client.call_tool("search_athletes", {"name": "Mahomes", "limit": 5})
```

---

### 🌐 Web Scraping Tools

#### crawl_url
**Purpose:** Extract clean, LLM-friendly text content from any webpage

**Parameters:**
- `url` (str, required): URL to crawl (HTTP/HTTPS only)
- `max_length` (int, optional): Max content length (100-50000, default: 10000)

**Returns:**
```json
{
  "url": "String - URL that was crawled",
  "title": "String - Page title",
  "content": "String - Extracted text content",
  "content_length": "Number - Length of extracted content",
  "success": "Boolean - Success status",
  "error": "String|null - Error message if failed"
}
```

**Security Features:**
- URL validation and safety checks
- Blocks private networks and dangerous patterns
- Content sanitization removes scripts and malicious code
- Request timeouts prevent hanging

**Use Cases:**
- Extracting article content for analysis
- Getting text from news pages
- Scraping public information

**Content Processing:**
- Removes scripts, styles, navigation, footers
- Normalizes whitespace
- Truncates to specified length
- HTML entities are decoded

**Example:**
```python
# Extract content from an article
content = await client.call_tool("crawl_url", {
    "url": "https://example.com/article",
    "max_length": 5000
})
```

---

### 🏆 Fantasy League Tools (Sleeper API)

#### get_league
**Purpose:** Get comprehensive league information from Sleeper

**Parameters:**
- `league_id` (str, required): Sleeper league ID

**Returns:**
```json
{
  "league": {
    "league_id": "String - League ID",
    "name": "String - League name",
    "total_rosters": "Number - Number of teams",
    "status": "String - League status",
    "sport": "String - Sport type",
    "season": "String - Season year",
    "season_type": "String - Season type",
    "settings": {
      "playoff_teams": "Number - Teams making playoffs",
      "playoff_weeks": "Array - Playoff week numbers"
    }
  },
  "success": "Boolean - Success status",
  "error": "String|null - Error message if failed"
}
```

**Use Cases:**
- Getting league configuration
- Understanding league settings
- Validating league access

**Example:**
```python
# Get league information
league = await client.call_tool("get_league", {"league_id": "123456789"})
```

---

#### get_rosters
**Purpose:** Get all team rosters in a Sleeper fantasy league

**Parameters:**
- `league_id` (str, required): Sleeper league ID

**Returns:**
```json
{
  "rosters": [
    {
      "roster_id": "Number - Roster/team ID",
      "owner_id": "String - User ID of team owner",
      "players": ["Array of player IDs on roster"],
      "starters": ["Array of player IDs in starting lineup"],
      "settings": {
        "wins": "Number - Season wins",
        "losses": "Number - Season losses",
        "fpts": "Number - Fantasy points scored"
      }
    }
  ],
  "count": "Number - Number of rosters returned",
  "success": "Boolean - Success status",
  "error": "String|null - Error message if failed",
  "error_type": "String|null - Type of error",
  "access_help": "String|null - Guidance for resolving access issues",
  "warning": "String|null - Warning about potential access restrictions"
}
```

**Access Requirements:**
- Public leagues: No restrictions
- Private leagues: May require roster access permissions
- League owners can enable/disable public roster access in settings

**Common Error Types:**
- `access_denied_error`: Roster information is private
- `http_error`: League not found or rate limited
- `network_error`: Connection issues

**Troubleshooting Roster Access Issues:**
1. **"Access denied" (403)**: League owner needs to enable public roster access
2. **"Authentication required" (401)**: Private league requiring login
3. **"League not found" (404)**: Verify league ID is correct
4. **Empty rosters but league exists**: Privacy settings may hide roster details

**Use Cases:**
- Analyzing team rosters and lineups
- Identifying available players
- League management and oversight
- Roster optimization analysis

**Example:**
```python
# Get league rosters
rosters = await client.call_tool("get_rosters", {"league_id": "123456789"})

# Check for access issues
if not rosters["success"]:
    if rosters["error_type"] == "access_denied_error":
        print(f"Access denied: {rosters['access_help']}")
    else:
        print(f"Error: {rosters['error']}")
```

---

#### get_trending_players
**Purpose:** Get players trending on waiver wire (adds/drops) with Sleeper counts and optional enrichment

**Parameters:**
- `trend_type` (str, optional): "add" or "drop" (default: "add")
- `lookback_hours` (int, optional): Hours to look back (1-168, default: 24)
- `limit` (int, optional): Max players (1-100, default: 25)

**Returns:**
```json
{
  "trending_players": [
    {
      "player_id": "String",
      "count": "Number - Sleeper adds/drops in window",
      "enriched": {
        "player_id": "String",
        "full_name": "String|null",
        "team": "String|null",
        "position": "String|null"
      }
    }
  ],
  "trend_type": "add|drop",
  "lookback_hours": 24,
  "count": 25,
  "success": true,
  "error": null
}
```
#### get_playoff_bracket
**Purpose:** Fetch either winners or losers playoff bracket.

**Parameters:**
- `league_id` (str, required)
- `bracket_type` (str, optional): "winners" (default) or "losers"

**Returns:**
```json
{
  "playoff_bracket": [ { "r": 1, "m": 3, "t1": 5, "t2": 8 } ],
  "bracket_type": "winners",
  "success": true,
  "error": null
}
```

**Notes:** Previously only winners bracket was retrievable; losers bracket now supported. Validation enforces accepted values.

#### get_transactions
**Change:** Week (or alias `round`) is now required. The undocumented all-weeks call was removed for correctness with official API.

**Parameters:**
- `league_id` (str, required)
- `week` (int, required, 1-18 typical) OR `round` (deprecated alias). If both provided must match.

**Validation Errors:**
- Missing week → "A week (round) parameter is required..."
- Mismatch week vs round → conflict validation error

#### Draft & Players Dataset Endpoints

| Tool | Purpose | Notes |
|------|---------|-------|
| `get_league_drafts` | List drafts attached to a league | Useful for multi-year leagues |
| `get_draft` | Draft metadata | Basic settings & status |
| `get_draft_picks` | All picks | Potentially large list |
| `get_draft_traded_picks` | Traded picks in draft | Combine with league traded picks |
| `fetch_all_players` | Metadata for full Sleeper players map | Uses in-memory 12h TTL cache; returns counts only to avoid huge payload |

**fetch_all_players Return Example:**
```json
{
  "player_count": 12187,
  "cached": true,
  "cached_age_seconds": 42,
  "players": {},
  "success": true
}
```

Call with `force_refresh=true` to bypass cache; large network request (multi‑MB) so avoid frequent refreshes.

**Use Cases:**
- Finding waiver wire targets
- Identifying players to drop
- Market trend analysis

**Example:**
```python
# Get top waiver wire adds from last 48 hours
trending = await client.call_tool("get_trending_players", {
    "trend_type": "add",
    "lookback_hours": 48,
    "limit": 15
})
```

---

## Error Handling Patterns

All tools follow consistent error handling:

1. **Success Response**: `"success": true, "error": null`
2. **Validation Error**: `"success": false, "error": "Invalid parameter: ..."`
3. **Network Error**: `"success": false, "error": "Request timed out"`
4. **API Error**: `"success": false, "error": "HTTP 404: Not Found"`

**Always check the `success` field before processing data.**

---

## Performance Considerations

### Expensive Operations (Use Sparingly):
- `fetch_athletes` - Downloads 10MB+ dataset
- `crawl_url` - Network request + HTML parsing
- Any tool with external API calls

### Fast Operations (Local Database):
- `lookup_athlete`, `search_athletes`, `get_athletes_by_team` (after `fetch_athletes`)
- Local validation functions

### Rate Limiting:
- Built-in rate limiting prevents abuse
- Sleeper API has its own rate limits
- ESPN endpoints may be rate limited

---

## Common Usage Patterns

### Fantasy Football Analysis:
1. `get_nfl_state` - Check current week
2. `get_trending_players` - Find waiver targets
3. `search_athletes` - Research specific players
4. `get_team_injuries` - Check injury status

### Team Research:
1. `get_teams` - Get team list
2. `get_depth_chart` - Analyze roster
3. `get_team_player_stats` - Performance data
4. `get_nfl_news` - Recent developments

### League Management:
1. `get_league` - Verify league access
2. `get_rosters` - Current rosters
3. `get_matchups` - Weekly analysis
4. `get_transactions` - Recent activity