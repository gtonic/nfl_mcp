"""
NFL MCP Server Package

A FastMCP server providing NFL fantasy football data, analysis, and lineup optimization.
"""

from ._version import __version__
from .server import create_app, main

__all__ = ["__version__", "create_app", "main"]
