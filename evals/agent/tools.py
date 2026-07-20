"""Build Anthropic tool definitions from the live MCP tool registry.

An assistant routes on tool **name + description + input schema**. We derive those
straight from `tool_registry.get_all_tools()` (introspecting each function's
signature and docstring), so the eval sees exactly the tools production exposes.
Pure/offline — no network, no API key.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any, Callable, Dict, List

# Params that are injected internally, never chosen by the model.
_SKIP_PARAMS = {"self", "db"}


def _json_type(annotation: Any) -> Dict[str, Any]:
    """Map a Python annotation to a (permissive) JSON-schema type."""
    if annotation is inspect._empty:  # no annotation
        return {"type": "string"}
    origin = typing.get_origin(annotation)
    if origin is typing.Union:  # Optional[X] / Union -> first non-None arg
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _json_type(args[0]) if args else {"type": "string"}
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if origin in (list, typing.List):
        return {"type": "array"}
    if origin in (dict, typing.Dict):
        return {"type": "object"}
    return {"type": "string"}


def _description(func: Callable) -> str:
    doc = (inspect.getdoc(func) or "").strip()
    # First paragraph is the summary; strip the LLM-agent boilerplate line.
    para = doc.split("\n\n")[0].strip()
    return para[:1024] or func.__name__


def build_tool(func: Callable) -> Dict[str, Any]:
    """Anthropic tool def for one registry function."""
    sig = inspect.signature(func)
    props: Dict[str, Any] = {}
    required: List[str] = []
    for name, p in sig.parameters.items():
        if name in _SKIP_PARAMS:
            continue
        props[name] = _json_type(p.annotation)
        if p.default is inspect._empty:
            required.append(name)
    return {
        "name": func.__name__,
        "description": _description(func),
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


def anthropic_tools_from_registry() -> List[Dict[str, Any]]:
    """All registered MCP tools as Anthropic tool definitions."""
    from nfl_mcp import tool_registry
    return [build_tool(fn) for fn in tool_registry.get_all_tools()]


def registry_tool_names() -> set:
    from nfl_mcp import tool_registry
    return {fn.__name__ for fn in tool_registry.get_all_tools()}
