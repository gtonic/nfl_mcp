"""Single source of truth for the package version.

``pyproject.toml`` owns the number. A source checkout (dev run, the Docker
image's ``/app``) reads it straight from there, because an editable install's
metadata is frozen at install time and goes stale on every bump. Anything else
(a wheel install) uses the installed distribution's metadata.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIST_NAME = "nfl_mcp"
_FALLBACK = "0.0.0+unknown"


def _version_from_pyproject() -> str | None:
    candidate = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        import tomllib

        with open(candidate, "rb") as f:
            project = tomllib.load(f).get("project", {})
    except (OSError, ValueError):
        return None
    if project.get("name", "").replace("-", "_") != _DIST_NAME:
        return None  # someone else's pyproject.toml
    return project.get("version") or None


def _version_from_metadata() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - stdlib since 3.8
        return None
    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:
        return None


@lru_cache(maxsize=1)
def get_version() -> str:
    """The package version: source ``pyproject.toml``, else installed metadata."""
    return _version_from_pyproject() or _version_from_metadata() or _FALLBACK


__version__ = get_version()
