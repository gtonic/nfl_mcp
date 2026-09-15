# FastMCP 4.0 Upgrade Summary

## Overview
Upgraded the NFL MCP Server from FastMCP 3.4.x to **FastMCP 4.0.0b1** and switched
the HTTP transport to **stateless MCP** — the sessionless `2026-07-28` protocol.
FastMCP 4 rebuilds on **MCP Python SDK v2** and serves the sessionless protocol
via mode negotiation, while still answering handshake-era clients from the same
endpoint.

## Date
August 5, 2026

## Why "stateless"?
The `2026-07-28` MCP revision removes the `initialize` handshake and the
protocol-level session: every request is self-contained (protocol data travels in
HTTP headers + the request-body `_meta` envelope). Enabling `stateless_http` on
the Streamable HTTP transport means the server keeps **no** server-side session
objects and issues no `Mcp-Session-Id`, so the deployment can scale horizontally
behind a plain round-robin load balancer — no sticky sessions, no shared session
store. (Background: https://simonwillison.net/2026/Jul/31/stateless-mcp/)

## Changes Made

### 1. Dependency Updates
- **pyproject.toml** / **requirements.txt**: `fastmcp>=3.4.5` → `fastmcp>=4.0.0b1`.
- Added defensive upper bounds **`httpx<1`** and **`pydantic<2.14`** (see Gotchas).
- **requirements.lock** regenerated for Python 3.13:
  ```
  uv pip compile requirements.txt --python-version 3.13 --prerelease=allow --output-file requirements.lock
  ```
  Resulting key pins: `fastmcp==4.0.0b1`, `fastmcp-slim==4.0.0b1`, `mcp==2.0.0`,
  `mcp-types==2.0.0`, `httpx==0.28.1`, `httpx2==2.9.1`, `pydantic==2.13.4`,
  `starlette==1.3.1`, `fastapi==0.141.1`, `uvicorn==0.52.1`.
- **Dockerfile**: updated the `requirements.lock` regeneration comment to include
  `--prerelease=allow`.

### 2. Server Code (`nfl_mcp/server.py`)
- **`create_app()`**: the background prefetch/shutdown lifespan is now passed to
  the **public** `FastMCP(name=…, lifespan=…)` constructor argument. FastMCP
  composes it with the transport's own session-manager lifespan. (`nfl_db` is
  created just before the `FastMCP(...)` call so it can be closed over.)
- **`main()`**: removed the fragile monkey-patch of the ASGI app's internal
  `router.lifespan_context`. The MCP HTTP app is now built with
  `app.http_app(path="/mcp", stateless_http=stateless_http)`, where
  `stateless_http` is read from `NFL_MCP_STATELESS_HTTP` (default `1` / enabled).
  Set `NFL_MCP_STATELESS_HTTP=0` to fall back to the session-based transport.

## What Didn't Change
- **No tool implementations changed.** All 77 tools register and run unchanged.
- The codebase used **none** of the v4-removed APIs: no `ctx.sample()` /
  `ctx.sample_step()` / `ctx.list_roots()` / `ctx.elicit()`, no direct
  `mcp.types` model construction, no removed transport kwargs on the `FastMCP()`
  constructor, no `import_server`/`as_proxy`/`McpError(ErrorData(...))`/tool
  `serializer=`/`exclude_args=`.
- Custom `/health` route, tool registration, and the async `list_tools()` API are
  all unchanged. The existing test suite needed **no** edits.

## Test Results
- **906 passed, 2 skipped** (the 2 skips are the opt-in `live` tests). ✅
- 4 warnings — pre-existing `AsyncMock` "coroutine never awaited" test artifacts,
  unrelated to the upgrade.

## Validation Performed
1. ✅ **Import + `create_app()`**: FastMCP instance built, 77 tools registered.
2. ✅ **Unit tests**: full suite green under FastMCP 4 (Python 3.11).
3. ✅ **Server startup**: `python -m nfl_mcp.server` boots; `/health` → `200`.
4. ✅ **Lifespan**: the constructor `lifespan=` runs on ASGI startup **and**
   shutdown (verified via `TestClient`).
5. ✅ **Stateless round-trip**: `fastmcp.Client` (modern/auto mode) lists 77
   tools; a **second independent client** works identically; `/mcp` returns **no**
   `Mcp-Session-Id` header. A hand-crafted legacy `initialize` is correctly
   rejected with `params._meta must be an object carrying
   'io.modelcontextprotocol/protocolVersion' …` — i.e. the endpoint enforces the
   new `2026-07-28` envelope.
6. ✅ **Docker parity**: on a fresh **Python 3.13** venv,
   `pip install -r requirements.lock` → `pip install -e .` → `import
   nfl_mcp.server` all succeed (mirrors the Dockerfile build + smoke test).

## Gotchas (beta sharp edges)
- **`uv` prerelease footgun.** `fastmcp==4.0.0b1` pins a pre-release
  `fastmcp-slim==4.0.0b1`, so `uv pip install -e .` fails without
  `--prerelease=allow`. But that flag is **global**, and uv will then also resolve
  `httpx==1.0.dev*` (a breaking-major dev release whose API drops
  `HTTPStatusError` etc.) and `pydantic==2.14.0a*`. The `httpx<1` / `pydantic<2.14`
  bounds prevent that. The correct uv command is:
  ```
  uv pip install --prerelease=allow -e ".[dev]"
  ```
- **`pip` is simpler.** Plain `pip install -e .` (no `--pre`) resolves correctly:
  pip enables pre-releases only for packages whose specifier references one, so it
  installs `fastmcp==4.0.0b1` while keeping `httpx`/`pydantic` on stable releases.
- **`4.0.0b1` is a beta** — pin an exact version and expect sharp edges. Revisit
  the pin when FastMCP 4 reaches GA.

## References
- Stateless MCP (Simon Willison): https://simonwillison.net/2026/Jul/31/stateless-mcp/
- Beta SDKs for the 2026-07-28 MCP spec: https://blog.modelcontextprotocol.io/posts/sdk-betas-2026-07-28/
- What's New in FastMCP 4: https://gofastmcp.com/getting-started/whats-new
- Upgrading from FastMCP 3: https://gofastmcp.com/getting-started/upgrading/from-fastmcp-3
- FastMCP Changelog: https://gofastmcp.com/changelog
