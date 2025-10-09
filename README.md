# NFL MCP Server

A FastMCP server that provides health monitoring, web content extraction, NFL news fetching, NFL teams information, and comprehensive fantasy league management through both REST and MCP protocols.

## Features

- **Health Endpoint**: Non-MCP REST endpoint at `/health` for monitoring server status
- **URL Crawling Tool**: MCP tool that crawls arbitrary URLs and extracts LLM-friendly text content
- **NFL News Tool**: MCP tool that fetches the latest NFL news from ESPN API
- **NFL Teams Tools**: Comprehensive MCP tools for NFL teams including:
  - Team data fetching and database caching from ESPN API
  - Depth chart retrieval for individual teams
- **Fantasy Intelligence APIs**: Advanced MCP tools for fantasy football decision making:
  - **Injury Reports**: Real-time injury status for start/sit decisions
  - **Player Performance Stats**: Team player statistics and fantasy relevance indicators  
  - **NFL Standings**: League standings with playoff implications and team motivation context
  - **Team Schedules**: Matchup analysis with fantasy implications and strength of schedule
- **Athlete Tools**: MCP tools for fetching, caching, and looking up NFL athletes from Sleeper API with SQLite persistence
- **Sleeper API Tools**: Comprehensive MCP tools for fantasy league management including:
  - League information, rosters, users, matchups, playoff brackets
  - Transactions, traded picks, NFL state, trending players
- **Flexible Configuration**: Environment variables and configuration files (YAML/JSON) with hot-reloading
- **HTTP Transport**: Runs on HTTP transport protocol (default port 9000)
- **Containerized**: Docker support for easy deployment
- **Well Tested**: Comprehensive unit tests for all functionality

## Quick Start

### Prerequisites

- Python 3.9 or higher
- Docker (optional, for containerized deployment)
- Task (optional, for using Taskfile commands)

### Installation

1. Clone the repository:
```bash
git clone https://github.com/gtonic/nfl_mcp.git
cd nfl_mcp
```

2. Install dependencies:
```bash
pip install -r requirements.txt
pip install -e ".[dev]"
```

### Running the Server

#### Local Development
```bash
python -m nfl_mcp.server
```

#### Using Docker
```bash
# Build and run
docker build -t nfl-mcp-server .
docker run --rm -p 9000:9000 nfl-mcp-server
```

#### Using Taskfile
```bash
# Install task: https://taskfile.dev/installation/
task run          # Run locally
task run-docker   # Run in Docker
task all          # Complete pipeline
```

## Configuration

The NFL MCP Server supports flexible configuration through environment variables and configuration files. See [CONFIGURATION.md](CONFIGURATION.md) for detailed documentation.

### Quick Configuration Examples

#### Environment Variables
```bash
# Set custom timeouts and limits
export NFL_MCP_TIMEOUT_TOTAL=45.0
export NFL_MCP_NFL_NEWS_MAX=75
export NFL_MCP_SERVER_VERSION="1.0.0"

# Advanced enrichment and prefetch (optional)
export NFL_MCP_ADVANCED_ENRICH=1        # Enable snap%, opponent, practice status, usage metrics
export NFL_MCP_PREFETCH=1               # Enable background data prefetch
export NFL_MCP_PREFETCH_INTERVAL=900    # Prefetch interval in seconds (default: 900 = 15 min)
export NFL_MCP_PREFETCH_SNAPS_TTL=1800  # Snap data TTL in seconds (default: 1800 = 30 min)

# Logging configuration (optional)
export NFL_MCP_LOG_LEVEL=INFO           # Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)

# Run the server
python -m nfl_mcp.server
```

**Note:** Logging is enabled at INFO level by default, providing comprehensive tracking of prefetch operations, enrichment activity, and API calls. See [LOGGING_GUIDE.md](LOGGING_GUIDE.md) for detailed logging documentation.

#### Configuration File (config.yml)
```yaml
timeout:
  total: 45.0
  connect: 15.0

limits:
  nfl_news_max: 75
  athletes_search_max: 150

rate_limits:
  default_requests_per_minute: 120

security:
  max_string_length: 2000
```

