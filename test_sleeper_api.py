"""Test script to check Sleeper API response structure"""

import json
import requests
from typing import Dict, Any

def test_sleeper_api():
    """Test Sleeper API to see what data is available"""
    
    print("="*80)
    print("TESTING SLEEPER API")
    print("="*80)
    
    # Get NFL state (current week, season)
    print("\n1. Getting NFL state...")
    state_url = "https://api.sleeper.app/v1/state/nfl"
    try:
        response = requests.get(state_url)
        state = response.json()
        print(f"✓ Current Season: {state.get('season')}")
        print(f"✓ Current Week: {state.get('week')}")
        print(f"✓ Season Type: {state.get('season_type')}")
    except Exception as e:
        print(f"✗ Error: {e}")
        return
    
    # Get all NFL players
    print("\n2. Getting NFL players data...")
    players_url = "https://api.sleeper.app/v1/players/nfl"
    try:
        response = requests.get(players_url)
        all_players = response.json()
        print(f"✓ Total players: {len(all_players)}")
        
        # Find a few active players
        active_players = []
        for player_id, player_data in list(all_players.items())[:1000]:
            if (player_data.get('active') and 
                player_data.get('position') in ['QB', 'RB', 'WR', 'TE']):
                active_players.append((player_id, player_data))
                if len(active_players) >= 5:
                    break
        
        print(f"✓ Found {len(active_players)} active offensive players for analysis")
        
        # Analyze first player in detail
        if active_players:
            player_id, player = active_players[0]
            
            print("\n" + "="*80)
            print(f"SAMPLE PLAYER: {player.get('full_name', 'Unknown')}")
            print("="*80)
            print(f"Player ID: {player_id}")
            print(f"Position: {player.get('position')}")
            print(f"Team: {player.get('team')}")
            
            print("\n" + "="*80)
            print("ALL AVAILABLE FIELDS:")
            print("="*80)
            for key in sorted(player.keys()):
                value = player[key]
                if value not in [None, '', [], {}, 0]:
                    print(f"  {key}: {value}")
            
            print("\n" + "="*80)
            print("CHECKING TARGET FIELDS:")
            print("="*80)
            
            # Check for snap percentage
            snap_fields = ['snap_percentage', 'snap_count', 'snaps', 'snap_pct']
            print("\nSnap% related fields:")
            for field in snap_fields:
                if field in player:
                    print(f"  ✓ {field}: {player[field]}")
                else:
                    print(f"  ✗ {field}: NOT FOUND")
            
            # Check for targets
            target_fields = ['targets', 'targets_per_game', 'avg_targets', 'targets_3w']
            print("\nTargets related fields:")
            for field in target_fields:
                if field in player:
                    print(f"  ✓ {field}: {player[field]}")
                else:
                    print(f"  ✗ {field}: NOT FOUND")
            
            # Check for routes
            route_fields = ['routes', 'routes_per_game', 'avg_routes', 'routes_3w']
            print("\nRoutes related fields:")
            for field in route_fields:
                if field in player:
                    print(f"  ✓ {field}: {player[field]}")
                else:
                    print(f"  ✗ {field}: NOT FOUND")
            
            # Check for red zone touches
            rz_fields = ['red_zone_touches', 'rz_touches', 'redzone_touches', 'rz_targets', 'rz_carries']
            print("\nRed Zone related fields:")
            for field in rz_fields:
                if field in player:
                    print(f"  ✓ {field}: {player[field]}")
                else:
                    print(f"  ✗ {field}: NOT FOUND")
            
            print("\n" + "="*80)
            print("FULL PLAYER DATA (JSON):")
            print("="*80)
            print(json.dumps(player, indent=2))
            
            # Check other players
            print("\n" + "="*80)
            print("OTHER PLAYERS SAMPLE:")
            print("="*80)
            for player_id, player in active_players[1:4]:
                print(f"\n{player.get('full_name', 'Unknown')} ({player.get('position')} - {player.get('team')})")
                print(f"  Available stats keys: {[k for k in player.keys() if 'stat' in k.lower() or 'snap' in k.lower() or 'target' in k.lower() or 'route' in k.lower()]}")
        
        print("\n" + "="*80)
        print("CONCLUSION:")
        print("="*80)
        print("Note: Sleeper API provides basic player info but NOT advanced stats like:")
        print("  - Snap percentages")
        print("  - Targets per game (3-week)")
        print("  - Routes per game (3-week)")
        print("  - Red zone touches per game")
        print("\nThese stats need to be fetched from:")
        print("  - NFL's NextGen Stats API")
        print("  - Pro Football Focus (PFF)")
        print("  - Fantasy Data providers (e.g., RotoWire, FantasyPros)")
        print("  - ESPN Fantasy API")
        
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_sleeper_api()
