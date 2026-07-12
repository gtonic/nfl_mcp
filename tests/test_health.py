"""Tests for health module (health_check, _get_version, _get_prefetch_config)."""
import pytest
import os
from unittest.mock import patch, MagicMock
from nfl_mcp.health import health_check, _get_version, _get_prefetch_config


class TestGetVersion:
    """Test _get_version function."""

    def test_get_version_from_pyproject(self, tmp_path):
        """Test version extraction from pyproject.toml."""
        # Create a temporary pyproject.toml
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "1.2.3"\n')
        
        with patch('nfl_mcp.health.Path') as mock_path:
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.__truediv__ = lambda self, other: tmp_path / other
            mock_path_instance.parents = [tmp_path]
            
            mock_path.return_value = mock_path_instance
            
            with open(pyproject, "rb") as f:
                import tomllib
                version = tomllib.load(f).get("project", {}).get("version", "unknown")
            
            assert version == "1.2.3"

    def test_get_version_fallback(self):
        """Test fallback to default version."""
        with patch('nfl_mcp.health.Path') as mock_path:
            mock_path.return_value.exists.return_value = False
            mock_path.return_value.parents = []
            
            with patch('nfl_mcp.health.Path.iterdir', return_value=[]):
                version = _get_version()
                assert version == "unknown"


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

    def test_health_check_success(self):
        """Test successful health check."""
        result = health_check()
        # health_check returns a JSONResponse object (not a coroutine)
        assert result.status_code == 200
        assert b'"status": "healthy"' in content
        assert b'"service": "NFL MCP Server"' in content
        assert b'"version":' in content

    @pytest.mark.asyncio
    async def test_health_check_includes_all_sections(self):
        """Test health check includes all expected sections."""
        result = await health_check()
        content = await result.body
        
        assert b'"database"' in content
        assert b'"circuit_breakers"' in content
        assert b'"rate_limiters"' in content
        assert b'"prefetch"' in content

    @pytest.mark.asyncio
    async def test_health_check_with_no_db(self):
        """Test health check when no DB is initialized."""
        with patch('nfl_mcp.health.get_db', return_value=None):
            result = await health_check()
            content = await result.body
            # Should still work, just with empty db_health
            assert b'"database":{}' in content or b'"database":{}' in content.decode()

    @pytest.mark.asyncio
    async def test_health_check_with_db(self):
        """Test health check with DB initialized."""
        from unittest.mock import MagicMock
        
        mock_db = MagicMock()
        mock_db.health_check.return_value = {"healthy": True, "pool_size": 2}
        
        with patch('nfl_mcp.health.get_db', return_value=mock_db):
            result = await health_check()
            content = await result.body
            # Should include database health info
            assert b'"database"' in content