#### Docker with Environment Variables
```bash
docker run --rm -p 9000:9000 \
  -e NFL_MCP_TIMEOUT_TOTAL=45.0 \
  -e NFL_MCP_RATE_LIMIT_DEFAULT=120 \
  -e NFL_MCP_ADVANCED_ENRICH=1 \
  -e NFL_MCP_PREFETCH=1 \
  -e NFL_MCP_LOG_LEVEL=INFO \
  nfl-mcp-server
```

## API Documentation

📋 **[Complete API Documentation](./API_DOCS.md)** - Comprehensive tool reference optimized for LLM understanding

### Quick Overview

The NFL MCP Server provides **26+ MCP tools** organized into these categories:

#### 🏈 NFL Information (9 tools)
- `get_nfl_news` - Latest NFL news from ESPN
- `get_teams` - All NFL team information  
- `fetch_teams` - Cache teams in database
- `get_depth_chart` - Team roster/depth chart
- `get_team_injuries` - Injury reports by team
- `get_team_player_stats` - Team player statistics
- `get_nfl_standings` - Current NFL standings
- `get_team_schedule` - Team schedules with fantasy context
- `get_league_leaders` - NFL statistical leaders by category

#### 👥 Player/Athlete (4 tools)
- `fetch_athletes` - Import all NFL players (expensive, use sparingly)
- `lookup_athlete` - Find player by ID
- `search_athletes` - Search players by name
- `get_athletes_by_team` - Get team roster

#### 🌐 Web Scraping (1 tool)
- `crawl_url` - Extract text from any webpage

#### 🏆 Fantasy League - Sleeper API (Expanded)
- Core League: `get_league`, `get_rosters`, `get_league_users`
- Match / Brackets: `get_matchups`, `get_playoff_bracket` (now supports winners|losers via bracket_type)
- Activity & Assets: `get_transactions` (week required), `get_traded_picks`
- Draft Data: `get_league_drafts`, `get_draft`, `get_draft_picks`, `get_draft_traded_picks`
- Global / Meta: `get_nfl_state`, `get_trending_players`, `fetch_all_players` (large players map w/ caching)

#### ❤️ Health Endpoint (REST)
- **GET** `/health` - Server status monitoring

### Tool Selection Guide

**For LLMs:** The [detailed API documentation](./API_DOCS.md) includes:
- 🎯 **When to use each tool** - Decision matrix for tool selection
- 📊 **Parameter validation** - Input constraints and validation rules
- 💡 **Usage patterns** - Common workflows and examples  
- ⚡ **Performance notes** - Which tools are expensive vs. fast
- 🛡️ **Error handling** - Consistent error response patterns

### Basic Usage Example

```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    # Get latest NFL news
    news = await client.call_tool("get_nfl_news", {"limit": 5})
    
    # Search for a player
    player = await client.call_tool("search_athletes", {"name": "Mahomes"})
    
    # Get team depth chart
    depth = await client.call_tool("get_depth_chart", {"team_id": "KC"})
```

### Sleeper Enhancements (Recent)

Recent upgrades to Sleeper tooling:
- Added losers bracket support: `get_playoff_bracket(league_id, bracket_type="losers")`
- Enforced explicit week for `get_transactions` (or alias round) to match official API
- Trending players now preserves Sleeper-provided `count` and enriches with local athlete data under `enriched`
- Added draft suite (`get_league_drafts`, `get_draft`, `get_draft_picks`, `get_draft_traded_picks`)
- Added full players dataset endpoint `fetch_all_players` with 12h in-memory TTL (returns metadata, not massive map)
- Added enrichment across core endpoints (rosters, matchups, transactions, traded picks, draft picks, trending)
- Automatic week inference for `get_transactions` (adds `auto_week_inferred`)
- Aggregator endpoint `get_fantasy_context` to batch league core data (optional `include`)
- Introduced central param validator utility (`param_validator.py`) for future consolidation
 - Robustness layer (retry + snapshot fallback) for: `get_rosters`, `get_transactions`, `get_matchups`
 - Snapshot metadata fields now returned:
   - `retries_used`: number of retry attempts consumed
   - `stale`: indicates if served data came from a snapshot beyond freshness TTL
   - `failure_reason`: last encountered failure code/category
   - `snapshot_fetched_at`, `snapshot_age_seconds`: present when snapshot used (or null on fresh)

