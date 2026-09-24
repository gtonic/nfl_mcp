"""Keep secrets out of the logs.

httpx logs every request URL at INFO -- including the Odds API key, which
travels as the ``apiKey`` query parameter -- and ``HTTPStatusError`` messages
embed the same URL. :func:`install_log_redaction` quiets httpx/httpcore to
WARNING and attaches :class:`SecretRedactionFilter` to the root handlers so
whatever still gets through is masked.
"""
from __future__ import annotations

import logging
import os
import re

_REDACTED = "***"

# (pattern, replacement) -- the key name is kept so the log stays readable.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(api_?key|apikey|access_token|token)=[^&\s'\"]+"), r"\1=" + _REDACTED),
    (re.compile(r"(?i)\b(authorization:\s*bearer\s+)[^\s'\",]+"), r"\1" + _REDACTED),
    (re.compile(r"(?i)(['\"]authorization['\"]\s*:\s*['\"]bearer\s+)[^'\"]+"), r"\1" + _REDACTED),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1" + _REDACTED),
)

_NOISY_HTTP_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")


def redact(text: str) -> str:
    """``text`` with API keys and bearer tokens replaced by ``***``."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SecretRedactionFilter(logging.Filter):
    """Rewrites a record's message (and traceback text) with secrets masked."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Idempotently add the filter to ``logger``'s (default: root) handlers and
    quiet the HTTP client loggers (override with ``NFL_MCP_HTTPX_LOG_LEVEL``)."""
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())
    level_name = os.getenv("NFL_MCP_HTTPX_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    for name in _NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(level)
