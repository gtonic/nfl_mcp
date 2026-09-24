"""Infrastructure hardening: breakers, outbound rate limits, config overrides,
startup, .env loading, input warnings, /metrics, crawl_url limits + DNS pinning,
the injury-list 304 path and the agent-eval skip exit code."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpcore
import httpx
import pytest
import yaml

from nfl_mcp import config, config_manager, retry_utils
from nfl_mcp.retry_utils import (
    CircuitState,
    RetryableHTTPStatus,
    get_circuit_breaker,
    raise_for_retryable_status,
    retry_with_backoff,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Circuit breakers: one failure per logical call; 5xx/429 fail, 4xx do not
# ---------------------------------------------------------------------------

class TestCircuitBreakerAccounting:
    @pytest.fixture(autouse=True)
    def _fresh_breakers(self):
        saved = dict(retry_utils._circuit_breakers)
        retry_utils._circuit_breakers.clear()
        yield
        retry_utils._circuit_breakers.clear()
        retry_utils._circuit_breakers.update(saved)

    @pytest.mark.asyncio
    async def test_exhausted_retries_count_one_failure(self):
        func = AsyncMock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(httpx.ConnectError):
            await retry_with_backoff(func, max_retries=3, initial_delay=0, circuit_breaker_name="cb_one")
        assert func.await_count == 4
        cb = get_circuit_breaker("cb_one")
        assert cb.failure_count == 1
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_flaky_then_success_records_no_failure(self):
        func = AsyncMock(side_effect=[httpx.ConnectError("x"), httpx.ConnectError("y"), "ok"])
        assert await retry_with_backoff(func, max_retries=3, initial_delay=0, circuit_breaker_name="cb_flaky") == "ok"
        assert get_circuit_breaker("cb_flaky").failure_count == 0

    @pytest.mark.asyncio
    async def test_opens_after_threshold_logical_calls(self):
        func = AsyncMock(side_effect=httpx.ConnectError("down"))
        cb = get_circuit_breaker("cb_threshold")
        for _ in range(cb.failure_threshold - 1):
            with pytest.raises(httpx.ConnectError):
                await retry_with_backoff(func, max_retries=2, initial_delay=0, circuit_breaker_name="cb_threshold")
        assert cb.state == CircuitState.CLOSED
        with pytest.raises(httpx.ConnectError):
            await retry_with_backoff(func, max_retries=2, initial_delay=0, circuit_breaker_name="cb_threshold")
        assert cb.state == CircuitState.OPEN

    @pytest.mark.parametrize("status,raises", [(500, True), (503, True), (429, True), (404, False), (400, False), (200, False)])
    def test_retryable_status_classification(self, status, raises):
        resp = MagicMock(status_code=status, url="https://api.sleeper.app/x")
        if raises:
            with pytest.raises(RetryableHTTPStatus):
                raise_for_retryable_status(resp)
        else:
            raise_for_retryable_status(resp)

    @staticmethod
    def _client_returning(status):
        resp = MagicMock(status_code=status, url="https://api.sleeper.app/v1/stats")
        resp.json.return_value = {}
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        return client

    @pytest.mark.asyncio
    async def test_snaps_5xx_is_a_breaker_failure(self, monkeypatch):
        from nfl_mcp import sleeper_enrichment

        monkeypatch.setattr(sleeper_enrichment, "ADVANCED_ENRICH_ENABLED", True)
        monkeypatch.setenv("NFL_MCP_RETRY_INITIAL_DELAY", "0")
        monkeypatch.setenv("NFL_MCP_MAX_RETRIES", "1")
        client = self._client_returning(503)
        with patch.object(sleeper_enrichment, "create_http_client", return_value=client):
            assert await sleeper_enrichment._fetch_week_player_snaps(2026, 3) == []
        assert client.get.await_count == 2  # retried
        assert get_circuit_breaker("sleeper_snaps").failure_count == 1

    @pytest.mark.asyncio
    async def test_snaps_404_is_not_a_failure(self, monkeypatch):
        from nfl_mcp import sleeper_enrichment

        monkeypatch.setattr(sleeper_enrichment, "ADVANCED_ENRICH_ENABLED", True)
        client = self._client_returning(404)
        with patch.object(sleeper_enrichment, "create_http_client", return_value=client):
            assert await sleeper_enrichment._fetch_week_player_snaps(2026, 3) == []
        assert client.get.await_count == 1
        assert get_circuit_breaker("sleeper_snaps").failure_count == 0


# ---------------------------------------------------------------------------
# Outbound rate limiting
# ---------------------------------------------------------------------------

class TestOutboundRateLimits:
    @pytest.mark.parametrize("host,name", [
        ("api.sleeper.app", "sleeper"),
        ("site.api.espn.com", "espn"),
        ("sports.core.api.espn.com", "espn"),
        ("api.the-odds-api.com", "odds_api"),
        ("github.com", "nflverse"),
        ("release-assets.githubusercontent.com", "nflverse"),
        ("api.fantasycalc.com", "fantasycalc"),
        ("example.com", None),
        ("notespn.com", None),
    ])
    def test_host_mapping(self, host, name):
        assert config.rate_limiter_name_for_host(host) == name

    def test_sleeper_default_is_under_documented_limit(self, monkeypatch):
        monkeypatch.delenv("NFL_MCP_SLEEPER_RATE_LIMIT", raising=False)
        monkeypatch.setattr(config, "_rate_limiters", {})
        limiter = config.get_rate_limiter("sleeper")
        assert 0 < limiter.rate * 60 < 1000

    @pytest.mark.asyncio
    async def test_client_requests_acquire_the_host_limiter(self, monkeypatch):
        monkeypatch.setattr(config, "_rate_limiters", {})
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        async with config.create_http_client(transport=transport) as client:
            await client.get("https://api.sleeper.app/v1/state/nfl")
            await client.get("https://api.sleeper.app/v1/state/nfl")
            await client.get("https://example.com/")
        assert set(config._rate_limiters) == {"sleeper"}
        status = config._rate_limiters["sleeper"].get_status()
        assert status["available_tokens"] <= status["capacity"] - 2 + 0.5

    def test_unused_inbound_rate_limit_helpers_are_gone(self):
        assert not hasattr(config, "check_rate_limit")
        assert not hasattr(config, "RATE_LIMITS")


# ---------------------------------------------------------------------------
# Config file overrides take effect at use time
# ---------------------------------------------------------------------------

class TestConfigFileOverride:
    @pytest.fixture
    def restore_manager(self, monkeypatch):
        for var in ("NFL_MCP_API_TIMEOUT", "NFL_MCP_API_LONG_TIMEOUT", "NFL_MCP_NFL_NEWS_MAX",
                    "NFL_MCP_TIMEOUT_TOTAL", "NFL_MCP_CONFIG_FILE"):
            monkeypatch.delenv(var, raising=False)
        yield
        config_manager.set_config_manager(config_manager.ConfigManager(enable_hot_reload=False))

    def test_file_override_of_limit_and_timeout(self, tmp_path, restore_manager):
        cfg = tmp_path / "config.yml"
        cfg.write_text(yaml.safe_dump({
            "limits": {"nfl_news_max": 7, "week_max": 18},
            "timeout": {"total": 12.0, "connect": 4.0},
        }))
        limits_obj, timeout_obj = config.LIMITS, config.DEFAULT_TIMEOUT

        config_manager.set_config_manager(config_manager.ConfigManager(cfg, enable_hot_reload=False))

        # Updated IN PLACE: a reference taken before the manager was installed
        # (what `from .config import LIMITS` in every tool module holds) sees it.
        assert limits_obj["nfl_news_max"] == 7
        assert limits_obj["week_max"] == 18
        assert timeout_obj.read == 12.0
        assert timeout_obj.connect == 4.0
        assert config.create_http_client().timeout.read == 12.0

    def test_hot_reload_propagates(self, tmp_path, restore_manager):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"limits": {"nfl_news_max": 9}}))
        cm = config_manager.ConfigManager(cfg, enable_hot_reload=False)
        config_manager.set_config_manager(cm)
        assert config.LIMITS["nfl_news_max"] == 9

        cfg.write_text(json.dumps({"limits": {"nfl_news_max": 11}}))
        cm.reload_configuration()
        assert config.LIMITS["nfl_news_max"] == 11

    def test_env_config_file_is_honored_by_default_manager(self, tmp_path, monkeypatch, restore_manager):
        cfg = tmp_path / "config.yml"
        cfg.write_text(yaml.safe_dump({"limits": {"nfl_news_max": 5}}))
        monkeypatch.setenv("NFL_MCP_CONFIG_FILE", str(cfg))
        monkeypatch.setattr(config_manager, "_config_manager", None)
        cm = config_manager.get_config_manager()
        assert cm.config_file_path == cfg.resolve()
        assert cm.config.limits.nfl_news_max == 5

    def test_hot_reload_matches_relative_config_path(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yml"
        cfg.write_text("limits: {nfl_news_max: 3}\n")
        monkeypatch.chdir(tmp_path)
        cm = config_manager.ConfigManager("config.yml", enable_hot_reload=False)
        cm.reload_configuration = MagicMock()
        handler = config_manager.ConfigFileHandler(cm)

        handler.on_modified(MagicMock(is_directory=False, src_path=str(cfg.resolve())))
        handler.on_modified(MagicMock(is_directory=False, src_path=str(tmp_path / "other.yml")))
        assert cm.reload_configuration.call_count == 1


# ---------------------------------------------------------------------------
# Server: startup is non-blocking; .env only in main()
# ---------------------------------------------------------------------------

class TestServerStartup:
    @pytest.mark.asyncio
    async def test_lifespan_does_not_wait_for_startup_prefetch(self, monkeypatch):
        from nfl_mcp import server

        started = asyncio.Event()

        async def slow_warmup(nfl_db, shutdown_event):
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(server, "_startup_warmup", slow_warmup)
        monkeypatch.setattr(server, "_STARTUP_TASK_SHUTDOWN_GRACE_SECONDS", 0.05)
        monkeypatch.setattr(server, "_prefetch_task", None)
        monkeypatch.setattr(server, "_shutdown_event", None)

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with server._create_prefetch_lifespan(MagicMock())(MagicMock()):
            entered = loop.time() - t0
            await asyncio.wait_for(started.wait(), 1)
        assert entered < 0.5
        assert server._prefetch_task.cancelled()

    def test_import_does_not_read_dotenv(self):
        """No module-level statement in server.py calls ``_load_dotenv``:
        importing the package must never pick up a developer's local .env."""
        import ast

        from nfl_mcp import server

        tree = ast.parse(Path(server.__file__).read_text())
        offenders = [
            node.lineno
            for stmt in tree.body
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            for node in ast.walk(stmt)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_load_dotenv"
        ]
        assert offenders == []

    def test_main_loads_dotenv_then_rederives_settings(self, monkeypatch):
        from nfl_mcp import server

        calls = []
        monkeypatch.setattr(server, "_load_dotenv", lambda: calls.append("dotenv") or 0)
        monkeypatch.setattr(server, "_load_runtime_settings", lambda: calls.append("settings"))
        app = MagicMock()
        monkeypatch.setattr(server, "create_app", lambda: calls.append("app") or app)
        monkeypatch.setattr(server, "_port_in_use", lambda host, port: False)
        monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append("run"))
        server.main()
        assert calls == ["dotenv", "settings", "app", "run"]


