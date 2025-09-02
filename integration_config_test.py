#!/usr/bin/env python3
"""
Integration test demonstrating the new configuration system.
This script shows how environment variables and config files work.
"""

import os
import tempfile
import yaml
from pathlib import Path

# Set some environment variables
os.environ['NFL_MCP_SERVER_VERSION'] = '2.0.0'
os.environ['NFL_MCP_TIMEOUT_TOTAL'] = '45.0'
os.environ['NFL_MCP_NFL_NEWS_MAX'] = '75'

# Import after setting environment variables
from nfl_mcp.config_manager import ConfigManager, get_config_manager
from nfl_mcp import config

def test_environment_variables():
    """Test that environment variables override defaults."""
    print("=== Testing Environment Variables ===")
    
    manager = ConfigManager()
    
    print(f"Server version: {manager.config.server.version}")  # Should be 2.0.0
    print(f"Timeout total: {manager.config.timeout.total}")    # Should be 45.0
    print(f"NFL news max: {manager.config.limits.nfl_news_max}")  # Should be 75
    
    assert manager.config.server.version == "2.0.0"
    assert manager.config.timeout.total == 45.0
    assert manager.config.limits.nfl_news_max == 75
    
    print("✓ Environment variables working correctly!")

def test_config_file_with_env_override():
    """Test config file with environment variable override."""
    print("\n=== Testing Config File with Environment Override ===")
    
    # Create a temporary config file
    config_data = {
        'timeout': {'total': 35.0, 'connect': 12.0},
        'server': {'version': '1.5.0'},  # This should be overridden by env var
        'limits': {'nfl_news_max': 60}   # This should be overridden by env var
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
        yaml.dump(config_data, f)
        config_file = f.name
    
    try:
        manager = ConfigManager(config_file=config_file, enable_hot_reload=False)
        
        print(f"Server version: {manager.config.server.version}")  # Should be 2.0.0 (from env)
        print(f"Timeout total: {manager.config.timeout.total}")    # Should be 45.0 (from env)
        print(f"Timeout connect: {manager.config.timeout.connect}")  # Should be 12.0 (from file)
        print(f"NFL news max: {manager.config.limits.nfl_news_max}")  # Should be 75 (from env)
        
        # Environment variables should override file values
        assert manager.config.server.version == "2.0.0"  # From env
        assert manager.config.timeout.total == 45.0      # From env
        assert manager.config.timeout.connect == 12.0    # From file
        assert manager.config.limits.nfl_news_max == 75  # From env
        
        print("✓ Config file with environment override working correctly!")
    finally:
        os.unlink(config_file)

def test_backward_compatibility():
    """Test that existing config.py usage still works."""
    print("\n=== Testing Backward Compatibility ===")
    
    # These should all work as before
    print(f"DEFAULT_TIMEOUT: {config.DEFAULT_TIMEOUT}")
    print(f"SERVER_VERSION: {config.SERVER_VERSION}")  # Should be 2.0.0 from env
    print(f"LIMITS['nfl_news_max']: {config.LIMITS['nfl_news_max']}")  # Should be 75 from env
    
    # Test functions
    headers = config.get_http_headers("nfl_news")
    print(f"User-Agent header: {headers['User-Agent']}")
    
    client = config.create_http_client()
    print(f"HTTP client timeout: {client._timeout}")
    
    # Verify environment variables are reflected
    assert config.SERVER_VERSION == "2.0.0"
    assert config.LIMITS['nfl_news_max'] == 75
    
    print("✓ Backward compatibility maintained!")

def test_user_agent_generation():
    """Test that user agents are generated correctly with new version."""
    print("\n=== Testing User Agent Generation ===")
    
    manager = get_config_manager()
    
    base_agent = manager.get_user_agent()
    service_agent = manager.get_user_agent("nfl_news")
    
    print(f"Base user agent: {base_agent}")
    print(f"Service user agent: {service_agent}")
    
    assert "2.0.0" in base_agent  # Should include env version
    assert "NFL News Fetcher" in service_agent
    
    print("✓ User agent generation working correctly!")

def test_hot_reload_simulation():
    """Test configuration hot reload simulation."""
    print("\n=== Testing Hot Reload Simulation ===")
    
    # Create initial config file
    config_data = {'server': {'version': '1.0.0'}}
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
        yaml.dump(config_data, f)
        config_file = f.name
    
    try:
        manager = ConfigManager(config_file=config_file, enable_hot_reload=False)
        
        # Environment variable should still override
        print(f"Initial version: {manager.config.server.version}")  # Should be 2.0.0 from env
        assert manager.config.server.version == "2.0.0"
        
        # Update config file
        updated_config = {'server': {'version': '3.0.0'}}
        with open(config_file, 'w') as f:
            yaml.dump(updated_config, f)
        
        # Manually reload (simulating hot reload)
        manager.reload_configuration()
        
        # Environment variable should still override
        print(f"After reload version: {manager.config.server.version}")  # Should still be 2.0.0 from env
        assert manager.config.server.version == "2.0.0"
        
        print("✓ Hot reload simulation working correctly!")
    finally:
        os.unlink(config_file)

def main():
    """Run all integration tests."""
    print("NFL MCP Configuration System Integration Test")
    print("=" * 50)
    
    try:
        test_environment_variables()
        test_config_file_with_env_override()
        test_backward_compatibility()
        test_user_agent_generation()
        test_hot_reload_simulation()
        
        print("\n" + "=" * 50)
        print("🎉 All integration tests passed!")
        print("Configuration system is working correctly!")
        
    except Exception as e:
        print(f"\n❌ Integration test failed: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())