#!/usr/bin/env python3
"""
Validation script to verify the tool_registry.py fix
"""

import sys
import ast

def validate_tool_registry():
    """Check that the tool_registry.py file has been properly fixed."""
    
    try:
        # Read the file content
        with open('/tmp/nfl_mcp/nfl_mcp/tool_registry.py', 'r') as f:
            content = f.read()
        
        print("File validation:")
        print(f"  File size: {len(content)} characters")
        
        # Check for key sections that were missing
        required_sections = [
            "get_vegas_lines",
            "get_game_environment", 
            "analyze_roster_vegas",
            "get_stack_opportunities",
            "get_coaching_staff",
            "get_all_coaching_staffs",
            "get_coaching_tree",
            "get_scheme_classification"
        ]
        
        missing_sections = []
        for section in required_sections:
            if section in content:
                print(f"  ✓ Found {section}")
            else:
                print(f"  ✗ Missing {section}")
                missing_sections.append(section)
        
        # Check for complete function definitions
        # Looking for the end of the file
        if "get_scheme_classification" in content[-1000:]:  # Check last 1000 chars
            print("  ✓ File appears complete")
        else:
            print("  ⚠ File may still be incomplete")
            
        # Check if it's syntactically valid Python
        try:
            ast.parse(content)
            print("  ✓ File is syntactically valid Python")
        except SyntaxError as e:
            print(f"  ✗ Syntax error: {e}")
            return False
            
        if missing_sections:
            print(f"\nMISSING COMPONENTS: {len(missing_sections)} sections")
            for sec in missing_sections:
                print(f"  - {sec}")
            return False
        else:
            print("\n✓ ALL COMPONENTS PRESENT")
            return True
            
    except Exception as e:
        print(f"ERROR during validation: {e}")
        return False

if __name__ == "__main__":
    success = validate_tool_registry()
    sys.exit(0 if success else 1)