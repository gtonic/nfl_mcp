# NFL MCP Server

A FastMCP server that provides health monitoring, arithmetic operations, web content extraction, NFL news fetching, NFL teams information, and comprehensive fantasy league management through both REST and MCP protocols.

## Features

- **Health Endpoint**: Non-MCP REST endpoint at `/health` for monitoring server status
- **Multiply Tool**: MCP tool that multiplies two integers and returns the result
- **URL Crawling Tool**: MCP tool that crawls arbitrary URLs and extracts LLM-friendly text content
- **NFL News Tool**: MCP tool that fetches the latest NFL news from ESPN API
- **NFL Teams Tools**: Comprehensive MCP tools for NFL teams including:
  - Team data fetching and database caching from ESPN API
  - Depth chart retrieval for individual teams
- **Athlete Tools**: MCP tools for fetching, caching, and looking up NFL athletes from Sleeper API with SQLite persistence
- **Sleeper API Tools**: Comprehensive MCP tools for fantasy league management including:
  - League information, rosters, users, matchups, playoff brackets
  - Transactions, traded picks, NFL state, trending players
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

## API Documentation

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

**Tools:** `get_league`, `get_rosters`, `get_league_users`, `get_matchups`, `get_playoff_bracket`, `get_transactions`, `get_traded_picks`, `get_nfl_state`, `get_trending_players`

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
```

### Multiply Tool (MCP)

**Tool Name:** `multiply`

Multiplies two integer numbers and returns the result.

**Parameters:**
- `x` (integer): First number to multiply
- `y` (integer): Second number to multiply

**Returns:** Integer result of x * y

**Example Usage with MCP Client:**
```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    result = await client.call_tool("multiply", {"x": 5, "y": 3})
    print(result.data)  # Output: 15
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
│   └── server.py      # FastMCP server implementation
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

## License

MIT License - see [LICENSE](LICENSE) file for details.