"""
Configuration management system for NFL MCP Server.

This module provides flexible configuration management with support for:
- Environment variables
- Configuration files (YAML/JSON)
- Configuration validation
- Hot-reloading
"""

import os
import json
import yaml
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union, List
from dataclasses import dataclass, field
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import httpx
from pydantic import BaseModel, ValidationError, Field


@dataclass
class TimeoutConfig:
    """HTTP timeout configuration."""
    total: float = 30.0
    connect: float = 10.0


@dataclass
class LongTimeoutConfig:
    """Long HTTP timeout configuration for large data fetches."""
    total: float = 60.0
    connect: float = 15.0


@dataclass
class ServerConfig:
    """Server configuration."""
    version: str = "0.1.0"
    base_user_agent: str = field(init=False)
    
    def __post_init__(self):
        self.base_user_agent = f"NFL-MCP-Server/{self.version}"


@dataclass
class ValidationLimits:
    """Parameter validation limits."""
    nfl_news_max: int = 50
    nfl_news_min: int = 1
    athletes_search_max: int = 100
    athletes_search_min: int = 1
    athletes_search_default: int = 10
    week_min: int = 1
    week_max: int = 22
    round_min: int = 1
    round_max: int = 18
    trending_lookback_min: int = 1
    trending_lookback_max: int = 168
    trending_limit_min: int = 1
    trending_limit_max: int = 100


@dataclass
class RateLimitConfig:
    """Rate limiting configuration."""
    default_requests_per_minute: int = 60
    heavy_requests_per_minute: int = 10
    burst_limit: int = 5


@dataclass
class SecurityConfig:
    """Security configuration."""
    allowed_url_schemes: List[str] = field(default_factory=lambda: ["http://", "https://"])
    max_string_length: int = 1000
    enable_injection_detection: bool = True


