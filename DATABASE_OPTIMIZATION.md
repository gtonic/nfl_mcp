# Database Optimization Features

The NFL MCP database has been enhanced with several optimization features to improve performance and scalability.

## Connection Pooling

The database now uses connection pooling to reduce connection overhead:

```python
from nfl_mcp.database import NFLDatabase, ConnectionPoolConfig

# Configure connection pool
pool_config = ConnectionPoolConfig(
    max_connections=5,           # Maximum connections in pool
    connection_timeout=30.0,     # Connection timeout in seconds  
    health_check_interval=60.0   # Health check interval in seconds
)

# Initialize database with pooling
db = NFLDatabase("nfl_data.db", pool_config)

# Get connection pool statistics
stats = db.get_connection_stats()
print(f"Pool utilization: {stats['pool_utilization']:.1f}%")
```

## Health Checks

Monitor database health and connectivity:

```python
# Synchronous health check
health = db.health_check()
print(f"Database healthy: {health['healthy']}")
print(f"Athlete count: {health['athlete_count']}")
print(f"Team count: {health['team_count']}")

# Async health check (requires aiosqlite)
import asyncio
async def check_health():
    health = await db.async_health_check()
    return health

health = asyncio.run(check_health())
```

## Optimized Indexing

The database now includes optimized indexes for better query performance:

- **Compound indexes**: For common query patterns like team + position
- **Covering indexes**: Include commonly accessed columns to avoid table lookups
- **Search indexes**: Case-insensitive text search with COLLATE NOCASE

Example queries that benefit from optimized indexes:

```python
# Fast team-based queries (uses idx_athletes_team_position)
team_qbs = db.get_athletes_by_team("KC")

# Fast name searches (uses idx_athletes_name_search) 
players = db.search_athletes_by_name("mahomes")

# Fast lookups (uses covering index idx_athletes_lookup)
player = db.get_athlete_by_id("12345")
```

## Database Migrations

Automatic schema versioning and migration system:

- **Schema version tracking**: Database schema version is tracked in `schema_version` table
- **Automatic migrations**: New schema changes are applied automatically on database initialization
- **Migration history**: Each migration is logged with timestamp

Current schema version: 2

Migration history:
- v1: Initial schema with basic tables and indexes
- v2: Optimized indexes for better query performance

## Async Operations

Async alternatives for high-concurrency scenarios (requires `aiosqlite`):

```python
import asyncio

async def async_operations():
    # Async athlete operations
    athlete = await db.async_get_athlete_by_id("12345")
    athletes = await db.async_search_athletes_by_name("brady")
    team_athletes = await db.async_get_athletes_by_team("TB")
    
    # Async team operations  
    team = await db.async_get_team_by_id("TB")
    team = await db.async_get_team_by_abbreviation("TB")
    all_teams = await db.async_get_all_teams()
    
    # Async bulk operations
    result = await db.async_upsert_athletes(athletes_data)
    result = await db.async_upsert_teams(teams_data)

# Run async operations
asyncio.run(async_operations())
```

## Performance Benefits

These optimizations provide several performance improvements:

1. **Reduced Latency**: Connection pooling eliminates connection setup overhead
2. **Better Concurrency**: Multiple threads can safely access the database simultaneously  
3. **Faster Queries**: Optimized indexes significantly improve query performance
4. **Scalability**: Async operations support high-concurrency scenarios
5. **Monitoring**: Health checks provide visibility into database performance

## Backward Compatibility

All existing database methods continue to work unchanged. The optimization features are additive and don't break existing code.

## Installation

To use async operations, install the optional aiosqlite dependency:

```bash
pip install aiosqlite>=0.19.0
```

The database will work without aiosqlite, but async operations will raise a `RuntimeError` if attempted.