# ---------------------------------------------------------------------------
# Input warnings surface on tool responses; /metrics
# ---------------------------------------------------------------------------

class TestInputWarningsAndMetrics:
    @pytest.mark.asyncio
    async def test_clamped_limit_is_reported_on_the_response(self):
        from nfl_mcp.metrics import timing_decorator

        @timing_decorator("test_clamp_tool", tool_type="test")
        async def tool(limit=None):
            limit = config.validate_limit(limit, 1, 100, 25)
            return {"success": True, "limit": limit}

        clamped = await tool(limit=500)
        assert clamped["limit"] == 100
        assert clamped["input_warnings"] == ["Value 500 exceeds maximum 100; clamped to 100"]
        assert "input_warnings" not in await tool(limit=10)

    def test_metrics_route_opt_in(self, monkeypatch):
        from starlette.testclient import TestClient

        from nfl_mcp.server import create_app

        monkeypatch.delenv("NFL_MCP_METRICS", raising=False)
        assert TestClient(create_app().http_app(path="/mcp")).get("/metrics").status_code == 404

        monkeypatch.setenv("NFL_MCP_METRICS", "1")
        resp = TestClient(create_app().http_app(path="/mcp")).get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# crawl_url: byte cap, content types, DNS pinning
# ---------------------------------------------------------------------------