class ConfigurationModel(BaseModel):
    """Pydantic model for configuration validation."""
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    long_timeout: LongTimeoutConfig = Field(default_factory=LongTimeoutConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    limits: ValidationLimits = Field(default_factory=ValidationLimits)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    
    model_config = {"arbitrary_types_allowed": True}


class ConfigFileHandler(FileSystemEventHandler):
    """File system event handler for configuration hot-reloading."""
    
    def __init__(self, config_manager: 'ConfigManager'):
        self.config_manager = config_manager
        super().__init__()
    
    def on_modified(self, event):
        if not event.is_directory and event.src_path == str(self.config_manager.config_file_path):
            print(f"Configuration file {event.src_path} modified, reloading...")
            self.config_manager.reload_configuration()


class ConfigManager:
    """
    Flexible configuration manager supporting environment variables,
    configuration files, validation, and hot-reloading.
    """
    
    def __init__(self, config_file: Optional[Union[str, Path]] = None, enable_hot_reload: bool = True):
        """
        Initialize the configuration manager.
        
        Args:
            config_file: Path to configuration file (YAML or JSON)
            enable_hot_reload: Whether to enable hot-reloading of configuration files
        """
        self.config_file_path = Path(config_file) if config_file else None
        self.enable_hot_reload = enable_hot_reload
        self._config_lock = threading.RLock()
        self._observer = None
        self._config: Optional[ConfigurationModel] = None
        
        # Load initial configuration
        self.load_configuration()
        
        # Set up hot-reloading if enabled and config file exists
        if self.enable_hot_reload and self.config_file_path and self.config_file_path.exists():
            self._setup_hot_reload()
    
    def _setup_hot_reload(self):
        """Set up file system monitoring for hot-reloading."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
        
        self._observer = Observer()
        event_handler = ConfigFileHandler(self)
        self._observer.schedule(event_handler, str(self.config_file_path.parent), recursive=False)
        self._observer.start()
    
    def load_configuration(self):
        """Load configuration from environment variables and config file."""
        with self._config_lock:
            # Start with default configuration
            config_dict = {}
            
            # Load from config file if it exists
            if self.config_file_path and self.config_file_path.exists():
                config_dict = self._load_config_file()
            
            # Override with environment variables
            config_dict = self._load_environment_variables(config_dict)
            
            # Validate and create configuration object
            try:
                self._config = ConfigurationModel(**config_dict)
            except ValidationError as e:
                raise ValueError(f"Configuration validation failed: {e}")
    
    def _load_config_file(self) -> Dict[str, Any]:
        """Load configuration from YAML or JSON file."""
        try:
            with open(self.config_file_path, 'r') as f:
                if self.config_file_path.suffix.lower() in ['.yml', '.yaml']:
                    return yaml.safe_load(f) or {}
                elif self.config_file_path.suffix.lower() == '.json':
                    return json.load(f)
                else:
                    raise ValueError(f"Unsupported configuration file format: {self.config_file_path.suffix}")
        except Exception as e:
            raise ValueError(f"Failed to load configuration file {self.config_file_path}: {e}")
    
    def _load_environment_variables(self, config_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Load configuration from environment variables."""
        # Environment variable mapping
        env_mappings = {
            # Timeout configuration
            'NFL_MCP_TIMEOUT_TOTAL': ('timeout', 'total', float),
            'NFL_MCP_TIMEOUT_CONNECT': ('timeout', 'connect', float),
            'NFL_MCP_LONG_TIMEOUT_TOTAL': ('long_timeout', 'total', float),
            'NFL_MCP_LONG_TIMEOUT_CONNECT': ('long_timeout', 'connect', float),
            
            # Server configuration
            'NFL_MCP_SERVER_VERSION': ('server', 'version', str),
            
            # Validation limits
            'NFL_MCP_NFL_NEWS_MAX': ('limits', 'nfl_news_max', int),
            'NFL_MCP_NFL_NEWS_MIN': ('limits', 'nfl_news_min', int),
            'NFL_MCP_ATHLETES_SEARCH_MAX': ('limits', 'athletes_search_max', int),
            'NFL_MCP_ATHLETES_SEARCH_MIN': ('limits', 'athletes_search_min', int),
            'NFL_MCP_ATHLETES_SEARCH_DEFAULT': ('limits', 'athletes_search_default', int),
            'NFL_MCP_WEEK_MIN': ('limits', 'week_min', int),
            'NFL_MCP_WEEK_MAX': ('limits', 'week_max', int),
            'NFL_MCP_ROUND_MIN': ('limits', 'round_min', int),
            'NFL_MCP_ROUND_MAX': ('limits', 'round_max', int),
            'NFL_MCP_TRENDING_LOOKBACK_MIN': ('limits', 'trending_lookback_min', int),
            'NFL_MCP_TRENDING_LOOKBACK_MAX': ('limits', 'trending_lookback_max', int),
            'NFL_MCP_TRENDING_LIMIT_MIN': ('limits', 'trending_limit_min', int),
            'NFL_MCP_TRENDING_LIMIT_MAX': ('limits', 'trending_limit_max', int),
            
            # Rate limiting
            'NFL_MCP_RATE_LIMIT_DEFAULT': ('rate_limits', 'default_requests_per_minute', int),
            'NFL_MCP_RATE_LIMIT_HEAVY': ('rate_limits', 'heavy_requests_per_minute', int),
            'NFL_MCP_RATE_LIMIT_BURST': ('rate_limits', 'burst_limit', int),
            
            # Security
            'NFL_MCP_MAX_STRING_LENGTH': ('security', 'max_string_length', int),
            'NFL_MCP_ENABLE_INJECTION_DETECTION': ('security', 'enable_injection_detection', lambda x: x.lower() in ['true', '1', 'yes']),
        }
        
        for env_var, (section, key, type_converter) in env_mappings.items():
            env_value = os.getenv(env_var)
            if env_value is not None:
                try:
                    # Ensure nested structure exists
                    if section not in config_dict:
                        config_dict[section] = {}
                    
                    # Convert and set the value
                    config_dict[section][key] = type_converter(env_value)
                except (ValueError, TypeError) as e:
                    raise ValueError(f"Invalid value for environment variable {env_var}: {env_value} ({e})")
        
        # Handle URL schemes separately (comma-separated list)
        url_schemes = os.getenv('NFL_MCP_ALLOWED_URL_SCHEMES')
        if url_schemes:
            if 'security' not in config_dict:
                config_dict['security'] = {}
            config_dict['security']['allowed_url_schemes'] = [scheme.strip() for scheme in url_schemes.split(',')]
        
        return config_dict
    
    def reload_configuration(self):
        """Reload configuration from file and environment variables."""
        try:
            self.load_configuration()
            print("Configuration reloaded successfully")
        except Exception as e:
            print(f"Failed to reload configuration: {e}")
    
    @property
    def config(self) -> ConfigurationModel:
        """Get the current configuration."""
        with self._config_lock:
            if self._config is None:
                raise RuntimeError("Configuration not loaded")
            return self._config
    
    def get_http_timeout(self) -> httpx.Timeout:
        """Get HTTP timeout configuration."""
        timeout_config = self.config.timeout
        return httpx.Timeout(timeout_config.total, connect=timeout_config.connect)
    
    def get_long_http_timeout(self) -> httpx.Timeout:
        """Get long HTTP timeout configuration."""
        timeout_config = self.config.long_timeout
        return httpx.Timeout(timeout_config.total, connect=timeout_config.connect)
    
    def get_user_agent(self, service_name: str = None) -> str:
        """Get user agent string for a service."""
        base_agent = self.config.server.base_user_agent
        if service_name:
            # Mapping of service names to descriptions
            service_descriptions = {
                "nfl_news": "NFL News Fetcher",
                "nfl_teams": "NFL Teams Fetcher",
                "depth_chart": "NFL Depth Chart Fetcher",
                "web_crawler": "Web Content Extractor",
                "athletes": "NFL Athletes Fetcher",
                "sleeper_league": "Sleeper League Fetcher",
                "sleeper_rosters": "Sleeper Rosters Fetcher",
                "sleeper_users": "Sleeper Users Fetcher",
                "sleeper_matchups": "Sleeper Matchups Fetcher",
                "sleeper_playoffs": "Sleeper Playoffs Fetcher",
                "sleeper_transactions": "Sleeper Transactions Fetcher",
                "sleeper_traded_picks": "Sleeper Traded Picks Fetcher",
                "sleeper_nfl_state": "Sleeper NFL State Fetcher",
                "sleeper_trending": "Sleeper Trending Players Fetcher",
                "cbs_fantasy": "CBS Fantasy Football Fetcher",
            }
            description = service_descriptions.get(service_name, "Generic Service")
            return f"{base_agent} ({description})"
        return base_agent
    
    def get_limits_dict(self) -> Dict[str, int]:
        """Get validation limits as a dictionary for backward compatibility."""
        limits = self.config.limits
        return {
            "nfl_news_max": limits.nfl_news_max,
            "nfl_news_min": limits.nfl_news_min,
            "athletes_search_max": limits.athletes_search_max,
            "athletes_search_min": limits.athletes_search_min,
            "athletes_search_default": limits.athletes_search_default,
            "week_min": limits.week_min,
            "week_max": limits.week_max,
            "round_min": limits.round_min,
            "round_max": limits.round_max,
            "trending_lookback_min": limits.trending_lookback_min,
            "trending_lookback_max": limits.trending_lookback_max,
            "trending_limit_min": limits.trending_limit_min,
            "trending_limit_max": limits.trending_limit_max,
        }
    
    def get_rate_limits_dict(self) -> Dict[str, int]:
        """Get rate limits as a dictionary for backward compatibility."""
        rate_limits = self.config.rate_limits
        return {
            "default_requests_per_minute": rate_limits.default_requests_per_minute,
            "heavy_requests_per_minute": rate_limits.heavy_requests_per_minute,
            "burst_limit": rate_limits.burst_limit,
        }
    
    def stop(self):
        """Stop the configuration manager and clean up resources."""
        if self._observer:
            self._observer.stop()
            self._observer.join()


# Global configuration manager instance
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """Get the global configuration manager instance."""
    global _config_manager
    if _config_manager is None:
        # Look for config file in common locations
        config_paths = [
            Path("config.yml"),
            Path("config.yaml"),
            Path("config.json"),
            Path("/etc/nfl-mcp/config.yml"),
            Path("/etc/nfl-mcp/config.yaml"),
            Path("/etc/nfl-mcp/config.json"),
        ]
        
        config_file = None
        for path in config_paths:
            if path.exists():
                config_file = path
                break
        
        _config_manager = ConfigManager(config_file)
    
    return _config_manager


def set_config_manager(config_manager: ConfigManager):
    """Set the global configuration manager instance."""
    global _config_manager
    if _config_manager:
        _config_manager.stop()
    _config_manager = config_manager