"""Per-tool-call collection of input-validation warnings.

Validators that correct an argument (clamping ``limit=500`` to the maximum
of 100) must not do so silently: they record a warning here, and the tool
wrapper (``metrics.timing_decorator``) attaches the collected warnings to the
tool's response as ``input_warnings``. Outside a tool call nothing is
collected and ``add_input_warning`` only logs.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar

logger = logging.getLogger(__name__)

_warnings: ContextVar[list[str] | None] = ContextVar("nfl_mcp_input_warnings", default=None)


def add_input_warning(message: str) -> None:
    """Record (and log) a correction applied to a caller-supplied argument."""
    logger.warning("Input corrected: %s", message)
    bucket = _warnings.get()
    if bucket is not None and message not in bucket:
        bucket.append(message)


def begin_collection():
    """Start collecting for the current call; returns a token for ``end_collection``."""
    return _warnings.set([])


def end_collection(token) -> list[str]:
    """Stop collecting and return what was recorded since ``begin_collection``."""
    collected = _warnings.get() or []
    _warnings.reset(token)
    return collected


def attach_warnings(result, collected: list[str]):
    """Add ``input_warnings`` to a dict result (other results are returned as-is)."""
    if collected and isinstance(result, dict):
        existing = result.get("input_warnings")
        if isinstance(existing, list):
            existing.extend(w for w in collected if w not in existing)
        else:
            result["input_warnings"] = list(collected)
    return result
