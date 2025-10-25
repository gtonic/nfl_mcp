#!/usr/bin/env python3
"""
Test script to debug ESPN Injury API responses
"""

import asyncio
import httpx
import json

async def test_espn_injuries():
    """Test ESPN injury API to see what's actually returned."""
    
    # Test a few teams
    teams = ["KC", "BUF", "SF", "PHI"]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for team in teams:
            url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{team}/injuries?limit=50"
            
            print(f"\n{'='*80}")
            print(f"Testing: {team}")
            print(f"URL: {url}")
            print(f"{'='*80}")
            
            try:
                resp = await client.get(url, headers=headers)
                
                print(f"Status: {resp.status_code}")
                print(f"Headers: {dict(resp.headers)}")
                
                if resp.status_code == 200:
                    data = resp.json()
                    print(f"\nResponse Keys: {list(data.keys())}")
                    print(f"Count: {data.get('count', 'N/A')}")
                    print(f"PageIndex: {data.get('pageIndex', 'N/A')}")
                    print(f"PageSize: {data.get('pageSize', 'N/A')}")
                    print(f"PageCount: {data.get('pageCount', 'N/A')}")
                    
                    items = data.get('items', [])
                    print(f"\nItems count: {len(items)}")
                    
                    if items:
                        print(f"\nFirst item structure:")
                        print(json.dumps(items[0], indent=2, default=str))
                    else:
                        print(f"\n⚠️ NO ITEMS! Full response:")
                        print(json.dumps(data, indent=2, default=str))
                else:
                    print(f"Error: {resp.status_code}")
                    print(f"Body: {resp.text[:500]}")
                    
            except Exception as e:
                print(f"Exception: {e}")
                import traceback
                traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_espn_injuries())
