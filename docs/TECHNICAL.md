# NFL MCP — Technical Guide

Setup, configuration, architecture and operations for the NFL MCP server. For
what the server *does* (features, tool catalog), see the [README](../README.md);
for the full per-tool reference see [AGENT.md](../AGENT.md).

---

## Requirements

- **Python 3.11+** (the package uses `datetime.UTC`/`tomllib`; CI tests 3.11 & 3.12)
- **Docker** (optional, for containerized deployment)
- **[Task](https://taskfile.dev/installation/)** (optional, for the Taskfile shortcuts)

## Installation

```bash
git clone https://github.com/gtonic/nfl_mcp.git
cd nfl_mcp
pip install -r requirements.txt
pip install -e ".[dev]"
```

For reproducible installs, `requirements.lock` pins every transitive dependency
(generated with `uv pip compile requirements.txt --python-version 3.11`); the
Docker image installs from it.

## Running the server

The server speaks **MCP over HTTP** at `http://localhost:9000/mcp/` (port 9000,
entrypoint module `nfl_mcp.server`). A non-MCP health endpoint lives at
`GET /health` → `{"status":"healthy","service":"NFL MCP Server","version":"0.6.0"}`.

```bash
# Local
python -m nfl_mcp.server

# Docker — published image (CI publishes to GHCR on push to main and on tags)
docker run --rm -p 9000:9000 ghcr.io/gtonic/nfl_mcp:latest
# ...or pin a version tag, e.g. ghcr.io/gtonic/nfl_mcp:0.6.0

# Docker — build locally
docker build -t nfl-mcp-server .
docker run --rm -p 9000:9000 nfl-mcp-server

# Taskfile
task run          # run locally
task run-docker   # run in Docker
task all          # full pipeline
```

The SQLite database is only a **cache** (athletes, schedules, enrichment). It
lives inside the container and repopulates from the source APIs on demand (e.g.
`fetch_athletes` or background prefetch), so losing it on restart is harmless —
**no volume required**.

## Going live: connect your AI client

Recommended production start with enrichment + cache warming:

```bash
docker run -d --name nfl-mcp -p 9000:9000 \
  -e NFL_MCP_ADVANCED_ENRICH=1 \   # real snap%/usage enrichment (in-season)
  -e NFL_MCP_PREFETCH=1 \          # warm caches in the background
  -e ODDS_API_KEY=your_key_here \  # optional: live Vegas lines (the-odds-api.com)
  ghcr.io/gtonic/nfl_mcp:latest

curl -s http://localhost:9000/health | jq .status   # -> "healthy"
```

**Claude Code (CLI):**
```bash
claude mcp add --transport http nfl-mcp http://localhost:9000/mcp/
# then in a session:  /mcp   (verify "nfl-mcp" is connected and lists tools)
```

**Claude Desktop / Cursor / other stdio clients** — bridge via
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):
```json
{
  "mcpServers": {
    "nfl-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:9000/mcp/"]
    }
  }
}
```

**Programmatic (Python):**
```python
from fastmcp import Client
async with Client("http://localhost:9000/mcp/") as client:
    print(await client.call_tool("get_player_values", {"scoring": "ppr"}))
```

**Point it at your Sleeper league:** ask your assistant *"My Sleeper username is
`your_name` — find my 2026 leagues"* (runs `get_user` → `get_user_leagues`), or
read the `league_id` from the app URL `sleeper.com/leagues/<league_id>/...`.

### Live draft CLIs (`evals.live`)

```bash
# Pre-draft flight check against your real league/draft
python -m evals.live.validate_draft --username your_sleeper_name --season 2026
#   ...or --league-id <id> / --draft-id <id>

# Live "war room" — recommends a pick each time you're on the clock
python -m evals.live.draft_watch --draft-id <draft_id> --my-slot 4
```

## Configuration

Configuration comes from environment variables and/or a config file
(`config.example.json` / `config.example.yml` show the shape). Environment
variables take precedence.

### Environment variables

