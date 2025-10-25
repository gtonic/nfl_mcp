"""Check which data sources are configured and available"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

def main():
    print("="*80)
    print("CHECKING DATA SOURCES IN CODEBASE")
    print("="*80)
    
    # Check for data source files
    src_path = Path(__file__).parent / "src" / "nfl_mcp"
    
    print("\n1. Python files in src/nfl_mcp/:")
    for file in sorted(src_path.glob("*.py")):
        print(f"   - {file.name}")
    
    # Check fantasy_extractor.py content
    extractor_file = src_path / "fantasy_extractor.py"
    if extractor_file.exists():
        print("\n2. Checking fantasy_extractor.py for API endpoints:")
        content = extractor_file.read_text()
        
        apis = {
            "Sleeper": "sleeper.app" in content.lower() or "sleeper api" in content.lower(),
            "ESPN": "espn.com" in content.lower() or "fantasy.espn" in content.lower(),
            "NFL NextGen": "nextgenstats" in content.lower() or "nfl.com/stats" in content.lower(),
            "PFF": "pff.com" in content.lower() or "profootballfocus" in content.lower(),
            "FantasyPros": "fantasypros" in content.lower(),
            "RotoWire": "rotowire" in content.lower(),
        }
        
        for api_name, found in apis.items():
            status = "✓ FOUND" if found else "✗ NOT FOUND"
            print(f"   {status}: {api_name}")
    
    # Check fantasy_manager.py
    manager_file = src_path / "fantasy_manager.py"
    if manager_file.exists():
        print("\n3. Checking fantasy_manager.py for data enrichment:")
        content = manager_file.read_text()
        
        # Look for methods that might enrich data
        methods_to_check = [
            "enrich_player_data",
            "fetch_advanced_stats",
            "get_snap_percentage",
            "get_targets",
            "get_routes",
            "get_red_zone",
        ]
        
        for method in methods_to_check:
            found = method in content
            status = "✓ FOUND" if found else "✗ NOT FOUND"
            print(f"   {status}: {method}()")
    
    print("\n4. Checking where the missing fields should come from:")
    print("   Based on the code, these fields are extracted in fantasy_extractor.py")
    print("   but Sleeper API doesn't provide them.")
    
    print("\n" + "="*80)
    print("RECOMMENDATIONS:")
    print("="*80)
    print("Option 1: Integrate NFL NextGen Stats API")
    print("   - Provides snap counts and percentages")
    print("   - Free but rate-limited")
    print("   - URL: https://www.nfl.com/stats/")
    
    print("\nOption 2: Use ESPN Fantasy API (if already integrated)")
    print("   - May provide some of these stats")
    print("   - Requires league authentication")
    
    print("\nOption 3: Use a paid service")
    print("   - FantasyPros API")
    print("   - RotoWire API")
    print("   - Pro Football Focus (PFF)")
    
    print("\nOption 4: Calculate 3-week averages manually")
    print("   - Fetch weekly stats from Sleeper")
    print("   - Calculate rolling 3-week averages yourself")
    
    # Check if we can find where these fields are supposed to be populated
    print("\n5. Searching for field population logic:")
    for py_file in src_path.glob("*.py"):
        content = py_file.read_text()
        if "snap_percentage" in content or "targets_per_game" in content:
            print(f"\n   Found in {py_file.name}:")
            lines = content.split('\n')
            for i, line in enumerate(lines, 1):
                if any(field in line for field in ['snap_percentage', 'targets_per_game', 'routes_per_game', 'rz_touches']):
                    print(f"      Line {i}: {line.strip()[:80]}")

if __name__ == "__main__":
    main()
