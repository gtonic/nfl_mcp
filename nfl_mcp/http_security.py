"""HTTP transport hardening: optional bearer auth and Host/Origin allowlisting.

- ``NFL_MCP_AUTH_TOKEN``: when set, every ``/mcp`` request must carry
  ``Authorization: Bearer <token>`` (compared in constant time). ``/health``
  stays open but only returns status/version without the token.
- ``NFL_MCP_ALLOWED_HOSTS``: extra Host header values (comma list, ``*``
  wildcards allowed) on top of localhost/127.0.0.1/[::1]. Requests with any
  other Host get 421, which blocks DNS-rebinding attacks from a browser.
- ``NFL_MCP_ALLOWED_ORIGINS``: extra browser origins (comma list). A request
  whose ``Origin`` header is present and not allowed gets 403; non-browser
  clients (Claude Code, curl, the Docker healthcheck) send no Origin.

Both checks use FastMCP's own machinery (``TokenVerifier`` auth provider and
``HostOriginGuardMiddleware`` via ``http_app(host_origin_protection=True)``).
"""
from __future__ import annotations

import hmac
import os

from fastmcp.server.auth import AccessToken, TokenVerifier

DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1")


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def auth_token() -> str | None:
    """The configured bearer token, or None when auth is disabled."""
    token = os.getenv("NFL_MCP_AUTH_TOKEN", "").strip()
    return token or None


def allowed_hosts() -> list[str]:
    """Host header allowlist: the loopback names plus ``NFL_MCP_ALLOWED_HOSTS``."""
    hosts = list(DEFAULT_ALLOWED_HOSTS)
    for host in _env_list("NFL_MCP_ALLOWED_HOSTS"):
        if host not in hosts:
            hosts.append(host)
    return hosts


def allowed_origins() -> list[str]:
    """Extra trusted browser origins (loopback origins are always accepted)."""
    return _env_list("NFL_MCP_ALLOWED_ORIGINS")


class StaticBearerTokenVerifier(TokenVerifier):
    """Accepts exactly one shared secret, compared with ``hmac.compare_digest``.

    (FastMCP's ``StaticTokenVerifier`` uses a dict lookup, which is not
    constant-time.)
    """

    def __init__(self, token: str):
        super().__init__()
        self._token = token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not hmac.compare_digest(token.strip().encode("utf-8"), self._token):
            return None
        return AccessToken(token=token, client_id="nfl-mcp-client", scopes=[])


def build_auth() -> StaticBearerTokenVerifier | None:
    """Auth provider for ``FastMCP(auth=...)``; None when no token is configured."""
    token = auth_token()
    return StaticBearerTokenVerifier(token) if token else None


def request_is_authenticated(request) -> bool:
    """Whether a Starlette request carried the valid bearer token.

    FastMCP's auth middleware runs app-wide, so custom routes (``/health``,
    ``/metrics``) see ``request.user`` populated even though only ``/mcp``
    enforces it.
    """
    if "user" not in request.scope:
        return False
    user = request.scope["user"]
    return bool(getattr(user, "is_authenticated", False))