| Variable | Meaning |
|---|---|
| `ODDS_API_KEY` | Enables live Vegas lines/totals ([the-odds-api.com](https://the-odds-api.com)). Without it, Vegas tools return neutral placeholders. Player values (FantasyCalc) need **no** key. |
| `NFL_MCP_ADVANCED_ENRICH` | `1` enables snap%, opponent, practice status and usage-trend enrichment (Schema v8). Also lets schedule fetches run. |
| `NFL_MCP_ALLOW_PRIVATE_URLS` | `1` lets `crawl_url` reach private/loopback addresses. Off by default (SSRF protection — see [SECURITY.md](../SECURITY.md)). |
| `NFL_MCP_PREFETCH` | `1` enables background data prefetch (cache warming). |
| `NFL_MCP_PREFETCH_INTERVAL` | Prefetch interval, seconds (default 900). |
| `NFL_MCP_PREFETCH_SNAPS_TTL` | Snap-data TTL, seconds (default 900). |
| `NFL_MCP_PREFETCH_SCHEDULE_WEEKS` | Weeks of schedule to prefetch (default 4). |
| `NFL_MCP_TIMEOUT_TOTAL` | Total HTTP request timeout (e.g. `45.0`). |
| `NFL_MCP_RATE_LIMIT_DEFAULT` | Default outbound rate limit (requests/min). |
| `NFL_MCP_NFL_NEWS_MAX` | Max NFL news items. |
| `NFL_MCP_SERVER_VERSION` | Server version string reported by `/health`. |
| `NFL_MCP_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` (default `INFO`). |

### Config file (`config.yml`)

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

## Architecture

```
nfl_mcp/
├── nfl_mcp/                 # Main package
│   ├── server.py           # FastMCP server (thin: lifespan + health + registry)
│   ├── tool_registry.py    # Single place every MCP tool is registered
│   ├── database.py         # SQLite cache + schema migrations
│   ├── config.py           # Shared config, HTTP client, validation, SSRF guard
│   ├── config_manager.py   # Env/file configuration model
│   ├── projections.py      # Transparent weekly point projections
│   ├── matchup_tools.py    # Defense-vs-position + offense strength (nflverse)
│   ├── sos_tools.py         # Strength of schedule (ROS / playoff weeks)
│   ├── streaming_tools.py   # Weekly DST/K/QB/TE streaming planner
│   ├── weather_tools.py     # Open-Meteo wind/weather + fantasy impact
│   ├── vegas_tools.py       # The Odds API lines / implied totals
│   ├── sleeper_tools.py     # Sleeper league/roster/draft/transactions + enrichment
│   └── ...                  # values, waivers, trade, injuries, coaching, cbs, ...
├── tests/                  # ~850 tests (unit; live tests gated behind --run-live)
├── evals/                  # 3-layer eval suite (see below)
├── docs/                   # This guide, DRAFT_DAY.md, research notes
├── Dockerfile · Taskfile.yml · pyproject.toml · requirements.lock
```

Design principles:
- **Single tool registry** — every tool is registered in `tool_registry.py`; the
  server module stays thin.
- **ContextVar-based DB injection** — async-safe database access without a
  mutable global.
- **SQLite as cache only** — ephemeral, repopulates from APIs; no persistence
  required.

### Data sources (no paid key required to start)

| Source | Used for | Key? |
|---|---|---|
| **Sleeper** | Your live league: leagues, rosters, drafts, transactions, trending, NFL state, players map | No |
| **nflverse** | Real weekly player stats → defense-vs-position, offense strength, backtests | No |
| **ESPN** | News, teams, depth charts, injuries, standings, schedules, league leaders | No |
| **FantasyCalc** | Market-consensus player values (trades, draft board), format-aware | No |
| **CBS Sports** | Player news, projections, expert picks | No |
| **Open-Meteo** | Per-game wind/precipitation/temperature (weather tool) | No |
| **The Odds API** | Live Vegas lines / implied totals | `ODDS_API_KEY` |

### Enrichment (opt-in via `NFL_MCP_ADVANCED_ENRICH=1`)

- **Schema v7 (additive):** `snap_pct` (+ `snap_pct_source`), `opponent`
  (+ `opponent_source`). Estimated snap% uses depth-chart heuristics
  (starter ≈ 70, #2 ≈ 45, others ≈ 15).
- **Schema v8:** `practice_status` (DNP/LP/FP/Full) with freshness fields,
  `usage_last_3_weeks` (targets/routes/rz_touches/snap_share averages),
  `usage_source`, and `usage_trend` (up/down/flat, 15% threshold).

### Robustness & snapshots

Robust endpoints (`get_rosters`, `get_transactions`, `get_matchups`) retry with
backoff and, on total failure, return the most recent cached snapshot with
`success=false` but usable data. Snapshot metadata: `retries_used`, `stale`,
`failure_reason`, `snapshot_fetched_at`, `snapshot_age_seconds` (present but
`null` on a fresh success).

## Eval suite (`evals/`)

A three-layer suite that keeps the intelligence honest:

- **Layer A — accuracy backtest** (`evals/backtest/`): leak-free walk-forward
  backtest measuring whether projections beat a trailing-PPG baseline against
  real nflverse outcomes (MAE/RMSE/Spearman). Imports the live constants so it
  grades production. Scheduled, non-blocking `evals.yml`.
- **Layer B — data-source contracts** (`evals/contracts/`): a daily
  `contracts.yml` watchdog that asserts the fields we depend on (`sleeperId`,
  `off_snp`, `opponent_team`, …) still exist upstream.
- **Layer C — agent tool-routing** (`evals/agent/`): scenarios mapping realistic
  prompts to the tool(s) an assistant should call; offline guards run in CI.

The backtest's headline finding — that flat multipliers barely move accuracy —
is why new signals (e.g. weather) ship as standalone tools first and only enter
the projection formula after they earn it on the backtest.

## CI/CD

`.github/workflows/ci.yml`:
1. **Lint** — `ruff check .` (rules F/B/I) gates everything.
2. **Tests** — full suite on Python 3.11 & 3.12 with coverage.
3. **Docker** — builds the image on every run (validates the Dockerfile on PRs)
   and publishes to `ghcr.io/gtonic/nfl_mcp` on pushes to `main` and version tags.

Additional scheduled/on-demand workflows: `evals.yml`, `contracts.yml`,
`agent-evals.yml`.

## Rate limiting

Built-in per-endpoint outbound rate limiting (in-memory for development; use
Redis for production). Configurable via `rate_limits.default_requests_per_minute`
/ `NFL_MCP_RATE_LIMIT_DEFAULT`, with rate-limit status reporting.

## Security

Input validation (SQL/XSS/command-injection/path-traversal detection), content
sanitization, parameterized SQL, request timeouts, and SSRF protection on
`crawl_url` (DNS-resolving private/link-local/metadata blocking + per-hop
redirect validation). The MCP transport has **no built-in auth** — do not expose
the port to untrusted networks; bind to localhost or place it behind an
authenticating reverse proxy. Full details and reporting policy in
[SECURITY.md](../SECURITY.md).

## Development

```bash
pytest -q                       # full unit suite (live API tests skipped)
pytest -q --run-live            # also run tests that hit real external APIs
pytest -q --cov=nfl_mcp --cov-report=term-missing
ruff check .                    # lint gate
```

Common Taskfile targets: `task install`, `task test`, `task run`, `task build`,
`task run-docker`, `task health-check`, `task clean` (`task --list` for all).
