#!/usr/bin/env python3
"""
Integration test script for new Fantasy Intelligence APIs.

This script tests the new APIs against real ESPN endpoints to validate functionality.
Run with: python test_integration_new_apis.py
"""

import asyncio
import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nfl_mcp.nfl_tools import get_team_injuries, get_team_player_stats, get_nfl_standings, get_team_schedule


async def test_new_apis():
    """Test the new Fantasy Intelligence APIs with real data."""
    
    print("🏈 Testing Fantasy Intelligence APIs Integration")
    print("=" * 50)
    
    # Test team to use for examples
    test_team = "KC"  # Kansas City Chiefs
    
    # Test 1: Team Injuries
    print(f"\n1️⃣ Testing Team Injuries API for {test_team}...")
    try:
        injuries_result = await get_team_injuries(test_team, limit=5)
        print(f"   ✅ Success: {injuries_result['success']}")
        print(f"   📊 Injuries found: {injuries_result['count']}")
        if injuries_result['injuries']:
            for injury in injuries_result['injuries'][:2]:  # Show first 2
                print(f"   🏥 {injury.get('player_name', 'Unknown')} ({injury.get('position', 'N/A')}) - {injury.get('status', 'Unknown')} [{injury.get('severity', 'Unknown')} severity]")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    # Test 2: Team Player Stats
    print(f"\n2️⃣ Testing Team Player Stats API for {test_team}...")
    try:
        stats_result = await get_team_player_stats(test_team, season=2025, limit=5)
        print(f"   ✅ Success: {stats_result['success']}")
        print(f"   📊 Players found: {stats_result['count']}")
        if stats_result['player_stats']:
            fantasy_relevant = [p for p in stats_result['player_stats'] if p.get('fantasy_relevant')]
            print(f"   🌟 Fantasy relevant players: {len(fantasy_relevant)}")
            for player in fantasy_relevant[:3]:  # Show first 3 fantasy relevant
                print(f"   🏈 {player.get('player_name', 'Unknown')} ({player.get('position', 'N/A')}) - Jersey #{player.get('jersey', 'N/A')}")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    # Test 3: NFL Standings
    print(f"\n3️⃣ Testing NFL Standings API...")
    try:
        standings_result = await get_nfl_standings(season=2025, season_type=2)
        print(f"   ✅ Success: {standings_result['success']}")
        print(f"   📊 Teams in standings: {standings_result['count']}")
        if standings_result['standings']:
            # Show a few teams with different motivation levels
            for team in standings_result['standings'][:3]:
                wins = team.get('wins', 0)
                losses = team.get('losses', 0)
                motivation = team.get('motivation_level', 'Unknown')
                print(f"   🏆 {team.get('abbreviation', 'UNK')} ({wins}-{losses}) - {motivation}")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    # Test 4: Team Schedule
    print(f"\n4️⃣ Testing Team Schedule API for {test_team}...")
    try:
        schedule_result = await get_team_schedule(test_team, season=2025)
        print(f"   ✅ Success: {schedule_result['success']}")
        print(f"   📊 Games in schedule: {schedule_result['count']}")
        if schedule_result['schedule']:
            upcoming_games = [g for g in schedule_result['schedule'] if g.get('result') == 'scheduled'][:3]
            print(f"   📅 Upcoming games: {len(upcoming_games)}")
            for game in upcoming_games:
                opponent = game.get('opponent', {}).get('abbreviation', 'UNK')
                home_away = "vs" if game.get('is_home') else "@"
                week = game.get('week', 'N/A')
                print(f"   🏟️  Week {week}: {home_away} {opponent}")
                if game.get('fantasy_implications'):
                    print(f"      💡 {game['fantasy_implications'][0]}")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print(f"\n🎯 Integration Test Complete!")
    print("=" * 50)


if __name__ == "__main__":
    print("Starting Fantasy Intelligence APIs Integration Test...")
    asyncio.run(test_new_apis())