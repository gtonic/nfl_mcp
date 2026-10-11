"""MCP tool wrappers, grouped by domain.

Each module holds the thin, validated entry points the server registers (the
heavy lifting lives in the ``nfl_mcp.*_tools`` / service modules). Which tools
are exposed, and in what order, is decided by ``nfl_mcp.tool_registry``.
"""
