# NFL MCP Server

A FastMCP server that provides health monitoring and arithmetic operations through both REST and MCP protocols.

## Features

- **Health Endpoint**: Non-MCP REST endpoint at `/health` for monitoring server status
- **Multiply Tool**: MCP tool that multiplies two integers and returns the result
- **URL Crawling Tool**: MCP tool that crawls arbitrary URLs and extracts LLM-friendly text content
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