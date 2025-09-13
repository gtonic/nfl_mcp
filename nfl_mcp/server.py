#!/usr/bin/env python3
"""
NFL MCP Server

A FastMCP server that provides comprehensive NFL and fantasy football tools:
- Health monitoring endpoint (REST)
- NFL data tools (news, teams, depth charts, league leaders)
- Athlete management tools (search, lookup, team rosters)
- Fantasy league tools via Sleeper API (leagues, rosters, matchups, transactions)
- Web content extraction tools
"""

from fastmcp import FastMCP
from starlette.responses import JSONResponse

from .database import NFLDatabase
from . import tool_registry


def create_app() -> FastMCP:
    """Create and configure the FastMCP server application."""
    
    # Create FastMCP server instance
    mcp = FastMCP(name="NFL MCP Server")
    
    # Initialize NFL database
    nfl_db = NFLDatabase()
    
    # Initialize the tool registry with database instance
    tool_registry.initialize_shared(nfl_db)
    
    # Register all tools from the tool registry
    for tool in tool_registry.get_tools():
        mcp.add_tool(tool)
    
    # Health endpoint (non-MCP, directly exposed REST endpoint)
    @mcp.custom_route(path="/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint for monitoring server status."""
        return JSONResponse({
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.1.0"
        })
    
    return mcp


def main():
    """Main entry point for the server."""
    app = create_app()
    
    # Run the server with HTTP transport on port 9000
    app.run(transport="http", port=9000, host="0.0.0.0")


if __name__ == "__main__":
    main()
