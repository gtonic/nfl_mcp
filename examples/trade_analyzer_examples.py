#!/usr/bin/env python3
"""
Trade Analyzer Usage Examples

This file demonstrates how to use the analyze_trade tool in various scenarios.
"""

import asyncio
from nfl_mcp.tool_registry import analyze_trade


async def example_3_interpretation():
    """Example 3: How to interpret results."""
    print("\n" + "=" * 60)
    print("Example 3: Interpreting Trade Analysis Results")
    print("=" * 60)
    print()
    
    print("Fairness Score Guide:")
    print("  90-100: Perfectly balanced trade")
    print("  75-89:  Fair trade with slight advantage")
    print("  60-74:  Needs adjustment, moderate imbalance")
    print("  <60:    Significantly unfair")
    print()
    
    print("Recommendation Types:")
    print("  'fair': Both teams benefit equally")
    print("  'slightly_favors_team_1': Team 1 gets slightly better value")
    print("  'slightly_favors_team_2': Team 2 gets slightly better value")
    print("  'needs_adjustment': Trade is imbalanced, consider modifying")
    print("  'unfair': Significant value discrepancy")
    print()
    
    print("Value Factors Considered:")
    print("  • Position scarcity (RB > TE > WR > QB)")
    print("  • Practice status (DNP = -30%, LP = -15%, Full = +5%)")
    print("  • Usage trends (Up = +15%, Down = -15%)")
    print("  • Snap percentage (>80% = +10%, <30% = -20%)")
    print("  • Trending status (Hot pickups get bonus value)")
    print("  • Team positional needs (Adjust value based on roster depth)")
    print()


async def example_4_typical_workflow():
    """Example 4: Typical trade evaluation workflow."""
    print("\n" + "=" * 60)
    print("Example 4: Typical Trade Evaluation Workflow")
    print("=" * 60)
    print()
    
    print("Step 1: Get trade proposal")
    print("  → Someone offers you a trade in your league")
    print()
    
    print("Step 2: Identify roster IDs and player IDs")
    print("  → Use get_rosters() to find roster IDs")
    print("  → Use get_rosters() to find player IDs in enriched data")
    print()
    
    print("Step 3: Run trade analysis")
    print("  → Call analyze_trade() with the trade details")
    print("  → Review fairness score and recommendation")
    print()
    
    print("Step 4: Review detailed analysis")
    print("  → Check team1_analysis and team2_analysis")
    print("  → Review positional_needs for both teams")
    print("  → Read warnings for injury/depth concerns")
    print()
    
    print("Step 5: Make informed decision")
    print("  → Accept if fair and fills positional needs")
    print("  → Counter-offer if needs adjustment")
    print("  → Reject if significantly unfair")
    print()


async def main():
    """Run all examples."""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 10 + "TRADE ANALYZER - USAGE EXAMPLES" + " " * 16 + "║")
    print("╚" + "=" * 58 + "╝")
    
    # Run examples
    await example_3_interpretation()
    await example_4_typical_workflow()
    
    print("\n" + "=" * 60)
    print("For detailed API documentation, see API_DOCS.md")
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(main())