def _stream_response(status=200, content_type="text/html", chunks=(b"<html></html>",)):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": content_type} if content_type else {}
    resp.charset_encoding = "utf-8"
    resp.raise_for_status = MagicMock()

    async def _aiter():
        for c in chunks:
            yield c

    resp.aiter_raw = _aiter
    resp.aclose = AsyncMock()
    return resp


def _client_for(response):
    client = MagicMock()
    client.build_request = MagicMock(side_effect=lambda method, url, **kw: url)
    client.send = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


_ALLOW = AsyncMock(return_value=(True, None, "example.com", ["93.184.216.34"]))


class TestCrawlLimits:
    @pytest.mark.asyncio
    async def test_body_is_capped(self, monkeypatch):
        from nfl_mcp import web_tools

        monkeypatch.setenv("NFL_MCP_CRAWL_MAX_BYTES", "1000")
        chunks = [b"<html><body>"] + [b"a" * 400] * 50  # ~20 KB, endless-ish
        resp = _stream_response(chunks=chunks)
        with patch.object(web_tools, "resolve_safe_url", _ALLOW), \
                patch.object(web_tools, "create_http_client", return_value=_client_for(resp)):
            result = await web_tools.crawl_url("https://example.com", max_length=100000)
        assert result["success"] is True
        assert result["truncated_bytes"] is True
        assert result["content_length"] <= 1000
        resp.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_binary_content_type_is_refused(self):
        from nfl_mcp import web_tools

        resp = _stream_response(content_type="application/octet-stream", chunks=[b"\x00" * 10])
        with patch.object(web_tools, "resolve_safe_url", _ALLOW), \
                patch.object(web_tools, "create_http_client", return_value=_client_for(resp)):
            result = await web_tools.crawl_url("https://example.com/file.bin")
        assert result["success"] is False
        assert "Unsupported content type" in result["error"]

    @pytest.mark.asyncio
    async def test_json_is_returned_as_text(self):
        from nfl_mcp import web_tools

        resp = _stream_response(content_type="application/json; charset=utf-8", chunks=[b'{"a": 1}'])
        with patch.object(web_tools, "resolve_safe_url", _ALLOW), \
                patch.object(web_tools, "create_http_client", return_value=_client_for(resp)):
            result = await web_tools.crawl_url("https://example.com/data.json")
        assert result["success"] is True
        assert result["content"] == '{"a": 1}'

    @pytest.mark.asyncio
    async def test_resolution_uses_the_event_loop_resolver(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("blocking getaddrinfo")), \
                patch.object(config, "resolve_host_addresses_async", AsyncMock(return_value=["10.1.2.3"])):
            ok, reason, _host, addrs = await config.resolve_safe_url("http://intranet.example.test/")
        assert ok is False and "Blocked non-public address" in reason and addrs == []


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host")
        body = f"<html><title>pinned</title><body>host={host}</body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestCrawlDnsPinning:
    @pytest.fixture
    def local_server(self):
        httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield httpd.server_address[1]
        httpd.shutdown()

    @pytest.mark.asyncio
    async def test_connects_to_the_validated_ip_with_original_host_header(self, local_server, monkeypatch):
        """``pinned.invalid`` never resolves: the fetch only works because the
        connection goes to the address validated earlier (127.0.0.1 here,
        allowed via a patched range check), while Host stays the hostname."""
        from nfl_mcp import web_tools

        monkeypatch.delenv("NFL_MCP_ALLOW_PRIVATE_URLS", raising=False)
        monkeypatch.setattr(config, "resolve_host_addresses_async", AsyncMock(return_value=["127.0.0.1"]))
        monkeypatch.setattr(config, "_ip_is_disallowed", lambda ip: False)
        monkeypatch.setattr(config, "ALLOWED_URL_PORTS", frozenset({80, 443, local_server}))
        result = await web_tools.crawl_url(f"http://pinned.invalid:{local_server}/")
        assert result["success"] is True, result
        assert result["title"] == "pinned"
        assert f"host=pinned.invalid:{local_server}" in result["content"]

    @pytest.mark.asyncio
    async def test_backend_refuses_unvalidated_hosts(self, monkeypatch):
        from nfl_mcp import web_tools

        monkeypatch.delenv("NFL_MCP_ALLOW_PRIVATE_URLS", raising=False)
        inner = MagicMock()
        inner.connect_tcp = AsyncMock(return_value="stream")
        backend = web_tools._PinnedDNSBackend(inner)
        backend.pin("example.com", ["93.184.216.34"])

        assert await backend.connect_tcp("example.com", 443) == "stream"
        assert inner.connect_tcp.await_args.args[0] == "93.184.216.34"
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("rebound.example.net", 443)


