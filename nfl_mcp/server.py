#!/usr/bin/env python3
"""
NFL MCP Server - Simplified Architecture

A FastMCP server that provides:
- Health endpoint (non-MCP REST endpoint)
- URL crawling tool (MCP tool for web content extraction)
- NFL news tool (MCP tool for fetching latest NFL news from ESPN)
- NFL teams tool (MCP tool for fetching all NFL teams from ESPN)
- Athlete tools (MCP tools for fetching and looking up NFL athletes from Sleeper API)
- Sleeper API tools (MCP tools for comprehensive fantasy league management):
  - League information, rosters, users, matchups, playoffs
  - Transactions, traded picks, NFL state, trending players
- Waiver wire analysis tools (MCP tools for advanced fantasy football waiver management)
"""

from fastmcp import FastMCP
from starlette.responses import JSONResponse

from .database import NFLDatabase
from . import tool_registry


def create_app() -> FastMCP:
    """Create and configure the FastMCP server application."""
    
    # Create FastMCP server instance
    mcp = FastMCP(
        name="NFL MCP Server"
    )
    
    # Initialize NFL database
    nfl_db = NFLDatabase()
    
    # Initialize shared resources in tool registry
    tool_registry.initialize_shared(nfl_db)
    
    # Health endpoint (non-MCP, directly exposed REST endpoint)
    @mcp.custom_route(path="/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint for monitoring server status."""
        return JSONResponse({
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.1.0"
        })
    
    # Register all tools from the tool registry
    for tool_func in tool_registry.get_all_tools():
        mcp.tool(tool_func)
    
    return mcp


def main():
    """Main entry point for the server."""
    app = create_app()
    
    # Run the server with HTTP transport on port 9000
    app.run(transport="http", port=9000, host="0.0.0.0")


if __name__ == "__main__":
    main()