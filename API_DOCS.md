# NFL MCP Server - API Documentation for LLMs

This document provides comprehensive tool documentation optimized for Large Language Model understanding and decision-making.

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
- **get_playoff_bracket** - Get playoff bracket
- **get_transactions** - Get league transactions
- **get_traded_picks** - Get traded draft picks
- **get_nfl_state** - Get current NFL week/season state
- **get_trending_players** - Get trending waiver wire players

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

#### get_trending_players
**Purpose:** Get players trending on waiver wire (adds/drops)

**Parameters:**
- `trend_type` (str, optional): "add" or "drop" (default: "add")
- `lookback_hours` (int, optional): Hours to look back (1-168, default: 24)
- `limit` (int, optional): Max players (1-50, default: 25)

**Returns:**
```json
{
  "trending_players": [
    {
      "player_id": "String - Player ID",
      "full_name": "String - Player name",
      "team": "String - Team abbreviation", 
      "position": "String - Position",
      "count": "Number - Number of adds/drops"
    }
  ],
  "trend_type": "String - Type of trend requested",
  "lookback_hours": "Number - Hours analyzed",
  "count": "Number - Number of players returned",
  "success": "Boolean - Success status",
  "error": "String|null - Error message if failed"
}
```

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