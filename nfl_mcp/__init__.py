"""
NFL MCP Server Package

A FastMCP server providing health endpoints and arithmetic tools.
"""

from .server import create_app, main

__version__ = "0.1.0"
__all__ = ["create_app", "main"]