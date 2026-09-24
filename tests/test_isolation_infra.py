"""Test isolation, the shared DB accessor and assorted infrastructure fixes."""
import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nfl_mcp import config, config_manager, database, health
from nfl_mcp.database import ConnectionPoolConfig, DatabaseConnectionPool, NFLDatabase

# ---------------------------------------------------------------------------
# The suite never touches ./nfl_data.db
# ---------------------------------------------------------------------------


class TestIsolatedDatabase:
    def test_default_path_is_the_per_test_temp_file(self, tmp_path):
        db = NFLDatabase()
        try:
            assert db.db_path == tmp_path / "nfl_data.db"
        finally:
            db.close()

    def test_shared_db_follows_the_configured_path(self, tmp_path):
        assert database.get_shared_db().db_path == tmp_path / "nfl_data.db"


class TestSharedDb:
    def test_one_instance_per_path(self):
        assert database.get_shared_db() is database.get_shared_db()

    def test_rebuilt_when_the_path_changes(self, tmp_path, monkeypatch):
        first = database.get_shared_db()
        monkeypatch.setenv("NFL_MCP_DB_PATH", str(tmp_path / "other.db"))
        second = database.get_shared_db()
        assert second is not first
        assert second.db_path == tmp_path / "other.db"
        first.close()

    def test_the_server_instance_is_what_tools_get(self, tmp_path):
        from nfl_mcp import tool_registry

        saved = tool_registry._db_token.get()
        server_db = NFLDatabase(str(tmp_path / "nfl_data.db"))
        try:
            tool_registry.initialize_shared(server_db)
            assert database.get_shared_db() is server_db
        finally:
            tool_registry._db_token.set(saved)

    def test_concurrent_first_use_builds_one_instance(self):
        seen = []

        def grab():
            seen.append(database.get_shared_db())

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({id(db) for db in seen}) == 1


# ---------------------------------------------------------------------------
# Connection pool accounting
# ---------------------------------------------------------------------------


class TestPoolSlotAccounting:
    def test_failed_reconnect_frees_the_slot(self, tmp_path):
        pool = DatabaseConnectionPool(
            str(tmp_path / "p.db"), ConnectionPoolConfig(max_connections=2, health_check_interval=0)
        )
        before = pool._total_connections
        pool._last_health_check = 0
        with patch.object(pool, "_test_connection", return_value=False), \
                patch.object(pool, "_create_connection", return_value=None), \
                pytest.raises(Exception, match="healthy database connection"), \
                pool.get_connection():
            pass
        assert pool._total_connections == before - 1
        pool.close()


# ---------------------------------------------------------------------------
# /health: robust env parsing, no filesystem path
# ---------------------------------------------------------------------------


class TestHealthPayload:
    def test_bad_interval_env_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("NFL_MCP_PREFETCH_INTERVAL", "15m")
        monkeypatch.setenv("NFL_MCP_PREFETCH_ATHLETES_INTERVAL", "")
        cfg = health._get_prefetch_config()
        assert cfg["interval_seconds"] == 900
        assert cfg["athletes_refresh_interval_seconds"] == 86400

    def test_server_settings_survive_a_bad_value(self, monkeypatch):
        from nfl_mcp import server

        monkeypatch.setenv("NFL_MCP_DB_PRUNE_INTERVAL", "daily")
        server._load_runtime_settings()
        try:
            assert server.DB_PRUNE_INTERVAL_SECONDS == 86400
        finally:
            monkeypatch.delenv("NFL_MCP_DB_PRUNE_INTERVAL")
            server._load_runtime_settings()

    def test_database_path_is_not_exposed(self, tmp_path):
        db = NFLDatabase(str(tmp_path / "h.db"))
        try:
            report = db._pool.health_check()
        finally:
            db.close()
        assert report["healthy"] is True
        assert "database_path" not in report
        assert str(tmp_path) not in json.dumps(report, default=str)


# ---------------------------------------------------------------------------
# Config: atomic hot-reload swap, hot reload opt-in, team_id case
# ---------------------------------------------------------------------------


