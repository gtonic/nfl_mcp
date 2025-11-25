"""
NFL MCP Server Package

A FastMCP server providing NFL fantasy football data, analysis, and lineup optimization.
"""

from .server import create_app, main

__version__ = "0.5.15"
__all__ = ["create_app", "main"]