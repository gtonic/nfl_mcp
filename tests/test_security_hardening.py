"""Security hardening: HTTP auth/Host/Origin, log redaction, crawl limits,
ESPN ref allowlist, snapshot dedup, upstream rate limits."""
import asyncio
import gzip
import logging
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from nfl_mcp import config, server
from nfl_mcp.log_redaction import SecretRedactionFilter, install_log_redaction, redact

_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


@pytest.fixture
def http_client(monkeypatch):
    async def _no_warmup(*_a, **_k):
        return None

    monkeypatch.setattr(server, "_startup_warmup", _no_warmup)

    def _make(token=None, hosts=None):
        if token:
            monkeypatch.setenv("NFL_MCP_AUTH_TOKEN", token)
        else:
            monkeypatch.delenv("NFL_MCP_AUTH_TOKEN", raising=False)
        if hosts:
            monkeypatch.setenv("NFL_MCP_ALLOWED_HOSTS", hosts)
        else:
            monkeypatch.delenv("NFL_MCP_ALLOWED_HOSTS", raising=False)
        app = server.build_http_app(server.create_app())
        return TestClient(app, base_url="http://localhost:9000")

    return _make


class TestHttpTransport:
    def test_default_bind_is_loopback(self, monkeypatch):
        monkeypatch.delenv("NFL_MCP_HOST", raising=False)
        seen = {}
        monkeypatch.setattr(server, "_port_in_use", lambda h, p: seen.setdefault("host", h) and False)
        with patch("uvicorn.run") as run, patch.object(server, "_load_dotenv", return_value=0):
            server.main()
        assert run.call_args.kwargs["host"] == "127.0.0.1"

    def test_no_token_claude_code_style_request_works(self, http_client):
        with http_client() as c:
            r = c.post("/mcp", json=_LIST, headers=_MCP_HEADERS)
            assert r.status_code == 200
            assert "get_nfl_news" in r.text
            health = c.get("/health").json()
            assert "database" in health  # full details without auth configured

    def test_token_required_on_mcp(self, http_client):
        with http_client(token="s3cret-token") as c:
            assert c.post("/mcp", json=_LIST, headers=_MCP_HEADERS).status_code == 401
            bad = {**_MCP_HEADERS, "Authorization": "Bearer wrong"}
            assert c.post("/mcp", json=_LIST, headers=bad).status_code == 401
            ok = {**_MCP_HEADERS, "Authorization": "Bearer s3cret-token"}
            r = c.post("/mcp", json=_LIST, headers=ok)
            assert r.status_code == 200 and "get_nfl_news" in r.text

    def test_health_trimmed_without_token(self, http_client):
        with http_client(token="s3cret-token") as c:
            r = c.get("/health")
            assert r.status_code == 200
            assert set(r.json()) == {"status", "service", "version"}
            full = c.get("/health", headers={"Authorization": "Bearer s3cret-token"}).json()
            assert "database" in full and "circuit_breakers" in full

    def test_foreign_host_rejected(self, http_client):
        with http_client() as c:
            r = c.post("/mcp", json=_LIST, headers={**_MCP_HEADERS, "Host": "evil.example"})
            assert r.status_code == 421
            assert c.get("/health", headers={"Host": "localhost:9000"}).status_code == 200

    def test_extra_allowed_host(self, http_client):
        with http_client(hosts="nfl-mcp,*.lan") as c:
            for host in ("nfl-mcp:9000", "box.lan:9000"):
                assert c.get("/health", headers={"Host": host}).status_code == 200
            assert c.get("/health", headers={"Host": "other:9000"}).status_code == 421

    def test_foreign_origin_rejected(self, http_client):
        with http_client() as c:
            r = c.post("/mcp", json=_LIST,
                       headers={**_MCP_HEADERS, "Origin": "https://evil.example"})
            assert r.status_code == 403
            r = c.post("/mcp", json=_LIST,
                       headers={**_MCP_HEADERS, "Origin": "http://localhost:3000"})
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_verifier_constant_time_compare(self):
        from nfl_mcp.http_security import StaticBearerTokenVerifier

        v = StaticBearerTokenVerifier("abc")
        with patch("nfl_mcp.http_security.hmac.compare_digest", wraps=__import__("hmac").compare_digest) as cd:
            assert await v.verify_token("abc") is not None
            assert await v.verify_token("abd") is None
        assert cd.call_count == 2


