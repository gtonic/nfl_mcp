"""Bounded waits on slow upstreams.

A tool call must answer within the MCP client's patience (a few minutes at
most, and an assistant gives up long before that). Several reads are slow by
nature -- the ESPN injury crawl is ~1900 requests behind a rate limiter, the
Sleeper player dump is ~5 MB -- and used to run inline, so a cold cache turned
a tool call into a multi-minute wait (``get_gameday_inactives`` passed 300 s
in the 2026-09-27 inactives window).

The pattern here: start the slow read as a background task (one per key at a
time -- concurrent callers share it), wait for it only as long as the caller's
budget allows, and on timeout answer with what is already cached while the
task finishes and fills the cache for the next call.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# key -> the running task (single flight per key).
_inflight: dict[str, asyncio.Task] = {}


def _forget(key: str, task: asyncio.Task) -> None:
    if _inflight.get(key) is task:
        _inflight.pop(key, None)
    if task.cancelled():
        return
    exc = task.exception()  # retrieved: no "exception was never retrieved"
    if exc is not None:
        logger.debug(f"[upstream] background read {key} failed: {exc!r}")


def single_flight(key: str, factory: Callable[[], Awaitable[Any]]) -> asyncio.Task:
    """The running task for ``key``, or a new one from ``factory()``.

    The task holds a strong reference here until it finishes, so it survives
    the caller that started it (the point: a timed-out caller leaves the read
    running and the next call finds the result in the cache).
    """
    loop = asyncio.get_running_loop()
    task = _inflight.get(key)
    if task is not None and not task.done() and task.get_loop() is loop:
        return task
    task = loop.create_task(factory(), name=f"upstream:{key}")
    _inflight[key] = task
    task.add_done_callback(lambda t, k=key: _forget(k, t))
    return task


def inflight(key: str) -> bool:
    """Whether a background read for ``key`` is running on this loop."""
    task = _inflight.get(key)
    if task is None or task.done():
        return False
    try:
        return task.get_loop() is asyncio.get_running_loop()
    except RuntimeError:
        return False


async def wait_bounded(task: asyncio.Task, timeout: float | None) -> tuple[bool, Any]:
    """``(finished, result)`` after waiting at most ``timeout`` seconds.

    The task is shielded: a timeout (or the caller being cancelled) leaves it
    running. A task that raised re-raises here.
    """
    if task.done():
        return True, task.result()
    if timeout is not None and timeout <= 0:
        return False, None
    try:
        return True, await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        return False, None


class Budget:
    """A deadline shared by the steps of one tool call."""

    def __init__(self, seconds: float):
        self.seconds = float(seconds)
        self.started = time.monotonic()
        self.deadline = self.started + self.seconds

    def remaining(self, cap: float | None = None, floor: float = 0.0) -> float:
        left = max(floor, self.deadline - time.monotonic())
        return min(left, cap) if cap is not None else left

    def elapsed(self) -> float:
        return time.monotonic() - self.started


def clear_inflight() -> None:
    """Forget every background read (tests: a task belongs to one event loop)."""
    _inflight.clear()
