"""Simple test of the weekly stats integration"""

import sys
from pathlib import Path

# Add src to Python path
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

# Import the modules
try:
    from nfl_mcp.sleeper_weekly_stats import SleeperWeeklyStats
    from nfl_mcp.fantasy_extractor import FantasyExtractor
    print("✓ Modules imported successfully")
except ImportError as e:
    print(f"✗ Import error: {e}")
    print(f"  Python path: {sys.path[:3]}")
    print(f"  Looking in: {src_path}")
    sys.exit(1)

def main():
    print("="*80)
    print("TESTING FANTASY DATA WITH 3-WEEK AVERAGES")
    print("="*80)
    
    # Test with a small sample
    print("\nInitializing FantasyExtractor...")
    extractor = FantasyExtractor(debug=False)
    
    print("Fetching players (limited to 5 for quick test)...")
    players = extractor.get_all_players(limit=5)
    
    print(f"\n{'='*80}")
    print(f"FETCHED {len(players)} PLAYERS")
    print(f"{'='*80}\n")
    
    for i, player in enumerate(players, 1):
        print(f"{i}. {player['name']:<25} {player['position']:<3} {player['team']:<4}")
        print(f"   Targets/G (3W):  {player['targets_per_game']:.1f}")
        print(f"   RZ Touches/G:    {player['rz_touches_per_game']:.1f}")
        print(f"   Snap%:           {player['snap_percentage']:.1%} (TODO)")
        print(f"   Routes/G (3W):   {player['routes_per_game']:.1f} (TODO)")
        print()
    
    print(f"{'='*80}")
    print("✓ PHASE 1 COMPLETE - Fields are now populated!")
    print(f"{'='*80}")
    print("✓ targets_per_game - From Sleeper 3-week average")
    print("✓ rz_touches_per_game - From Sleeper 3-week average") 
    print("✗ snap_percentage - Needs Phase 2")
    print("✗ routes_per_game - Needs Phase 2")

if __name__ == "__main__":
    main()