class TestLogRedaction:
    def test_patterns(self):
        assert redact("GET https://x/odds?apiKey=SECRET123&regions=us") == \
            "GET https://x/odds?apiKey=***&regions=us"
        assert "SECRET" not in redact("url?api_key=SECRET")
        assert redact("Authorization: Bearer abc.def") == "Authorization: Bearer ***"

    def test_filter_masks_args_and_exceptions(self):
        records = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        log = logging.getLogger("test.redaction")
        handler = _H()
        handler.addFilter(SecretRedactionFilter())
        log.addHandler(handler)
        log.propagate = False
        try:
            log.warning("HTTP Request: GET %s", "https://api/odds?apiKey=TOPSECRET&x=1")
            try:
                raise RuntimeError("boom https://api/odds?apiKey=TOPSECRET")
            except RuntimeError:
                log.exception("failed")
        finally:
            log.removeHandler(handler)
        assert records and all("TOPSECRET" not in r for r in records)
        assert "apiKey=***" in records[0]

    def test_install_quiets_httpx(self, monkeypatch):
        monkeypatch.delenv("NFL_MCP_HTTPX_LOG_LEVEL", raising=False)
        logging.getLogger("httpx").setLevel(logging.INFO)
        install_log_redaction()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert all(any(isinstance(f, SecretRedactionFilter) for f in h.filters)
                   for h in logging.getLogger().handlers)


def _raw_response(chunks, encoding=None, content_type="text/html"):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": content_type}
    if encoding:
        resp.headers["content-encoding"] = encoding
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
    client.build_request = MagicMock(side_effect=lambda method, url, **kw: (url, kw))
    client.send = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


_ALLOW = AsyncMock(return_value=(True, None, "example.com", ["93.184.216.34"]))


class TestCrawlHardening:
    @pytest.mark.asyncio
    async def test_gzip_bomb_is_capped(self, monkeypatch):
        from nfl_mcp import web_tools

        monkeypatch.setenv("NFL_MCP_CRAWL_MAX_BYTES", "4096")
        bomb = gzip.compress(b"a" * (64 * 1024 * 1024))  # ~64 KB -> 64 MB
        resp = _raw_response([bomb], encoding="gzip")
        seen = []
        real = zlib.decompressobj

        def _spy(*a, **k):
            d = real(*a, **k)
            seen.append(d)
            return d

        with patch.object(web_tools, "resolve_safe_url", _ALLOW), \
                patch.object(web_tools.zlib, "decompressobj", _spy), \
                patch.object(web_tools, "create_http_client", return_value=_client_for(resp)):
            result = await web_tools.crawl_url("https://example.com", max_length=100000)
        assert result["success"] is True
        assert result["truncated_bytes"] is True
        assert result["content_length"] <= 4096
        assert seen  # inflated by our bounded decoder

    @pytest.mark.asyncio
    async def test_small_gzip_body_decoded(self):
        from nfl_mcp import web_tools

        body = gzip.compress(b"<html><title>T</title><body>hello</body></html>")
        resp = _raw_response([body[:10], body[10:]], encoding="gzip")
        client = _client_for(resp)
        with patch.object(web_tools, "resolve_safe_url", _ALLOW), \
                patch.object(web_tools, "create_http_client", return_value=client):
            result = await web_tools.crawl_url("https://example.com")
        assert result["success"] is True and result["title"] == "T"
        assert result["truncated_bytes"] is False
        sent_headers = client.build_request.call_args.kwargs["headers"]
        assert sent_headers["Accept-Encoding"] == "identity"

    @pytest.mark.asyncio
    async def test_unknown_encoding_refused(self):
        from nfl_mcp import web_tools

        resp = _raw_response([b"xx"], encoding="br")
        with patch.object(web_tools, "resolve_safe_url", _ALLOW), \
                patch.object(web_tools, "create_http_client", return_value=_client_for(resp)):
            result = await web_tools.crawl_url("https://example.com")
        assert result["success"] is False and "encoding" in result["error"]

    @pytest.mark.asyncio
    async def test_total_timeout(self, monkeypatch):
        from nfl_mcp import web_tools

        monkeypatch.setattr(web_tools, "CRAWL_TOTAL_TIMEOUT_SECONDS", 0.05)
        client = _client_for(None)

        async def _slow(*_a, **_k):
            await asyncio.sleep(5)

        client.send = AsyncMock(side_effect=_slow)
        with patch.object(web_tools, "resolve_safe_url", _ALLOW), \
                patch.object(web_tools, "create_http_client", return_value=client):
            result = await web_tools.crawl_url("https://example.com")
        assert result["success"] is False and result["error_type"] == "timeout_error"

    @pytest.mark.parametrize("url", ["http://93.184.216.34:6379/", "https://93.184.216.34:8443/x"])
    def test_non_web_ports_blocked(self, url, monkeypatch):
        monkeypatch.delenv("NFL_MCP_ALLOW_PRIVATE_URLS", raising=False)
        ok, reason = config.is_safe_public_url(url)
        assert ok is False and "Blocked port" in reason
        ok, reason, _h, _a = asyncio.run(config.resolve_safe_url(url))
        assert ok is False and "Blocked port" in reason

    @pytest.mark.parametrize("url", ["http://93.184.216.34:80/", "https://93.184.216.34:443/"])
    def test_web_ports_allowed(self, url):
        assert config.is_safe_public_url(url) == (True, None)