class TestConfigReload:
    def test_swap_never_leaves_the_dict_empty(self):
        target = {"a": 1, "b": 2, "stale": 3}
        seen_empty = []

        class Watched(dict):
            def pop(self, *a):
                if not self:
                    seen_empty.append(True)
                return super().pop(*a)

        watched = Watched(target)
        config._swap_in_place(watched, {"a": 10, "b": 20})
        assert dict(watched) == {"a": 10, "b": 20}
        assert not seen_empty

    def test_refresh_keeps_the_same_limits_object(self):
        before = config.LIMITS
        config.refresh_from_config_manager()
        assert config.LIMITS is before
        assert "nfl_news_max" in config.LIMITS

    def test_implicit_config_file_does_not_hot_reload_by_default(self, tmp_path, monkeypatch):
        (tmp_path / "config.yml").write_text("limits: {nfl_news_max: 7}\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("NFL_MCP_CONFIG_FILE", raising=False)
        monkeypatch.delenv("NFL_MCP_CONFIG_HOT_RELOAD", raising=False)
        saved = config_manager._config_manager
        monkeypatch.setattr(config_manager, "_config_manager", None)
        with patch.object(config_manager.ConfigManager, "_setup_hot_reload") as setup:
            cm = config_manager.get_config_manager()
        try:
            assert cm.enable_hot_reload is False
            setup.assert_not_called()
            assert cm.config.limits.nfl_news_max == 7
        finally:
            cm.stop()
            config_manager._config_manager = saved

    def test_implicit_config_file_hot_reloads_when_asked(self, tmp_path, monkeypatch):
        (tmp_path / "config.yml").write_text("limits: {nfl_news_max: 7}\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("NFL_MCP_CONFIG_FILE", raising=False)
        monkeypatch.setenv("NFL_MCP_CONFIG_HOT_RELOAD", "1")
        saved = config_manager._config_manager
        monkeypatch.setattr(config_manager, "_config_manager", None)
        with patch.object(config_manager.ConfigManager, "_setup_hot_reload") as setup:
            cm = config_manager.get_config_manager()
        try:
            assert cm.enable_hot_reload is True
            setup.assert_called_once()
        finally:
            cm.stop()
            config_manager._config_manager = saved


class TestTeamIdCase:
    def test_lowercase_team_id_is_accepted(self):
        assert config.validate_string_input("kc", "team_id") == "KC"
        assert config.validate_string_input(" Sf ", "team_id") == "SF"

    def test_garbage_is_still_rejected(self):
        with pytest.raises(ValueError):
            config.validate_string_input("k1", "team_id")

    @pytest.mark.asyncio
    async def test_coaching_tools_accept_lowercase(self):
        from nfl_mcp import coaching_tools, tool_registry

        staff = AsyncMock(return_value={"team_id": "KC", "success": True})
        scheme = AsyncMock(return_value={"team_id": "KC", "success": True})
        with patch.object(coaching_tools, "get_coaching_staff", staff), \
                patch.object(coaching_tools, "get_scheme_classification", scheme):
            assert (await tool_registry.get_coaching_staff(team_id="kc"))["success"] is True
            assert (await tool_registry.get_scheme_classification(team_id="kc"))["success"] is True
        assert staff.await_args.args[0] == "KC"
        assert scheme.await_args.args[0] == "KC"


# ---------------------------------------------------------------------------
# crawl_url: max_length range, parse off the event loop
# ---------------------------------------------------------------------------


def _html_response(body: bytes):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "text/html"}
    resp.charset_encoding = "utf-8"
    resp.raise_for_status = MagicMock()

    async def _aiter():
        yield body

    resp.aiter_raw = _aiter
    resp.aclose = AsyncMock()
    client = MagicMock()
    client.build_request = MagicMock(side_effect=lambda method, url, **kw: url)
    client.send = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


_ALLOW = AsyncMock(return_value=(True, None, "example.com", ["93.184.216.34"]))


class TestCrawlMaxLength:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("asked,cap", [(1, 100), (-5, 100), (10**9, 50000)])
    async def test_out_of_range_is_clamped(self, asked, cap):
        from nfl_mcp import web_tools

        body = b"<html><body>" + b"word " * 20000 + b"</body></html>"
        with patch.object(web_tools, "resolve_safe_url", _ALLOW), \
                patch.object(web_tools, "create_http_client", return_value=_html_response(body)):
            result = await web_tools.crawl_url("https://example.com", max_length=asked)
        assert result["success"] is True
        assert result["content_length"] == cap + len("...")

    @pytest.mark.asyncio
    async def test_html_is_parsed_in_a_worker_thread(self):
        from nfl_mcp import web_tools

        loop_thread = threading.get_ident()
        parsed_on = []
        real = web_tools._extract_text

        def spy(*a):
            parsed_on.append(threading.get_ident())
            return real(*a)

        body = b"<html><title>T</title><body>hi</body></html>"
        with patch.object(web_tools, "resolve_safe_url", _ALLOW), \
                patch.object(web_tools, "create_http_client", return_value=_html_response(body)), \
                patch.object(web_tools, "_extract_text", spy):
            result = await web_tools.crawl_url("https://example.com")
        assert result["title"] == "T" and "hi" in result["content"]
        assert parsed_on and parsed_on[0] != loop_thread


# ---------------------------------------------------------------------------
# Bulk writes run off the event loop
# ---------------------------------------------------------------------------


class TestBulkWritesOffTheLoop:
    @pytest.mark.asyncio
    async def test_athletes_upsert_runs_in_a_thread(self):
        from nfl_mcp import athlete_tools

        loop_thread = threading.get_ident()
        db = MagicMock()
        db.upsert_athletes.side_effect = lambda data: threading.get_ident()
        db.get_last_updated.return_value = "now"
        resp = MagicMock()
        resp.json.return_value = {"1": {"full_name": "A"}}
        resp.raise_for_status = MagicMock()
        client = AsyncMock()
        client.get.return_value = resp
        client.__aenter__.return_value = client
        with patch.object(athlete_tools, "create_http_client", return_value=client):
            result = await athlete_tools.fetch_athletes(db)
        assert result["success"] is True
        assert result["athletes_count"] != loop_thread

    @pytest.mark.asyncio
    async def test_startup_schedule_upsert_runs_in_a_thread(self, monkeypatch):
        from nfl_mcp import server, sleeper_tools

        loop_thread = threading.get_ident()
        wrote_on = []
        db = MagicMock()
        db.upsert_schedule_games.side_effect = lambda rows: wrote_on.append(threading.get_ident()) or 1
        monkeypatch.setattr(server, "PREFETCH_ENABLED", True)
        monkeypatch.setattr(server, "PREFETCH_ATHLETES", False)
        monkeypatch.setattr(server, "_prune_db_if_due", AsyncMock(return_value=False))
        monkeypatch.setattr(server, "_prefetch_loop", AsyncMock())
        monkeypatch.setattr(sleeper_tools, "advanced_enrich_enabled", lambda: True)
        monkeypatch.setattr(sleeper_tools, "get_nfl_state",
                            AsyncMock(return_value={"success": True, "nfl_state": {"season": "2026"}}))
        monkeypatch.setattr(sleeper_tools, "_fetch_all_team_schedules",
                            AsyncMock(return_value=[{"season": 2026, "week": 1, "team": "KC"}]))
        await server._startup_warmup(db, asyncio.Event())
        assert wrote_on and wrote_on[0] != loop_thread


def test_no_stray_database_in_the_working_directory():
    """Running the suite must not create ./nfl_data.db (it used to)."""
    # Every default path in this test points at tmp; the cwd copy, if a
    # developer has one, is theirs — only assert we did not open it.
    assert Path(os.environ["NFL_MCP_DB_PATH"]).name == "nfl_data.db"
    assert Path(os.environ["NFL_MCP_DB_PATH"]).parent != Path.cwd()
