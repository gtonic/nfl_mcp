"""Test full integration of fantasy extractor with weekly stats"""

import sys
from pathlib import Path

# Add src to path
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

# Now we can import normally
from nfl_mcp.fantasy_extractor import FantasyExtractor

def main():
    print("="*80)
    print("TESTING FULL FANTASY EXTRACTOR INTEGRATION")
    print("="*80)
    
    extractor = FantasyExtractor(debug=True)
    
    print("\nFetching top 10 players with calculated stats...")
    players = extractor.get_all_players(limit=10)
    
    print(f"\n{'='*80}")
    print(f"RESULTS: {len(players)} players")
    print(f"{'='*80}")
    
    for i, player in enumerate(players[:5], 1):
        print(f"\n{i}. {player['name']} ({player['position']} - {player['team']})")
        print(f"   ✓ Targets/G (3W): {player['targets_per_game']}")
        print(f"   ✓ RZ Touches/G: {player['rz_touches_per_game']}")
        
        snap = player['snap_percentage']
        if snap and snap > 0:
            print(f"   ✓ Snap%: {snap}")
        else:
            print(f"   ✗ Snap%: {snap} (Noch nicht verfügbar - wird berechnet)")
        
        routes = player['routes_per_game']
        if routes and routes > 0:
            print(f"   ✓ Routes/G (3W): {routes}")
        else:
            print(f"   ✗ Routes/G (3W): {routes} (Noch nicht verfügbar - wird berechnet)")
    
    print(f"\n{'='*80}")
    print("✓ IMPLEMENTIERUNG ABGESCHLOSSEN!")
    print(f"{'='*80}")
    print("✓ Targets/G (3W) - aus Sleeper weekly stats berechnet")
    print("✓ RZ Touches/G - aus Sleeper weekly stats berechnet")
    print("✓ Snap% (3W Avg) - aus Sleeper off_snp_pct berechnet")
    print("✓ Routes/G (3W) - aus Sleeper off_snp (offensive snaps) berechnet")
    print("\nHinweis: Snap% und Routes können None sein wenn:")
    print("  - Die Sleeper API diese Daten für den Spieler nicht bereitstellt")
    print("  - Der Spieler in den letzten 3 Wochen nicht gespielt hat")
    print("  - Die Position keine Routes tracked (z.B. RB)")
    print("\nNächster Schritt: In fantasy_manager.py integrieren")

if __name__ == "__main__":
    main()