#### Additional Enrichment (Schema v7)
New optional fields may appear within `players_enriched`, `starters_enriched`, and transaction add/drop enrichment objects:

| Field | Description | Source Values |
|-------|-------------|---------------|
| `snap_pct` | Offensive snap percentage for the current week (float, one decimal) | Derived or cached |
| `snap_pct_source` | Provenance for `snap_pct` | `cached`, `estimated` |
| `opponent` | Opponent team abbreviation (for DEF entries) | Schedule cache |
| `opponent_source` | Provenance for `opponent` | `cached`, `fetched` |

Notes:
- Estimated snap% uses depth-chart heuristics (starter≈70, #2≈45, others≈15) when real stats absent.
- All fields are additive and may be absent without breaking existing consumers.

#### Enhanced Enrichment (Schema v8)
Additional practice status & usage metrics (requires `NFL_MCP_ADVANCED_ENRICH=1`):

| Field | Description | Values/Format |
|-------|-------------|---------------|
| `practice_status` | Latest injury practice designation | DNP, LP, FP, Full |
| `practice_status_date` | Date of practice report | ISO date (YYYY-MM-DD) |
| `practice_status_age_hours` | Age of practice report in hours | Float (1 decimal) |
| `practice_status_stale` | Report older than 72h | Boolean |
| `usage_last_3_weeks` | Avg usage metrics (WR/RB/TE only) | Object (see below) |
| `usage_source` | Provenance for usage data | `sleeper`, `estimated` |
| `usage_trend` | Trend analysis per metric (WR/RB/TE) | Object (see below) |
| `usage_trend_overall` | Overall usage trend direction | `up`, `down`, `flat` |

**Usage Object Fields:**
- `targets_avg`: Average targets per game (1 decimal)
- `routes_avg`: Average routes run per game (1 decimal)
- `rz_touches_avg`: Average red zone touches per game (1 decimal)
- `snap_share_avg`: Average snap share percentage (1 decimal)
- `weeks_sample`: Number of weeks in sample (1-3)

**Usage Trend Object Fields:**
- `targets`: Trend for targets (`up`/`down`/`flat`)
- `routes`: Trend for routes run (`up`/`down`/`flat`)
- `snap_share`: Trend for snap percentage (`up`/`down`/`flat`)

**Notes:**
- Practice status helps identify injury risk (DNP = high risk, LP = moderate, FP/Full = low)
- Usage metrics provide true volume indicators beyond depth chart position
- Trend calculation compares most recent week vs prior weeks (15% threshold)
- Trend "up" (↑) = rising usage, "down" (↓) = declining usage, "flat" (→) = stable usage
- All fields are additive; absent fields mean data unavailable
- See [USAGE_TREND_ANALYSIS.md](USAGE_TREND_ANALYSIS.md) for detailed trend documentation


#### Robustness & Snapshot Behavior
Each robust endpoint attempts multiple fetches with backoff. If all fail, the server returns the most recent cached snapshot **with `success=false`** but still provides usable data so LLM workflows can continue gracefully. Always check:

```jsonc
{
  "success": false,
  "stale": true,
  "retries_used": 3,
  "failure_reason": "timeout",
  "snapshot_fetched_at": "2025-09-13T11:22:33Z",
  "snapshot_age_seconds": 642
}
```

For fresh successful responses the snapshot fields are present with `null` values (allowing uniform downstream parsing).

#### Aggregator Quick Use
```
get_fantasy_context(league_id="12345", include="league,rosters,matchups")
```
If `week` omitted it will be inferred from NFL state. Response includes `week` and `auto_week_inferred`.

#### Updated Transactions Behavior
Calling `get_transactions(league_id)` without `week` now attempts inference; falls back to validation error only if NFL state unavailable.

### Architecture Improvements

**Simplified Design** (v2.0):
- ✅ **Single Tool Registry** - All tools defined in one place
- ✅ **92% Code Reduction** - Server simplified from 766 to 59 lines
- ✅ **Zero Duplication** - Eliminated redundant tool definitions
- ✅ **Clean Dependencies** - Straightforward import structure

### Health Endpoint (REST)

**GET** `/health`

Returns server health status.

**Response:**
```json
{
  "status": "healthy",
  "service": "NFL MCP Server", 
  "version": "0.1.0"
}
```

### NFL News Tool (MCP)

**Tool Name:** `get_nfl_news`

Fetches the latest NFL news from ESPN API and returns structured news data.

**Parameters:**
- `limit` (integer, optional): Maximum number of news articles to retrieve (default: 50, max: 50)

**Returns:** Dictionary with the following fields:
- `articles`: List of news articles with headlines, descriptions, published dates, etc.
- `total_articles`: Number of articles returned
- `success`: Whether the request was successful
- `error`: Error message (if any)

**Example Usage with MCP Client:**
```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    result = await client.call_tool("get_nfl_news", {"limit": 10})
    
    if result.data["success"]:
        print(f"Found {result.data['total_articles']} articles")
        for article in result.data["articles"]:
            print(f"- {article['headline']}")
            print(f"  Published: {article['published']}")
    else:
        print(f"Error: {result.data['error']}")
```

**Article Structure:**
Each article in the `articles` list contains:
- `headline`: Article headline
- `description`: Brief description/summary
- `published`: Publication date/time
- `type`: Article type (Story, News, etc.)
- `story`: Full story content
- `categories`: List of category descriptions
- `links`: Associated links (web, mobile, etc.)

### NFL Teams Tools (MCP)

**Tools:** `get_teams`, `fetch_teams`, `get_depth_chart`

These tools provide comprehensive NFL teams data management with database caching and depth chart access.

#### get_teams

Fetches all NFL teams from ESPN API and returns structured team data.

**Parameters:** None

**Returns:** Dictionary with the following fields:
- `teams`: List of teams with comprehensive team information
- `total_teams`: Number of teams returned
- `success`: Whether the request was successful
- `error`: Error message (if any)

#### fetch_teams

Fetches all NFL teams from ESPN API and stores them in the local database for caching.

**Parameters:** None

**Returns:** Dictionary with the following fields:
- `teams_count`: Number of teams processed and stored
- `last_updated`: Timestamp of the update
- `success`: Whether the fetch was successful
- `error`: Error message (if any)

#### get_depth_chart

Fetches the depth chart for a specific NFL team from ESPN.

**Parameters:**
- `team_id`: Team abbreviation (e.g., 'KC', 'TB', 'NE')

**Returns:** Dictionary with the following fields:
- `team_id`: The team identifier used
- `team_name`: The team's full name
- `depth_chart`: List of positions with players in depth order
- `success`: Whether the request was successful
- `error`: Error message (if any)

**Example Usage with MCP Client:**
```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    # Get all teams
    result = await client.call_tool("get_teams", {})
    for team in result.data["teams"]:
        print(f"- {team['displayName']} ({team['abbreviation']})")
    
    # Fetch and cache teams data
    result = await client.call_tool("fetch_teams", {})
    print(f"Cached {result.data['teams_count']} teams")
    
    # Get depth chart for Kansas City Chiefs
    result = await client.call_tool("get_depth_chart", {"team_id": "KC"})
    print(f"Depth chart for {result.data['team_name']}:")
    for position in result.data['depth_chart']:
        print(f"  {position['position']}: {', '.join(position['players'])}")
```

**Team Structure:**
Each team in the `teams` list contains:
- `id`: Unique team identifier
- `name`: Team name

### Fantasy Intelligence APIs (MCP)

**Tools:** `get_team_injuries`, `get_team_player_stats`, `get_nfl_standings`, `get_team_schedule`

These advanced tools provide critical fantasy football intelligence for making informed decisions about lineups, waiver wire pickups, and long-term strategy.

#### get_team_injuries

Fetches real-time injury reports for a specific NFL team from ESPN's Core API.

**Parameters:**
- `team_id`: Team abbreviation (e.g., 'KC', 'TB', 'NE')
- `limit`: Maximum number of injuries to return (1-100, defaults to 50)

**Returns:** Dictionary with the following fields:
- `team_id`: The team identifier used
- `team_name`: The team's full name
- `injuries`: List of injured players with status and fantasy severity
- `count`: Number of injuries returned
- `success`: Whether the request was successful
- `error`: Error message (if any)

**Injury Structure:**
Each injury contains:
- `player_name`: Player's full name
- `position`: Player's position (QB, RB, WR, etc.)
- `status`: Injury status (Out, Questionable, Doubtful, etc.)
- `severity`: Fantasy impact level (High, Medium, Low)
- `description`: Injury description
- `type`: Type of injury

#### get_team_player_stats

Fetches player statistics and fantasy relevance for a specific NFL team.

**Parameters:**
- `team_id`: Team abbreviation (e.g., 'KC', 'TB', 'NE')
- `season`: Season year (defaults to 2025)
- `season_type`: 1=Pre, 2=Regular, 3=Post, 4=Off (defaults to 2)
- `limit`: Maximum number of players to return (1-100, defaults to 50)

**Returns:** Dictionary with the following fields:
- `team_id`: The team identifier used
- `team_name`: The team's full name
- `season`: Season year requested
- `season_type`: Season type requested
- `player_stats`: List of players with performance data
- `count`: Number of players returned
- `success`: Whether the request was successful
- `error`: Error message (if any)

**Player Stats Structure:**
Each player contains:
- `player_name`: Player's full name
- `position`: Player's position
- `fantasy_relevant`: Boolean indicating fantasy football relevance
- `jersey`: Jersey number
- `age`: Player's age
- `experience`: Years of NFL experience

#### get_nfl_standings

Fetches current NFL standings with fantasy context about team motivation and playoff implications.

**Parameters:**
- `season`: Season year (defaults to 2025)
- `season_type`: 1=Pre, 2=Regular, 3=Post, 4=Off (defaults to 2)
- `group`: Conference group (1=AFC, 2=NFC, None=both, defaults to None)

**Returns:** Dictionary with the following fields:
- `standings`: List of teams with records and fantasy context
- `season`: Season year requested
- `season_type`: Season type requested
- `group`: Conference group requested
- `count`: Number of teams returned
- `success`: Whether the request was successful
- `error`: Error message (if any)

**Standings Structure:**
Each team contains:
- `team_name`: Team's full name
- `abbreviation`: Team abbreviation
- `wins`: Number of wins
- `losses`: Number of losses
- `motivation_level`: Team motivation for fantasy purposes (High/Medium/Low)
- `fantasy_context`: Description of potential player usage implications

#### get_team_schedule

Fetches team schedule with matchup analysis and fantasy implications.

**Parameters:**
- `team_id`: Team abbreviation (e.g., 'KC', 'TB', 'NE')
- `season`: Season year (defaults to 2025)

**Returns:** Dictionary with the following fields:
- `team_id`: The team identifier used
- `team_name`: The team's full name
- `season`: Season year requested
- `schedule`: List of games with matchup details
- `count`: Number of games returned
- `success`: Whether the request was successful
- `error`: Error message (if any)

**Schedule Structure:**
Each game contains:
- `date`: Game date and time
- `week`: Week number
- `season_type`: Season type (Regular Season, Playoffs, etc.)
- `opponent`: Opponent team information
- `is_home`: Boolean indicating if it's a home game
- `result`: Game result (win/loss/scheduled)
- `fantasy_implications`: List of fantasy-relevant insights for the matchup

### Athlete Tools (MCP)

**Tools:** `fetch_athletes`, `lookup_athlete`, `search_athletes`, `get_athletes_by_team`

These tools provide comprehensive athlete data management with SQLite-based caching.

#### fetch_athletes

Fetches all NFL players from Sleeper API and stores them in the local SQLite database.

**Parameters:** None

**Returns:** Dictionary with athlete count, last updated timestamp, success status, and error (if any)

#### lookup_athlete

Look up an athlete by their unique ID.

**Parameters:**
- `athlete_id`: The unique identifier for the athlete

**Returns:** Dictionary with athlete information, found status, and error (if any)

#### search_athletes

Search for athletes by name (supports partial matches).

**Parameters:**
- `name`: Name or partial name to search for
- `limit`: Maximum number of results (default: 10, max: 100)

**Returns:** Dictionary with matching athletes, count, search term, and error (if any)

#### get_athletes_by_team

Get all athletes for a specific team.

**Parameters:**
- `team_id`: The team identifier (e.g., "SF", "KC", "NE")

**Returns:** Dictionary with team athletes, count, team ID, and error (if any)

**Example Usage with MCP Client:**
```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    # Fetch and cache all athletes
    result = await client.call_tool("fetch_athletes", {})
    print(f"Cached {result.data['athletes_count']} athletes")
    
    # Look up specific athlete
    result = await client.call_tool("lookup_athlete", {"athlete_id": "2307"})
    if result.data["found"]:
        print(f"Found: {result.data['athlete']['full_name']}")
    
    # Search by name
    result = await client.call_tool("search_athletes", {"name": "Mahomes"})
    for athlete in result.data["athletes"]:
        print(f"- {athlete['full_name']} ({athlete['position']})")
    
    # Get team roster
    result = await client.call_tool("get_athletes_by_team", {"team_id": "KC"})
    print(f"KC has {result.data['count']} players")
```

### Crawl URL Tool (MCP)

**Tool Name:** `crawl_url`

Crawls a URL and extracts its text content in a format understandable by LLMs.

**Parameters:**
- `url` (string): The URL to crawl (must include http:// or https://)
- `max_length` (integer, optional): Maximum length of extracted text (default: 10000 characters)

**Returns:** Dictionary with the following fields:
- `url`: The crawled URL
- `title`: Page title (if available)
- `content`: Cleaned text content
- `content_length`: Length of extracted content
- `success`: Whether the crawl was successful
- `error`: Error message (if any)

**Example Usage with MCP Client:**
```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    result = await client.call_tool("crawl_url", {
        "url": "https://example.com",
        "max_length": 1000
    })
    
    if result.data["success"]:
        print(f"Title: {result.data['title']}")
        print(f"Content: {result.data['content']}")
    else:
        print(f"Error: {result.data['error']}")
```

**Features:**
- Automatically extracts and cleans text content from HTML
- Removes scripts, styles, navigation, and footer elements
- Normalizes whitespace and formats text for LLM consumption
- Handles various error conditions (timeouts, HTTP errors, etc.)
- Configurable content length limiting
- Follows redirects automatically
- Sets appropriate User-Agent header

### Sleeper API Tools (MCP)

**Basic Tools:** `get_league`, `get_rosters`, `get_league_users`, `get_matchups`, `get_playoff_bracket`, `get_transactions`, `get_traded_picks`, `get_nfl_state`, `get_trending_players`
**Strategic Tools:** `get_strategic_matchup_preview`, `get_season_bye_week_coordination`, `get_trade_deadline_analysis`, `get_playoff_preparation_plan`

### Waiver Wire Analysis Tools (MCP)

**Tools:** `get_waiver_log`, `check_re_entry_status`, `get_waiver_wire_dashboard`

These advanced tools provide enhanced waiver wire intelligence for fantasy football decision making, addressing common issues like duplicate transaction tracking and identifying volatile players.

These tools provide comprehensive fantasy league management by connecting to the Sleeper API.

#### get_league

Get detailed information about a specific fantasy league.

**Parameters:**
- `league_id` (string): The unique identifier for the league

**Returns:** Dictionary with league information, success status, and error (if any)

#### get_rosters

Get all rosters in a fantasy league.

**Parameters:**
- `league_id` (string): The unique identifier for the league

**Returns:** Dictionary with rosters list, count, success status, and error (if any)

#### get_league_users

Get all users/managers in a fantasy league.

**Parameters:**
- `league_id` (string): The unique identifier for the league

**Returns:** Dictionary with users list, count, success status, and error (if any)

#### get_matchups

Get matchups for a specific week in a fantasy league.

**Parameters:**
- `league_id` (string): The unique identifier for the league
- `week` (integer): Week number (1-22)

**Returns:** Dictionary with matchups list, week, count, success status, and error (if any)

#### get_playoff_bracket

Get playoff bracket information for a fantasy league.

**Parameters:**
- `league_id` (string): The unique identifier for the league

**Returns:** Dictionary with bracket information, success status, and error (if any)

#### get_transactions

Get transactions for a fantasy league.

**Parameters:**
- `league_id` (string): The unique identifier for the league
- `round` (integer, optional): Round number (1-18, omit for all transactions)

**Returns:** Dictionary with transactions list, round, count, success status, and error (if any)

#### get_traded_picks

Get traded draft picks for a fantasy league.

**Parameters:**
- `league_id` (string): The unique identifier for the league

**Returns:** Dictionary with traded picks list, count, success status, and error (if any)

#### get_nfl_state

Get current NFL state information.

**Parameters:** None

**Returns:** Dictionary with NFL state information, success status, and error (if any)

#### get_trending_players

Get trending players from fantasy leagues.

**Parameters:**
- `trend_type` (string, optional): "add" or "drop" (default: "add")
- `lookback_hours` (integer, optional): Hours to look back (1-168, default: 24)
- `limit` (integer, optional): Maximum results (1-100, default: 25)

**Returns:** Dictionary with trending players list, parameters, count, success status, and error (if any)


### Strategic Planning Tools

#### get_strategic_matchup_preview

Strategic preview of upcoming matchups for multi-week planning. Combines Sleeper league data with NFL schedules to identify bye weeks, challenging periods, and opportunities 4-8 weeks ahead.

**Parameters:**
- `league_id` (string): The unique identifier for the league
- `current_week` (integer): Current NFL week (1-22)
- `weeks_ahead` (integer, optional): Weeks to analyze ahead (1-8, default: 4)

**Returns:** Dictionary with strategic analysis including bye week alerts, opportunity windows, and trade recommendations

#### get_season_bye_week_coordination

Season-long bye week coordination with fantasy league schedule. Analyzes entire NFL bye week calendar against your league's playoff schedule for strategic roster planning.

**Parameters:**
- `league_id` (string): The unique identifier for the league  
- `season` (integer, optional): NFL season year (default: 2025)

**Returns:** Dictionary with coordination plan including bye week calendar, strategic periods, and timing recommendations

#### get_trade_deadline_analysis

Strategic trade deadline timing analysis. Evaluates optimal trade timing by analyzing upcoming bye weeks, playoff schedules, and league patterns.

**Parameters:**
- `league_id` (string): The unique identifier for the league
- `current_week` (integer): Current NFL week for timeline analysis

**Returns:** Dictionary with trade analysis including timing windows, urgency factors, and strategic recommendations

#### get_playoff_preparation_plan

Comprehensive playoff preparation plan combining league and NFL data. Provides detailed preparation strategy including roster optimization and readiness assessment.

**Parameters:**
- `league_id` (string): The unique identifier for the league
- `current_week` (integer): Current NFL week for timeline analysis

**Returns:** Dictionary with playoff plan, strategic priorities, NFL schedule insights, and readiness score (0-100)

#### get_waiver_log

Get waiver wire log with optional de-duplication.

**Parameters:**
- `league_id` (string): The unique identifier for the league
- `round` (integer, optional): Round number (1-18, omit for all transactions)  
- `dedupe` (boolean, optional): Enable de-duplication (default: true)

**Returns:** Dictionary with waiver log, duplicates found, transaction counts, success status, and error (if any)

#### check_re_entry_status

Check re-entry status for players (dropped then re-added).

**Parameters:**
- `league_id` (string): The unique identifier for the league
- `round` (integer, optional): Round number (1-18, omit for all transactions)

**Returns:** Dictionary with re-entry analysis, volatile players list, player counts, success status, and error (if any)

#### get_waiver_wire_dashboard

Get comprehensive waiver wire dashboard with analysis.

**Parameters:**
- `league_id` (string): The unique identifier for the league
- `round` (integer, optional): Round number (1-18, omit for all transactions)

**Returns:** Dictionary with waiver log, re-entry analysis, dashboard summary, volatile players, success status, and error (if any)


**Example Usage with MCP Client:**
```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    # Get NFL state
    result = await client.call_tool("get_nfl_state", {})
    print(f"Current NFL week: {result.data['nfl_state']['week']}")
    
    # Get trending players
    result = await client.call_tool("get_trending_players", {
        "trend_type": "add",
        "limit": 10
    })
    print(f"Found {result.data['count']} trending players")
    
    # Get league information (requires valid league_id)
    result = await client.call_tool("get_league", {"league_id": "your_league_id"})
    if result.data["success"]:
        print(f"League: {result.data['league']['name']}")
        
    # Strategic planning - Preview upcoming challenges
    result = await client.call_tool("get_strategic_matchup_preview", {
        "league_id": "your_league_id",
        "current_week": 8,
        "weeks_ahead": 6
    })
    if result.data["success"]:
        critical_byes = result.data["strategic_preview"]["summary"]["critical_bye_weeks"]
        print(f"Critical bye weeks coming: {len(critical_byes)}")
        
    # Strategic planning - Trade deadline analysis
    result = await client.call_tool("get_trade_deadline_analysis", {
        "league_id": "your_league_id", 
        "current_week": 11
    })
    if result.data["success"]:
        phase = result.data["trade_analysis"]["strategic_windows"]["current_phase"]
        urgency = result.data["trade_analysis"]["strategic_windows"]["urgency"]
        print(f"Trade strategy: {phase} ({urgency} urgency)")
        
    # Strategic planning - Playoff preparation 
    result = await client.call_tool("get_playoff_preparation_plan", {
        "league_id": "your_league_id",
        "current_week": 12
    })
    if result.data["success"]:
        score = result.data["readiness_score"]
        phase = result.data["playoff_plan"]["preparation_phases"]["current_phase"]["name"]
        print(f"Playoff readiness: {score}/100 ({phase})")
```

## Development

### Running Tests
```bash
pytest tests/ -v --cov=nfl_mcp --cov-report=term-missing
```

### Project Structure
```
nfl_mcp/
├── nfl_mcp/           # Main package
│   ├── __init__.py
│   ├── server.py      # FastMCP server implementation
│   ├── database.py    # SQLite database management
│   └── config.py      # Shared configuration and utilities
├── tests/             # Unit tests
│   ├── __init__.py
│   └── test_server.py
├── Dockerfile         # Container definition
├── Taskfile.yml       # Task automation
├── pyproject.toml     # Project configuration
├── requirements.txt   # Dependencies
└── README.md
```

### Available Tasks

Run `task --list` to see all available tasks:

- `task install` - Install dependencies
- `task test` - Run unit tests with coverage
- `task run` - Run server locally
- `task build` - Build Docker image
- `task run-docker` - Build and run in Docker
- `task health-check` - Check server health
- `task clean` - Clean up Docker resources

## Security Considerations

This server implements several security measures:

- **Enhanced Input Validation**: 
  - String inputs are validated against injection patterns (SQL, XSS, command injection)
  - Numeric parameters have type checking and range validation
  - URL validation includes private network blocking and dangerous pattern detection
- **Content Sanitization**: Web crawled content is sanitized to remove scripts and dangerous patterns
- **URL Security**: Only HTTP/HTTPS URLs are allowed with additional security checks
- **Request Timeouts**: All HTTP requests have reasonable timeout limits (30s total, 10s connect)
- **User-Agent Headers**: All requests are properly identified
- **SQL Injection Prevention**: All database queries use parameterized statements
- **Rate Limiting**: Built-in rate limiting utilities to prevent abuse
- **No Code Execution**: The server does not execute arbitrary code or eval statements

**Input Validation Features:**
- SQL injection pattern detection and prevention
- XSS injection pattern detection and prevention  
- Command injection pattern detection and prevention
- Path traversal pattern detection and prevention
- HTML content sanitization with script removal
- Team ID, League ID, and other parameter format validation
- Safe character pattern matching for different input types

**Rate Limiting:**
- Configurable per-endpoint rate limiting
- In-memory storage for development (Redis recommended for production)
- Customizable limits and time windows
- Rate limit status reporting

When deploying in production:
- Run in a containerized environment
- Use proper network security controls
- Implement persistent rate limiting with Redis or similar
- Monitor for unusual request patterns
- Keep dependencies updated
- Consider additional WAF (Web Application Firewall) protection

## License

MIT License - see [LICENSE](LICENSE) file for details.