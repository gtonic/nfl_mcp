"""Agent / tool-use evals (Eval Layer C).

Given a natural-language prompt, does an LLM using our MCP tools pick the *right*
tool(s) with sensible args? The tool schemas are derived from the live tool
registry, so this evaluates the tools an assistant actually sees.

The live run needs an ``ANTHROPIC_API_KEY`` (it calls the model); the scenario
set, schema builder and registry guards are validated offline in
``tests/test_agent_scenarios.py``.
"""
