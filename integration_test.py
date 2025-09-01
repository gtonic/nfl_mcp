#!/usr/bin/env python3
"""
Integration test script for NFL MCP Server

This script demonstrates both the REST health endpoint and MCP multiply tool functionality.
"""

import asyncio
import httpx
import time
from fastmcp import Client


async def test_health_endpoint():
    """Test the health REST endpoint."""
    print("🏥 Testing Health Endpoint...")
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get("http://localhost:9000/health", timeout=5.0)
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Health endpoint working! Status: {data['status']}")
                print(f"   Service: {data['service']}")
                print(f"   Version: {data['version']}")
                return True
            else:
                print(f"❌ Health endpoint failed with status: {response.status_code}")
                return False
                
        except Exception as e:
            print(f"❌ Health endpoint error: {e}")
            return False


async def test_mcp_multiply_tool():
    """Test the MCP multiply tool."""
    print("\n🔧 Testing MCP Multiply Tool...")
    
    try:
        async with Client("http://localhost:9000/mcp/") as client:
            # Test basic multiplication
            result = await client.call_tool("multiply", {"x": 6, "y": 7})
            expected = 42
            
            if result.data == expected:
                print(f"✅ Multiply tool working! 6 × 7 = {result.data}")
            else:
                print(f"❌ Multiply tool failed! Expected {expected}, got {result.data}")
                return False
            
            # Test with zero
            result = await client.call_tool("multiply", {"x": 100, "y": 0})
            expected = 0
            
            if result.data == expected:
                print(f"✅ Zero test passed! 100 × 0 = {result.data}")
            else:
                print(f"❌ Zero test failed! Expected {expected}, got {result.data}")
                return False
            
            # Test with negative numbers
            result = await client.call_tool("multiply", {"x": -5, "y": 3})
            expected = -15
            
            if result.data == expected:
                print(f"✅ Negative test passed! -5 × 3 = {result.data}")
            else:
                print(f"❌ Negative test failed! Expected {expected}, got {result.data}")
                return False
            
            return True
            
    except Exception as e:
        print(f"❌ MCP tool error: {e}")
        return False


async def main():
    """Main test function."""
    print("🚀 NFL MCP Server Integration Test")
    print("=" * 50)
    
    print("⏳ Waiting for server to start...")
    await asyncio.sleep(2)  # Give server time to start
    
    # Test health endpoint
    health_ok = await test_health_endpoint()
    
    # Test MCP functionality
    mcp_ok = await test_mcp_multiply_tool()
    
    print("\n" + "=" * 50)
    if health_ok and mcp_ok:
        print("🎉 All tests passed! Server is working correctly.")
        return 0
    else:
        print("💥 Some tests failed!")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)