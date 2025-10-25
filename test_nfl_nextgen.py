"""Test NFL NextGen Stats API integration"""

import sys
from pathlib import Path
import importlib.util

# Find and import the nfl_nextgen_client module directly
src_path = Path(__file__).parent / "src" / "nfl_mcp"
client_file = src_path / "nfl_nextgen_client.py"

if not client_file.exists():
    print(f"Error: {client_file} does not exist!")
    print(f"Looking for file at: {client_file.absolute()}")
    sys.exit(1)

# Import the module directly
spec = importlib.util.spec_from_file_location("nfl_nextgen_client", client_file)
nfl_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nfl_module)
NFLNextGenClient = nfl_module.NFLNextGenClient

def main():
    print("="*80)
    print("TESTING NFL NEXTGEN STATS API")
    print("="*80)
    
    client = NFLNextGenClient(debug=True)
    
    print("\n1. Fetching player stats for 2024 season...")
    stats = client.get_player_stats(season=2024)
    
    if stats:
        print(f"✓ Retrieved stats for {len(stats)} players")
        
        # Show sample players
        print("\n" + "="*80)
        print("SAMPLE PLAYERS WITH STATS:")
        print("="*80)
        
        for i, player in enumerate(stats[:5], 1):
            print(f"\n{i}. Player ID: {player.get('player_id')}")
            print(f"   Snap %: {player.get('snap_percentage', 0):.1%}")
            print(f"   Targets: {player.get('targets', 0)}")
            print(f"   Routes: {player.get('routes', 0)}")
            print(f"   RZ Touches: {player.get('rz_touches', 0)}")
    else:
        print("✗ No stats retrieved")
        print("\n" + "="*80)
        print("NEXT STEPS:")
        print("="*80)
        print("The NFL API endpoints are not publicly available.")
        print("\nAlternative solutions:")
        print("1. Use web scraping from nfl.com/stats")
        print("2. Use a paid API service (FantasyPros, RotoWire)")
        print("3. Set fields to 0 for now with TODO comments")
        print("\nWhich approach would you like to use?")

if __name__ == "__main__":
    main()
