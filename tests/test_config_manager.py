"""
Tests for the configuration management system.
"""

import os
import json
import yaml
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch
import time

from nfl_mcp.config_manager import (
    ConfigManager, 
    ConfigurationModel, 
    TimeoutConfig, 
    LongTimeoutConfig,
    ServerConfig,
    ValidationLimits,
    RateLimitConfig,
    SecurityConfig,
    get_config_manager,
    set_config_manager
)


class TestConfigurationModels:
    """Test configuration data models."""
    
    def test_timeout_config_defaults(self):
        """Test default timeout configuration."""
        config = TimeoutConfig()
        assert config.total == 30.0
        assert config.connect == 10.0
    
    def test_long_timeout_config_defaults(self):
        """Test default long timeout configuration."""
        config = LongTimeoutConfig()
        assert config.total == 60.0
        assert config.connect == 15.0
    
    def test_server_config_defaults(self):
        """Test default server configuration."""
        config = ServerConfig()
        assert config.version == "0.1.0"
        assert config.base_user_agent == "NFL-MCP-Server/0.1.0"
    
    def test_server_config_custom_version(self):
        """Test server configuration with custom version."""
        config = ServerConfig(version="1.2.3")
        assert config.version == "1.2.3"
        assert config.base_user_agent == "NFL-MCP-Server/1.2.3"
    
    def test_validation_limits_defaults(self):
        """Test default validation limits."""
        limits = ValidationLimits()
        assert limits.nfl_news_max == 50
        assert limits.nfl_news_min == 1
        assert limits.athletes_search_max == 100
        assert limits.week_min == 1
        assert limits.week_max == 22
    
    def test_rate_limit_config_defaults(self):
        """Test default rate limit configuration."""
        config = RateLimitConfig()
        assert config.default_requests_per_minute == 60
        assert config.heavy_requests_per_minute == 10
        assert config.burst_limit == 5
    
    def test_security_config_defaults(self):
        """Test default security configuration."""
        config = SecurityConfig()
        assert config.allowed_url_schemes == ["http://", "https://"]
        assert config.max_string_length == 1000
        assert config.enable_injection_detection is True


class TestConfigurationValidation:
    """Test configuration validation with pydantic."""
    
    def test_valid_configuration(self):
        """Test that valid configuration passes validation."""
        config_data = {
            "timeout": {"total": 45.0, "connect": 15.0},
            "server": {"version": "1.0.0"},
            "limits": {"nfl_news_max": 100}
        }
        config = ConfigurationModel(**config_data)
        assert config.timeout.total == 45.0
        assert config.timeout.connect == 15.0
        assert config.server.version == "1.0.0"
        assert config.limits.nfl_news_max == 100
    
    def test_invalid_configuration_type(self):
        """Test that invalid configuration types fail validation."""
        with pytest.raises(Exception):  # pydantic ValidationError
            ConfigurationModel(timeout={"total": "invalid"})
    
    def test_partial_configuration(self):
        """Test that partial configuration uses defaults."""
        config = ConfigurationModel(server={"version": "2.0.0"})
        assert config.server.version == "2.0.0"
        assert config.timeout.total == 30.0  # Default
        assert config.limits.nfl_news_max == 50  # Default