class TestEspnRefs:
    @pytest.mark.parametrize("ref,expected", [
        ("http://sports.core.api.espn.com/v2/x?lang=en",
         "https://sports.core.api.espn.com/v2/x?lang=en"),
        ("https://espn.com/a", "https://espn.com/a"),
        ("http://evil.example/v2/x", None),
        ("http://espn.com.evil.example/x", None),
        ("http://sports.core.api.espn.com:8080/x", None),
        ("ftp://espn.com/x", None),
        (None, None),
    ])
    def test_safe_espn_ref(self, ref, expected):
        assert config.safe_espn_ref(ref) == expected

    @pytest.mark.asyncio
    async def test_injury_ref_off_espn_not_fetched(self):
        from nfl_mcp.nfl_tools import get_team_injuries

        listing = MagicMock(status_code=200)
        listing.json.return_value = {"items": [{"$ref": "http://169.254.169.254/latest"}]}
        listing.raise_for_status = MagicMock()
        client = AsyncMock()
        client.get = AsyncMock(return_value=listing)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("nfl_mcp.nfl_tools.create_http_client", return_value=client):
            await get_team_injuries("KC")
        fetched = [c.args[0] for c in client.get.await_args_list]
        assert not any("169.254" in u for u in fetched)


class TestSnapshotDedup:
    def _count(self, db, table):
        with db._pool.get_connection() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_identical_payload_not_duplicated(self, tmp_path):
        from nfl_mcp.database import NFLDatabase

        db = NFLDatabase(str(tmp_path / "s.db"))
        for _ in range(5):
            db.save_roster_snapshot("L1", [{"roster_id": 1}])
            db.save_matchup_snapshot("L1", 3, [{"m": 1}])
        assert self._count(db, "roster_snapshots") == 1
        assert self._count(db, "matchup_snapshots") == 1
        snap = db.load_roster_snapshot("L1")
        assert snap["rosters"] == [{"roster_id": 1}] and snap["stale"] is False

    def test_changed_payload_keeps_latest_n(self, tmp_path):
        from nfl_mcp.database import NFLDatabase

        db = NFLDatabase(str(tmp_path / "s.db"))
        for i in range(10):
            db.save_roster_snapshot("L1", [{"v": i}])
        db.save_roster_snapshot("L2", [{"v": 0}])
        assert self._count(db, "roster_snapshots") == NFLDatabase.SNAPSHOT_ROWS_PER_KEY + 1
        assert db.load_roster_snapshot("L1")["rosters"] == [{"v": 9}]

    def test_migration_prunes_to_newest_per_key(self, tmp_path):
        from nfl_mcp.database import NFLDatabase

        path = str(tmp_path / "m.db")
        db = NFLDatabase(path)
        with db._pool.get_connection() as conn:
            for i in range(4):
                conn.execute(
                    "INSERT INTO roster_snapshots (league_id, payload_json, fetched_at) VALUES (?,?,?)",
                    ("L1", f'[{i}]', f"2026-09-2{i}T00:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO matchup_snapshots (league_id, week, payload_json, fetched_at) VALUES (?,?,?,?)",
                    ("L1", 1 + i % 2, f'[{i}]', f"2026-09-2{i}T00:00:00+00:00"),
                )
            conn.execute("DELETE FROM schema_version WHERE version = 16")
            conn.commit()
        db.close()
        again = NFLDatabase(path)
        assert self._count(again, "roster_snapshots") == 1
        assert self._count(again, "matchup_snapshots") == 2
        assert again.load_roster_snapshot("L1")["rosters"] == [3]

    def test_journal_size_limit_set(self, tmp_path):
        from nfl_mcp.database import NFLDatabase

        db = NFLDatabase(str(tmp_path / "j.db"))
        with db._pool.get_connection() as conn:
            assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 16777216


class TestUpstreamRateLimits:
    @pytest.mark.asyncio
    async def test_fetch_all_players_force_refresh_limited(self):
        from nfl_mcp import sleeper_tools

        sleeper_tools._PLAYERS_CACHE["data"] = {"1": {}}
        sleeper_tools._PLAYERS_CACHE["fetched_at"] = __import__("time").time() - 60
        try:
            with patch.object(sleeper_tools, "create_http_client") as factory:
                out = await sleeper_tools.fetch_all_players(force_refresh=True)
            factory.assert_not_called()
            assert out["cached"] is True and "force_refresh ignored" in out["note"]
        finally:
            sleeper_tools._PLAYERS_CACHE["data"] = None
            sleeper_tools._PLAYERS_CACHE["fetched_at"] = 0

    @pytest.mark.asyncio
    async def test_fetch_athletes_limited_when_fresh(self):
        from datetime import UTC, datetime

        from nfl_mcp import tool_registry

        db = MagicMock()
        db.get_last_updated.return_value = datetime.now(UTC).isoformat()
        db.get_athlete_count.return_value = 3000
        with patch.object(tool_registry, "get_db", return_value=db), \
                patch.object(tool_registry.athlete_tools, "fetch_athletes", AsyncMock()) as fetch:
            out = await tool_registry.fetch_athletes()
        fetch.assert_not_called()
        assert out["cached"] is True and out["athletes_count"] == 3000

    @pytest.mark.asyncio
    async def test_fetch_athletes_runs_when_stale(self):
        from nfl_mcp import tool_registry

        db = MagicMock()
        db.get_last_updated.return_value = "2020-01-01T00:00:00+00:00"
        with patch.object(tool_registry, "get_db", return_value=db), \
                patch.object(tool_registry.athlete_tools, "fetch_athletes",
                             AsyncMock(return_value={"success": True})) as fetch:
            await tool_registry.fetch_athletes()
        fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uncached_injury_crawl_limited(self, monkeypatch):
        from nfl_mcp import injury_service, tool_registry

        tool_registry._last_uncached_injury_crawl.clear()
        calls = []

        async def _reports(teams=None, db=None, use_cache=True):
            calls.append(use_cache)
            return []

        monkeypatch.setattr(injury_service, "get_injury_reports", _reports)
        first = await tool_registry.get_injury_report(use_cache=False)
        second = await tool_registry.get_injury_report(use_cache=False)
        tool_registry._last_uncached_injury_crawl.clear()
        assert calls == [False, True]
        assert "note" not in first and "use_cache=False ignored" in second["note"]

    @pytest.mark.asyncio
    async def test_vegas_concurrent_calls_fetch_once(self):
        from nfl_mcp.vegas_tools import VegasLinesAnalyzer

        analyzer = VegasLinesAnalyzer(api_key="k")
        calls = 0

        async def _fetch(include_live):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            from datetime import UTC, datetime
            analyzer._lines_cache = {"KC@BUF": {}}
            analyzer._cache_time = datetime.now(UTC)
            return analyzer._lines_cache

        analyzer._fetch_lines_from_api = _fetch
        results = await asyncio.gather(*(analyzer.fetch_current_lines() for _ in range(5)))
        assert calls == 1
        assert all(r == {"KC@BUF": {}} for r in results)