# ---------------------------------------------------------------------------
# Agent eval: missing key is SKIPPED and non-zero
# ---------------------------------------------------------------------------

class TestAgentEvalSkip:
    def test_missing_key_exits_non_zero(self, monkeypatch, capsys):
        from evals.agent import run

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert run.main([]) == run.EXIT_SKIPPED != 0
        assert "SKIPPED" in capsys.readouterr().out
        assert run.main(["--allow-skip"]) == 0


# ---------------------------------------------------------------------------
# Injury list 304 reuses the cached refs
# ---------------------------------------------------------------------------

class TestInjuryConditionalRequests:
    @pytest.mark.asyncio
    async def test_304_reuses_cached_injury_refs(self):
        from nfl_mcp.injury_service import InjuryAggregator, InjuryReport

        InjuryAggregator.clear_caches()
        first = MagicMock(status_code=200, headers={"ETag": '"v1"'})
        first.json.return_value = {"pageCount": 1, "items": [{"$ref": "https://x/inj/1"}]}
        not_modified = MagicMock(status_code=304, headers={})
        client = MagicMock()
        client.get = AsyncMock(side_effect=[first, not_modified])
        agg = InjuryAggregator(http_client=client)
        detail = InjuryReport(player_id="1", player_name="Josh Allen", team_id="BUF")
        try:
            with patch.object(agg, "_fetch_espn_injury_detail", AsyncMock(return_value=detail)):
                fresh = await agg._fetch_team_espn_injuries("BUF", {})
                cached = await agg._fetch_team_espn_injuries("BUF", {})
        finally:
            InjuryAggregator.clear_caches()

        assert [i.player_name for i in fresh] == ["Josh Allen"]
        assert [i.player_name for i in cached] == ["Josh Allen"]
        # The second list request was conditional and answered 304.
        assert client.get.await_args_list[1].kwargs["headers"].get("If-None-Match") == '"v1"'
