"""Tests for health module (health_check, _get_version, _get_prefetch_config)."""
import os
from unittest.mock import MagicMock, patch

import pytest

from nfl_mcp.health import _get_prefetch_config, _get_version, health_check


class TestGetVersion:
    """Version: one source of truth, never "unknown" in a normal checkout."""

    def test_version_matches_pyproject(self):
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with open(pyproject, "rb") as f:
            expected = tomllib.load(f)["project"]["version"]
        assert _get_version() == expected

    def test_package_and_config_agree(self):
        import nfl_mcp
        from nfl_mcp import config
        from nfl_mcp.config_manager import ServerConfig

        assert nfl_mcp.__version__ == _get_version()
        assert ServerConfig().version == _get_version()
        assert _get_version() == config.SERVER_VERSION
        assert f"NFL-MCP-Server/{_get_version()} " in config.BASE_USER_AGENT

    def test_metadata_fallback_when_no_pyproject(self):
        from nfl_mcp import _version

        _version.get_version.cache_clear()
        try:
            with patch.object(_version, "_version_from_pyproject", return_value=None), \
                    patch.object(_version, "_version_from_metadata", return_value="9.9.9"):
                assert _version.get_version() == "9.9.9"
        finally:
            _version.get_version.cache_clear()


class TestGetPrefetchConfig:
    """Test _get_prefetch_config function."""

    def test_prefetch_enabled(self):
        """Test prefetch enabled config."""
        with patch.dict(os.environ, {"NFL_MCP_PREFETCH": "1", "NFL_MCP_ADVANCED_ENRICH": "1"}):
            config = _get_prefetch_config()
            assert config["enabled"] is True
            assert config["advanced_enrich_enabled"] is True

    def test_prefetch_disabled(self):
        """Test prefetch disabled config."""
        with patch.dict(os.environ, {"NFL_MCP_PREFETCH": "0", "NFL_MCP_ADVANCED_ENRICH": "0"}):
            config = _get_prefetch_config()
            assert config["enabled"] is False
            assert config["advanced_enrich_enabled"] is False

    def test_prefetch_default_interval(self):
        """Test default prefetch interval."""
        with patch.dict(os.environ, {}, clear=True):
            config = _get_prefetch_config()
            assert config["interval_seconds"] == 900

    def test_prefetch_custom_interval(self):
        """Test custom prefetch interval."""
        with patch.dict(os.environ, {"NFL_MCP_PREFETCH_INTERVAL": "1800"}):
            config = _get_prefetch_config()
            assert config["interval_seconds"] == 1800


class TestHealthCheck:
    """Test health_check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check_success(self):
        """Test successful health check."""
        # Other tests leave breakers open in the process-wide registry.
        with patch('nfl_mcp.retry_utils.get_all_circuit_breaker_status', return_value={}):
            result = await health_check()
        # health_check returns a starlette JSONResponse; .body is bytes.
        content = result.body
        assert result.status_code == 200
        assert b'"status":"healthy"' in content or b'"status": "healthy"' in content
        assert b'NFL MCP Server' in content
        assert b'version' in content

    @pytest.mark.asyncio
    async def test_health_check_includes_all_sections(self):
        """Test health check includes all expected sections."""
        result = await health_check()
        content = result.body

        assert b'database' in content
        assert b'circuit_breakers' in content
        assert b'rate_limiters' in content
        assert b'prefetch' in content

    @pytest.mark.asyncio
    async def test_health_check_with_no_db(self):
        """Test health check when no DB is initialized."""
        # get_db is imported inside health_check from tool_registry.
        with patch('nfl_mcp.tool_registry.get_db', return_value=None):
            result = await health_check()
            content = result.body.decode()
            # Should still work, just with empty db_health
            assert '"database": {}' in content or '"database":{}' in content

    @pytest.mark.asyncio
    async def test_health_check_with_db(self):
        """Test health check with DB initialized."""
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        mock_db.health_check.return_value = {"healthy": True, "pool_size": 2}

        with patch('nfl_mcp.tool_registry.get_db', return_value=mock_db):
            result = await health_check()
            content = result.body
            # Should include database health info
            assert b'database' in content
            assert b'healthy' in content


class TestHealthStatus:
    """Overall status reflects the DB and the circuit breakers."""

    @staticmethod
    def _db(healthy=True, **extra):
        mock_db = MagicMock()
        mock_db.health_check.return_value = {"healthy": healthy, **extra}
        return mock_db

    @pytest.mark.asyncio
    async def test_healthy_db_no_open_breakers(self):
        import json

        with patch('nfl_mcp.tool_registry.get_db', return_value=self._db()), \
                patch('nfl_mcp.retry_utils.get_all_circuit_breaker_status', return_value={}):
            result = await health_check()
        body = json.loads(result.body)
        assert result.status_code == 200
        assert body["status"] == "healthy"
        assert body["version"] != "unknown"

    @pytest.mark.asyncio
    async def test_db_down_is_unhealthy_503(self):
        import json

        db = self._db(healthy=False, error="database is locked")
        with patch('nfl_mcp.tool_registry.get_db', return_value=db):
            result = await health_check()
        assert result.status_code == 503
        assert json.loads(result.body)["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_db_check_raising_is_unhealthy(self):
        db = MagicMock()
        db.health_check.side_effect = RuntimeError("pool exhausted")
        with patch('nfl_mcp.tool_registry.get_db', return_value=db):
            result = await health_check()
        assert result.status_code == 503

    @pytest.mark.asyncio
    async def test_open_breaker_is_degraded_200(self):
        import json

        breakers = {
            "espn_schedule": {"state": "open", "failure_count": 5},
            "sleeper_snaps": {"state": "closed", "failure_count": 0},
        }
        with patch('nfl_mcp.tool_registry.get_db', return_value=self._db()), \
                patch('nfl_mcp.retry_utils.get_all_circuit_breaker_status', return_value=breakers):
            result = await health_check()
        body = json.loads(result.body)
        assert result.status_code == 200
        assert body["status"] == "degraded"
        assert body["open_circuit_breakers"] == ["espn_schedule"]

    @pytest.mark.asyncio
    async def test_db_down_beats_open_breaker(self):
        breakers = {"espn_schedule": {"state": "open"}}
        with patch('nfl_mcp.tool_registry.get_db', return_value=self._db(healthy=False)), \
                patch('nfl_mcp.retry_utils.get_all_circuit_breaker_status', return_value=breakers):
            result = await health_check()
        assert result.status_code == 503
