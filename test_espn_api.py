"""Quick test script to check ESPN API response structure"""

import json
import sys
from pathlib import Path
import os

def find_fantasy_extractor():
    """Find the fantasy_extractor.py file"""
    base_path = Path(__file__).parent
    
    # Search for fantasy_extractor.py
    print("Searching for fantasy_extractor.py...")
    for root, dirs, files in os.walk(base_path):
        if 'fantasy_extractor.py' in files:
            full_path = Path(root) / 'fantasy_extractor.py'
            print(f"Found at: {full_path}")
            
            # Add parent directory to path
            parent = Path(root).parent
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
                print(f"Added to path: {parent}")
            
            return full_path
    
    print("fantasy_extractor.py not found!")
    return None

def main():
    # Find and add the correct path
    extractor_path = find_fantasy_extractor()
    
    if not extractor_path:
        print("\nCannot proceed without fantasy_extractor.py")
        print("\nDirectory structure:")
        for root, dirs, files in os.walk(Path(__file__).parent):
            level = root.replace(str(Path(__file__).parent), '').count(os.sep)
            indent = ' ' * 2 * level
            print(f'{indent}{os.path.basename(root)}/')
            subindent = ' ' * 2 * (level + 1)
            for file in files:
                if file.endswith('.py'):
                    print(f'{subindent}{file}')
        return
    
    try:
        # Try different import methods
        try:
            from nfl_mcp.fantasy_extractor import FantasyExtractor
            print("✓ Import successful: nfl_mcp.fantasy_extractor")
        except ImportError:
            try:
                from src.nfl_mcp.fantasy_extractor import FantasyExtractor
                print("✓ Import successful: src.nfl_mcp.fantasy_extractor")
            except ImportError:
                # Direct import from file
                import importlib.util
                spec = importlib.util.spec_from_file_location("fantasy_extractor", extractor_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                FantasyExtractor = module.FantasyExtractor
                print("✓ Import successful: direct file import")
        
    except Exception as e:
        print(f"Error importing FantasyExtractor: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Initialize extractor with debug mode
    print("\nInitializing FantasyExtractor...")
    extractor = FantasyExtractor()
    
    # Show available methods
    print("\nAvailable methods in FantasyExtractor:")
    for method in dir(extractor):
        if not method.startswith('_') and callable(getattr(extractor, method)):
            print(f"  - {method}")
    
    # Test with a league ID
    league_id = input("\nEnter your ESPN Fantasy League ID (or press Enter to skip): ").strip()
    
    if not league_id:
        print("\nNo league ID provided. Cannot fetch player data without it.")
        print("\nTo find your league ID:")
        print("1. Go to your ESPN Fantasy Football league")
        print("2. Look at the URL: https://fantasy.espn.com/football/league?leagueId=XXXXXX")
        print("3. The number after 'leagueId=' is your league ID")
        return
    
    try:
        print(f"\nFetching players from ESPN Fantasy API (League: {league_id})...")
        
        # Try to get roster players first
        try:
            players = extractor.get_roster_players(league_id=league_id)
            print(f"✓ Using get_roster_players method")
        except:
            # Fallback to get_all_players if available
            try:
                players = extractor.get_all_players(league_id=league_id, limit=5)
                print(f"✓ Using get_all_players method")
            except AttributeError:
                print("ERROR: Neither get_roster_players nor get_all_players method found")
                print("\nAvailable methods:")
                for method in dir(extractor):
                    if not method.startswith('_'):
                        print(f"  - {method}")
                return
        
        # Limit to first 5 players for analysis
        players = list(players)[:5] if hasattr(players, '__iter__') else players
        
        print(f"\n{'='*80}")
        print(f"Successfully fetched {len(players)} players")
        print(f"{'='*80}")
        
        if players:
            print("\n" + "="*80)
            print("FIRST PLAYER - FULL DATA STRUCTURE:")
            print("="*80)
            print(json.dumps(players[0], indent=2))
            
            print("\n" + "="*80)
            print("CHECKING PROBLEMATIC FIELDS (First 3 players):")
            print("="*80)
            
            for i, player in enumerate(players[:3], 1):
                print(f"\n{i}. {player.get('name', 'Unknown')} ({player.get('position', '?')} - {player.get('team', '?')})")
                print(f"   snap_percentage: {player.get('snap_percentage', 'MISSING')}")
                print(f"   targets_per_game: {player.get('targets_per_game', 'MISSING')}")
                print(f"   routes_per_game: {player.get('routes_per_game', 'MISSING')}")
                print(f"   rz_touches_per_game: {player.get('rz_touches_per_game', 'MISSING')}")
            
            print("\n" + "="*80)
            print("ALL AVAILABLE KEYS IN FIRST PLAYER:")
            print("="*80)
            for key in sorted(players[0].keys()):
                value = players[0][key]
                if value and value != 0 and value != "Unknown" and value != "FA":
                    print(f"  {key}: {value}")
            
    except Exception as e:
        print(f"\n{'='*80}")
        print(f"ERROR: {e}")
        print(f"{'='*80}")
        import traceback
        traceback.print_exc()
        
        print("\n\nDEBUGGING INFO:")
        print("Please check:")
        print("1. Is your league ID correct?")
        print("2. Is your league public or do you need authentication?")
        print("3. Check fantasy_extractor.py for the API call")

if __name__ == "__main__":
    main()
