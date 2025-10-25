"""Test weekly stats calculation"""

import sys
from pathlib import Path
import importlib.util

# Find and import the sleeper_weekly_stats module directly
src_path = Path(__file__).parent / "src" / "nfl_mcp"
stats_file = src_path / "sleeper_weekly_stats.py"

if not stats_file.exists():
    print(f"Error: {stats_file} does not exist!")
    print(f"Looking for file at: {stats_file.absolute()}")
    sys.exit(1)

# Import the module directly
spec = importlib.util.spec_from_file_location("sleeper_weekly_stats", stats_file)
stats_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats_module)
SleeperWeeklyStats = stats_module.SleeperWeeklyStats

def main():
    print("="*80)
    print("TESTING SLEEPER WEEKLY STATS CALCULATION")
    print("="*80)
    
    client = SleeperWeeklyStats(debug=True)
    
    # Get current week
    season, week = client.get_current_week_and_season()
    print(f"\n✓ Current Season: {season}, Week: {week}")
    
    # Test with known popular players (Sleeper IDs)
    # These are example IDs - they may need to be updated
    test_player_ids = ["4035", "4046", "6786", "7564"]  # Popular players
    
    print("\n" + "="*80)
    print("CALCULATING 3-WEEK AVERAGES FOR SAMPLE PLAYERS:")
    print("="*80)
    
    results = client.get_bulk_three_week_averages(test_player_ids, season)
    
    if results:
        for player_id, stats in results.items():
            print(f"\nPlayer ID: {player_id}")
            print(f"  Targets/G (3W): {stats['targets_per_game_3w']}")
            print(f"  Receptions/G (3W): {stats['receptions_per_game_3w']}")
            print(f"  Rush Att/G (3W): {stats['rush_attempts_per_game_3w']}")
            print(f"  RZ Touches/G: {stats['rz_touches_per_game']}")
        
        print("\n" + "="*80)
        print("✓ SUCCESS! Phase 1 Implementation Complete")
        print("="*80)
        print("✓ Targets/G (3W) - Calculated from Sleeper weekly stats")
        print("✓ RZ Touches/G - Calculated from Sleeper weekly stats")
        print("✗ Snap% - Still needs external source")
        print("✗ Routes/G (3W) - Still needs external source")
    else:
        print("\n✗ No data returned. This could mean:")
        print("  - Player IDs are incorrect")
        print("  - No stats available for recent weeks")
        print("  - API issue")

if __name__ == "__main__":
    main()
