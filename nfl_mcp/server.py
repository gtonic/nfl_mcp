#!/usr/bin/env python3
"""
NFL MCP Server

A FastMCP server that provides:
- Health endpoint (non-MCP REST endpoint)
- Multiply tool (MCP tool for arithmetic operations)
"""

from fastmcp import FastMCP


def create_app() -> FastMCP:
    """Create and configure the FastMCP server application."""
    
    # Create FastMCP server instance
    mcp = FastMCP(
        name="NFL MCP Server"
    )
    
    # Health endpoint (non-MCP, directly exposed REST endpoint)
    @mcp.custom_route(path="/health", methods=["GET"])
    async def health_check():
        """Health check endpoint for monitoring server status."""
        return {
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.1.0"
        }
    
    # MCP Tool: Multiply two integers
    @mcp.tool
    def multiply(x: int, y: int) -> int:
        """
        Multiply two integer numbers and return the result.
        
        Args:
            x: First integer to multiply
            y: Second integer to multiply
            
        Returns:
            The product of x and y
        """
        return x * y
    
    return mcp


def main():
    """Main entry point for the server."""
    app = create_app()
    
    # Run the server with HTTP transport on port 9000
    app.run(transport="http", port=9000, host="0.0.0.0")


if __name__ == "__main__":
    main()