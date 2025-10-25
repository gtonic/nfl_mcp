#!/usr/bin/env python3
"""Test ESPN Injury API structure"""
import asyncio
import json
import httpx

async def test_injury_structure():
    url = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/ARI/injuries?limit=50&page=1"
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        data = resp.json()
        
        print(f"\n{'='*60}")
        print(f"Response Status: {resp.status_code}")
        print(f"Count: {data.get('count')}")
        print(f"PageCount: {data.get('pageCount')}")
        print(f"Items Length: {len(data.get('items', []))}")
        print(f"{'='*60}\n")
        
        items = data.get('items', [])
        if items:
            print("FIRST ITEM STRUCTURE:")
            print(json.dumps(items[0], indent=2, default=str))
            
            print("\n\nATHLETE OBJECT:")
            athlete = items[0].get('athlete', {})
            print(json.dumps(athlete, indent=2, default=str))
            
            print(f"\n\nATHLETE KEYS: {list(athlete.keys())}")
            print(f"Athlete 'id': {athlete.get('id')}")
            print(f"Athlete 'displayName': {athlete.get('displayName')}")
            print(f"Athlete '$ref': {athlete.get('$ref')}")

if __name__ == "__main__":
    asyncio.run(test_injury_structure())
