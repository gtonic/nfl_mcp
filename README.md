# NFL MCP Server

A comprehensive FastMCP server providing NFL data, fantasy league management, and web content tools through both REST and MCP protocols.

## Quick Start

### Installation
```bash
git clone https://github.com/gtonic/nfl_mcp.git
cd nfl_mcp
pip install -r requirements.txt
pip install -e ".[dev]"
```

### Run the Server
```bash
python -m nfl_mcp.server  # Local development
# OR
docker run --rm -p 9000:9000 gtonic/nfl-mcp-server  # Docker
```

Server runs on `http://localhost:9000` with health check at `/health`.

## Tool Categories

### NFL Data Tools
- **`get_nfl_news`** - Latest NFL news from ESPN
- **`get_teams`** - All NFL team information  
- **`fetch_teams`** - Update local team database
- **`get_depth_chart`** - Team depth charts and starters
- **`get_league_leaders`** - Statistical leaders (feature-flagged)

### Player Tools  
- **`fetch_athletes`** - Download all NFL player data (Sleeper API)
- **`search_athletes`** - Find players by name
- **`lookup_athlete`** - Get player by ID  
- **`get_athletes_by_team`** - Team rosters

### Fantasy League Tools (Sleeper API)
- **`get_league`** - League settings and info
- **`get_rosters`** - All team rosters
- **`get_league_users`** - League members
- **`get_matchups`** - Weekly matchups and scores
- **`get_playoff_bracket`** - Fantasy playoff bracket
- **`get_transactions`** - League transactions (adds/drops/trades)
- **`get_traded_picks`** - Traded draft picks
- **`get_nfl_state`** - Current NFL week/season
- **`get_trending_players`** - Popular waiver wire adds/drops

### Utility Tools
- **`crawl_url`** - Extract clean text from web pages

## Configuration

Configure via environment variables or config files (YAML/JSON):

```bash
# Environment variables
export NFL_MCP_TIMEOUT_TOTAL=45.0
export NFL_MCP_NFL_NEWS_MAX=75
export NFL_MCP_SERVER_VERSION="1.0.0"
```

```yaml
# config.yml
timeout:
  total: 45.0
  connect: 15.0
limits:
  nfl_news_max: 75
  athletes_search_max: 150
rate_limits:
  default_requests_per_minute: 120
```

See [CONFIGURATION.md](CONFIGURATION.md) for complete options.

## API Usage Examples

### MCP Client
```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    # Get NFL news
    result = await client.call_tool("get_nfl_news", {"limit": 10})
    
    # Search for players
    result = await client.call_tool("search_athletes", {"name": "Mahomes"})
    
    # Get league information  
    result = await client.call_tool("get_league", {"league_id": "your_league_id"})
```

### REST Health Check
```bash
curl http://localhost:9000/health
# Returns: {"status": "healthy", "service": "NFL MCP Server", "version": "0.1.0"}
```

## Tool Reference

### NFL News
```python
get_nfl_news(limit=50)
# Use for: "NFL news", "recent stories", "league updates"
# Returns: {articles: [...], total_articles, success}
```

### Player Search  
```python
search_athletes(name="Mahomes", limit=10)  
# Use for: "find player", "search roster", player names
# Returns: {athletes: [...], count, search_term}
```

### Fantasy League
```python
get_league(league_id="123456789012345678")
# Use for: league settings, scoring, roster positions
# Returns: {league: {...}, success}

get_matchups(league_id="123456789012345678", week=5)
# Use for: weekly matchups, scores, results  
# Returns: {matchups: [...], week, count}
```

### Web Content
```python
crawl_url(url="https://example.com", max_length=5000)
# Use for: analyze URLs, extract page content
# Returns: {url, title, content, content_length}
```

## Development

### Testing
```bash
pytest tests/ -v --cov=nfl_mcp
```

### Available Tasks (with [Task](https://taskfile.dev))
```bash
task test      # Run tests with coverage
task run       # Run server locally  
task build     # Build Docker image
task clean     # Clean up resources
```

### Project Structure
```
nfl_mcp/
├── nfl_mcp/              # Main package
│   ├── server.py         # FastMCP server setup
│   ├── tool_registry.py  # All MCP tools definitions
│   ├── database.py       # SQLite data management
│   ├── config*.py        # Configuration management
│   └── *_tools.py        # Tool implementations
├── tests/                # Comprehensive test suite
├── Dockerfile            # Container definition
└── README.md
```

## Security Features

- **Input Validation**: SQL injection, XSS, command injection prevention
- **URL Security**: Blocks private networks and dangerous patterns  
- **Content Sanitization**: Removes scripts and malicious content
- **Rate Limiting**: Configurable request limits
- **Request Timeouts**: Prevents hanging requests
- **Safe Database Operations**: Parameterized queries only

## License

MIT License - see [LICENSE](LICENSE) file for details.