class TestConfigManager:
    """Test the ConfigManager class."""
    
    def setup_method(self):
        """Set up test environment."""
        # Clear any existing config manager
        self.original_config_manager = None
    
    def teardown_method(self):
        """Clean up test environment."""
        # Clear environment variables
        env_vars_to_clear = [
            'NFL_MCP_TIMEOUT_TOTAL',
            'NFL_MCP_SERVER_VERSION',
            'NFL_MCP_NFL_NEWS_MAX',
            'NFL_MCP_ALLOWED_URL_SCHEMES',
        ]
        for var in env_vars_to_clear:
            if var in os.environ:
                del os.environ[var]
    
    def test_config_manager_defaults(self):
        """Test ConfigManager with default values."""
        manager = ConfigManager()
        config = manager.config
        
        assert config.timeout.total == 30.0
        assert config.server.version == "0.1.0"
        assert config.limits.nfl_news_max == 50
    
    def test_config_manager_environment_variables(self):
        """Test ConfigManager loading from environment variables."""
        # Set environment variables
        os.environ['NFL_MCP_TIMEOUT_TOTAL'] = '45.0'
        os.environ['NFL_MCP_SERVER_VERSION'] = '2.0.0'
        os.environ['NFL_MCP_NFL_NEWS_MAX'] = '75'
        os.environ['NFL_MCP_ALLOWED_URL_SCHEMES'] = 'https://, http://'
        
        manager = ConfigManager()
        config = manager.config
        
        assert config.timeout.total == 45.0
        assert config.server.version == "2.0.0"
        assert config.limits.nfl_news_max == 75
        assert "https://" in config.security.allowed_url_schemes
        assert "http://" in config.security.allowed_url_schemes
    
    def test_config_manager_yaml_file(self):
        """Test ConfigManager loading from YAML file."""
        config_data = {
            'timeout': {'total': 40.0, 'connect': 12.0},
            'server': {'version': '1.5.0'},
            'limits': {'nfl_news_max': 80},
            'security': {'max_string_length': 2000}
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            yaml.dump(config_data, f)
            config_file = f.name
        
        try:
            manager = ConfigManager(config_file=config_file, enable_hot_reload=False)
            config = manager.config
            
            assert config.timeout.total == 40.0
            assert config.timeout.connect == 12.0
            assert config.server.version == "1.5.0"
            assert config.limits.nfl_news_max == 80
            assert config.security.max_string_length == 2000
        finally:
            os.unlink(config_file)
    
    def test_config_manager_json_file(self):
        """Test ConfigManager loading from JSON file."""
        config_data = {
            'timeout': {'total': 35.0},
            'server': {'version': '1.8.0'},
            'rate_limits': {'default_requests_per_minute': 120}
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config_data, f)
            config_file = f.name
        
        try:
            manager = ConfigManager(config_file=config_file, enable_hot_reload=False)
            config = manager.config
            
            assert config.timeout.total == 35.0
            assert config.server.version == "1.8.0"
            assert config.rate_limits.default_requests_per_minute == 120
        finally:
            os.unlink(config_file)
    
    def test_environment_variables_override_config_file(self):
        """Test that environment variables override config file values."""
        # Create config file
        config_data = {
            'timeout': {'total': 40.0},
            'server': {'version': '1.0.0'}
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            yaml.dump(config_data, f)
            config_file = f.name
        
        # Set environment variable that should override
        os.environ['NFL_MCP_TIMEOUT_TOTAL'] = '50.0'
        
        try:
            manager = ConfigManager(config_file=config_file, enable_hot_reload=False)
            config = manager.config
            
            # Environment variable should override file value
            assert config.timeout.total == 50.0
            # File value should be used for non-overridden values
            assert config.server.version == "1.0.0"
        finally:
            os.unlink(config_file)
    
    def test_invalid_config_file_format(self):
        """Test error handling for invalid config file format."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("invalid config")
            config_file = f.name
        
        try:
            with pytest.raises(ValueError, match="Unsupported configuration file format"):
                ConfigManager(config_file=config_file, enable_hot_reload=False)
        finally:
            os.unlink(config_file)
    
    def test_invalid_yaml_file(self):
        """Test error handling for invalid YAML file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write("invalid: yaml: content: [")
            config_file = f.name
        
        try:
            with pytest.raises(ValueError, match="Failed to load configuration file"):
                ConfigManager(config_file=config_file, enable_hot_reload=False)
        finally:
            os.unlink(config_file)
    
    def test_invalid_environment_variable(self):
        """Test error handling for invalid environment variable values."""
        os.environ['NFL_MCP_TIMEOUT_TOTAL'] = 'invalid_number'
        
        with pytest.raises(ValueError, match="Invalid value for environment variable"):
            ConfigManager()
    
    def test_config_manager_helper_methods(self):
        """Test ConfigManager helper methods."""
        manager = ConfigManager()
        
        # Test HTTP timeout methods
        timeout = manager.get_http_timeout()
        # httpx.Timeout uses read/write/pool for total and connect for connection timeout
        assert timeout.connect == 10.0
        assert timeout.read == 30.0  # total timeout is mapped to read timeout
        
        long_timeout = manager.get_long_http_timeout()
        assert long_timeout.connect == 15.0
        assert long_timeout.read == 60.0  # total timeout is mapped to read timeout
        
        # Test user agent methods
        base_agent = manager.get_user_agent()
        assert base_agent == "NFL-MCP-Server/0.1.0"
        
        service_agent = manager.get_user_agent("nfl_news")
        assert service_agent == "NFL-MCP-Server/0.1.0 (NFL News Fetcher)"
        
        # Test dictionary methods for backward compatibility
        limits_dict = manager.get_limits_dict()
        assert isinstance(limits_dict, dict)
        assert limits_dict["nfl_news_max"] == 50
        
        rate_limits_dict = manager.get_rate_limits_dict()
        assert isinstance(rate_limits_dict, dict)
        assert rate_limits_dict["default_requests_per_minute"] == 60


class TestConfigReloading:
    """Test configuration hot-reloading functionality."""
    
    def test_manual_reload(self):
        """Test manual configuration reloading."""
        # Create initial config file
        config_data = {'server': {'version': '1.0.0'}}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            yaml.dump(config_data, f)
            config_file = f.name
        
        try:
            manager = ConfigManager(config_file=config_file, enable_hot_reload=False)
            assert manager.config.server.version == "1.0.0"
            
            # Update config file
            updated_config = {'server': {'version': '2.0.0'}}
            with open(config_file, 'w') as f:
                yaml.dump(updated_config, f)
            
            # Manually reload
            manager.reload_configuration()
            assert manager.config.server.version == "2.0.0"
        finally:
            os.unlink(config_file)
    
    def test_reload_with_invalid_config(self):
        """Test that reload gracefully handles invalid configuration."""
        # Create initial valid config file
        config_data = {'server': {'version': '1.0.0'}}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            yaml.dump(config_data, f)
            config_file = f.name
        
        try:
            manager = ConfigManager(config_file=config_file, enable_hot_reload=False)
            original_version = manager.config.server.version
            
            # Update config file with invalid content
            with open(config_file, 'w') as f:
                f.write("invalid: yaml: content: [")
            
            # Reload should fail gracefully, keeping original config
            manager.reload_configuration()
            assert manager.config.server.version == original_version
        finally:
            os.unlink(config_file)


class TestGlobalConfigManager:
    """Test global configuration manager functions."""
    
    def setup_method(self):
        """Set up test environment."""
        # Store original global config manager
        from nfl_mcp.config_manager import _config_manager
        self.original_config_manager = _config_manager
    
    def teardown_method(self):
        """Clean up test environment."""
        # Restore original global config manager
        from nfl_mcp import config_manager
        config_manager._config_manager = self.original_config_manager
    
    def test_get_config_manager_singleton(self):
        """Test that get_config_manager returns a singleton."""
        # Clear global instance
        from nfl_mcp import config_manager
        config_manager._config_manager = None
        
        manager1 = get_config_manager()
        manager2 = get_config_manager()
        
        assert manager1 is manager2
    
    def test_set_config_manager(self):
        """Test setting a custom global config manager."""
        custom_manager = ConfigManager()
        set_config_manager(custom_manager)
        
        retrieved_manager = get_config_manager()
        assert retrieved_manager is custom_manager


class TestBackwardCompatibility:
    """Test backward compatibility with existing config.py usage."""
    
    def test_config_constants_available(self):
        """Test that existing configuration constants are still available."""
        from nfl_mcp import config
        
        # Test that constants exist and have expected types
        assert hasattr(config, 'DEFAULT_TIMEOUT')
        assert hasattr(config, 'LONG_TIMEOUT')
        assert hasattr(config, 'SERVER_VERSION')
        assert hasattr(config, 'BASE_USER_AGENT')
        assert hasattr(config, 'USER_AGENTS')
        assert hasattr(config, 'ALLOWED_URL_SCHEMES')
        assert hasattr(config, 'LIMITS')
        assert hasattr(config, 'RATE_LIMITS')
        
        # Test that they have correct types
        assert isinstance(config.SERVER_VERSION, str)
        assert isinstance(config.USER_AGENTS, dict)
        assert isinstance(config.LIMITS, dict)
        assert isinstance(config.RATE_LIMITS, dict)
        assert isinstance(config.ALLOWED_URL_SCHEMES, list)
    
    def test_config_functions_work(self):
        """Test that existing configuration functions still work."""
        from nfl_mcp import config
        
        # Test helper functions
        headers = config.get_http_headers("nfl_news")
        assert isinstance(headers, dict)
        assert "User-Agent" in headers
        
        client = config.create_http_client()
        assert client is not None
        
        # Test validation functions
        is_valid = config.is_valid_url("https://example.com")
        assert is_valid is True
        
        # Test string validation
        validated = config.validate_string_input("test", "general")
        assert validated == "test"