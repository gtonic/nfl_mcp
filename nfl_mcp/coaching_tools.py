"""
Coaching-related MCP tools for the NFL MCP Server.

This module contains MCP tools for fetching coaching staff data, 
coach records, and coaching tree information from ESPN API.
"""

import httpx
from typing import Optional, Dict, Any, List
import logging

from .config import get_http_headers, create_http_client, validate_limit, LIMITS
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors, handle_validation_error
)

logger = logging.getLogger(__name__)

# ESPN team ID mapping for abbreviation to numeric ID
# This is needed for the Core API endpoints that require numeric IDs
TEAM_ID_MAP = {
    "ARI": "22", "ATL": "1", "BAL": "33", "BUF": "2", "CAR": "29",
    "CHI": "3", "CIN": "4", "CLE": "5", "DAL": "6", "DEN": "7",
    "DET": "8", "GB": "9", "HOU": "34", "IND": "11", "JAX": "30",
    "KC": "12", "LV": "13", "LAC": "24", "LAR": "14", "MIA": "15",
    "MIN": "16", "NE": "17", "NO": "18", "NYG": "19", "NYJ": "20",
    "PHI": "21", "PIT": "23", "SF": "25", "SEA": "26", "TB": "27",
    "TEN": "10", "WAS": "28"
}


def _get_espn_team_id(team_id: str) -> str:
    """
    Convert team abbreviation to ESPN numeric team ID.
    
    Args:
        team_id: Team abbreviation (e.g., 'KC', 'NE') or numeric ID
        
    Returns:
        ESPN numeric team ID as string
    """
    team_upper = team_id.upper().strip()
    
    # If it's already numeric, return as-is
    if team_upper.isdigit():
        return team_upper
    
    # Look up in mapping
    return TEAM_ID_MAP.get(team_upper, team_upper)


def _classify_coach_role(role_name: str) -> Dict[str, Any]:
    """
    Classify a coach role into standard categories.
    
    Args:
        role_name: The raw role name from ESPN API
        
    Returns:
        Dictionary with role classification
    """
    role_lower = role_name.lower()
    
    if 'head coach' in role_lower:
        return {"category": "head_coach", "side": "both", "is_coordinator": False}
    elif 'offensive coordinator' in role_lower:
        return {"category": "coordinator", "side": "offense", "is_coordinator": True}
    elif 'defensive coordinator' in role_lower:
        return {"category": "coordinator", "side": "defense", "is_coordinator": True}
    elif 'special teams coordinator' in role_lower:
        return {"category": "coordinator", "side": "special_teams", "is_coordinator": True}
    elif 'quarterback' in role_lower or 'qb' in role_lower:
        return {"category": "position_coach", "side": "offense", "is_coordinator": False}
    elif any(pos in role_lower for pos in ['receiver', 'wr', 'tight end', 'te']):
        return {"category": "position_coach", "side": "offense", "is_coordinator": False}
    elif any(pos in role_lower for pos in ['running back', 'rb', 'offensive line', 'o-line']):
        return {"category": "position_coach", "side": "offense", "is_coordinator": False}
    elif any(pos in role_lower for pos in ['linebacker', 'lb', 'defensive line', 'd-line', 'secondary', 'corner', 'safety']):
        return {"category": "position_coach", "side": "defense", "is_coordinator": False}
    else:
        return {"category": "assistant", "side": "unknown", "is_coordinator": False}


@handle_http_errors(
    default_data={"team_id": None, "team_name": None, "coaches": [], "head_coach": None},
    operation_name="fetching coaching staff"
)
async def get_coaching_staff(team_id: str) -> dict:
    """
    Get the coaching staff for a specific NFL team from ESPN API.
    
    This tool fetches coaching information including head coach, coordinators,
    and position coaches from ESPN's Core API.
    
    Args:
        team_id: The team abbreviation (e.g., 'KC', 'TB', 'NE') or ESPN team ID
        
    Returns:
        A dictionary containing:
        - team_id: The team identifier used
        - team_name: The team's full name
        - coaches: List of all coaches with roles and details
        - head_coach: The head coach information (convenience field)
        - offensive_coordinator: The OC information if available
        - defensive_coordinator: The DC information if available
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate team_id
    if not team_id or not isinstance(team_id, str):
        return handle_validation_error(
            "Team ID is required and must be a string",
            {"team_id": team_id, "team_name": None, "coaches": [], "head_coach": None}
        )
    
    team_id_upper = team_id.upper().strip()
    espn_team_id = _get_espn_team_id(team_id_upper)
    
    headers = get_http_headers("nfl_teams")
    
    # ESPN Core API endpoint for team coaches
    url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{espn_team_id}/coaches"
    
    async with create_http_client() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return create_success_response({
                    "team_id": team_id_upper,
                    "team_name": None,
                    "coaches": [],
                    "head_coach": None,
                    "offensive_coordinator": None,
                    "defensive_coordinator": None,
                    "message": f"No coaching data found for team '{team_id}'. Team may not exist or data unavailable."
                })
            raise
        
        # Parse JSON response
        data = response.json()
        
        # ESPN Core API returns a list of coach references that need to be followed
        coach_refs = data.get('items', [])
        
        coaches = []
        head_coach = None
        offensive_coordinator = None
        defensive_coordinator = None
        team_name = None
        
        # Fetch details for each coach reference
        for coach_ref in coach_refs:
            ref_url = coach_ref.get('$ref', '')
            if ref_url:
                try:
                    coach_response = await client.get(ref_url, headers=headers)
                    coach_response.raise_for_status()
                    coach_data = coach_response.json()
                    
                    # Extract coach info
                    coach_info = {
                        "id": coach_data.get('id', ''),
                        "name": coach_data.get('displayName', coach_data.get('fullName', '')),
                        "first_name": coach_data.get('firstName', ''),
                        "last_name": coach_data.get('lastName', ''),
                        "role": coach_data.get('position', {}).get('name', 'Unknown'),
                        "experience": coach_data.get('experience', None),
                    }
                    
                    # Classify the role
                    role_info = _classify_coach_role(coach_info['role'])
                    coach_info.update(role_info)
                    
                    # Get team name if we don't have it yet
                    if not team_name:
                        team_ref = coach_data.get('team', {}).get('$ref', '')
                        if team_ref:
                            try:
                                team_response = await client.get(team_ref, headers=headers)
                                team_response.raise_for_status()
                                team_data = team_response.json()
                                team_name = team_data.get('displayName', team_data.get('name', ''))
                            except Exception:
                                pass
                    
                    coaches.append(coach_info)
                    
                    # Track key positions
                    if coach_info['category'] == 'head_coach':
                        head_coach = coach_info
                    elif 'offensive coordinator' in coach_info['role'].lower():
                        offensive_coordinator = coach_info
                    elif 'defensive coordinator' in coach_info['role'].lower():
                        defensive_coordinator = coach_info
                        
                except Exception as e:
                    logger.warning(f"Failed to fetch coach details from {ref_url}: {e}")
                    continue
        
        return create_success_response({
            "team_id": team_id_upper,
            "team_name": team_name,
            "coaches": coaches,
            "head_coach": head_coach,
            "offensive_coordinator": offensive_coordinator,
            "defensive_coordinator": defensive_coordinator,
            "total_coaches": len(coaches)
        })


@handle_http_errors(
    default_data={"teams": [], "total_teams": 0},
    operation_name="fetching all coaching staffs"
)
async def get_all_coaching_staffs() -> dict:
    """
    Get coaching staff information for all NFL teams.
    
    This tool fetches coaching information for all 32 NFL teams,
    returning a summary of each team's coaching staff.
    
    Returns:
        A dictionary containing:
        - teams: List of teams with their coaching staff summary
        - total_teams: Number of teams retrieved
        - success: Whether the request was successful
        - error: Error message (if any)
    """
    headers = get_http_headers("nfl_teams")
    
    teams_url = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams?limit=32"
    
    async with create_http_client() as client:
        # First, get all teams
        response = await client.get(teams_url, headers=headers)
        response.raise_for_status()
        
        teams_data = response.json()
        team_refs = teams_data.get('items', [])
        
        all_teams = []
        
        for team_ref in team_refs:
            team_url = team_ref.get('$ref', '')
            if not team_url:
                continue
                
            try:
                # Fetch team details
                team_response = await client.get(team_url, headers=headers)
                team_response.raise_for_status()
                team_info = team_response.json()
                
                team_id = team_info.get('abbreviation', team_info.get('id', ''))
                team_name = team_info.get('displayName', team_info.get('name', ''))
                
                # Fetch coaches for this team
                coaches_url = f"{team_url}/coaches"
                try:
                    coaches_response = await client.get(coaches_url, headers=headers)
                    coaches_response.raise_for_status()
                    coaches_data = coaches_response.json()
                    
                    coach_refs = coaches_data.get('items', [])
                    head_coach_name = None
                    
                    # Get head coach name
                    for coach_ref in coach_refs[:5]:  # Check first 5 coaches
                        ref_url = coach_ref.get('$ref', '')
                        if ref_url:
                            try:
                                coach_response = await client.get(ref_url, headers=headers)
                                coach_response.raise_for_status()
                                coach_data = coach_response.json()
                                role = coach_data.get('position', {}).get('name', '').lower()
                                if 'head coach' in role:
                                    head_coach_name = coach_data.get('displayName', coach_data.get('fullName', ''))
                                    break
                            except Exception:
                                continue
                    
                    all_teams.append({
                        "team_id": team_id,
                        "team_name": team_name,
                        "head_coach": head_coach_name,
                        "coach_count": len(coach_refs)
                    })
                    
                except Exception as e:
                    logger.warning(f"Failed to fetch coaches for team {team_id}: {e}")
                    all_teams.append({
                        "team_id": team_id,
                        "team_name": team_name,
                        "head_coach": None,
                        "coach_count": 0
                    })
                    
            except Exception as e:
                logger.warning(f"Failed to fetch team details: {e}")
                continue
        
        # Sort by team name
        all_teams.sort(key=lambda x: x.get('team_name', ''))
        
        return create_success_response({
            "teams": all_teams,
            "total_teams": len(all_teams)
        })


# Known coaching trees for major NFL coaching lineages
COACHING_TREES = {
    "Andy Reid": {
        "mentors": ["Mike Holmgren"],
        "proteges": ["Doug Pederson", "John Harbaugh", "Sean McDermott", "Todd Bowles", "Ron Rivera", "Matt Nagy", "Pat Shurmur"],
        "scheme_family": "West Coast Offense",
        "known_for": ["QB development", "Offensive innovation", "Player-friendly culture"]
    },
    "Bill Belichick": {
        "mentors": ["Bill Parcells"],
        "proteges": ["Nick Saban", "Romeo Crennel", "Eric Mangini", "Josh McDaniels", "Brian Flores", "Joe Judge", "Matt Patricia", "Jerod Mayo"],
        "scheme_family": "Erhardt-Perkins System",
        "known_for": ["Defensive schemes", "Situational football", "System flexibility"]
    },
    "Kyle Shanahan": {
        "mentors": ["Mike Shanahan", "Gary Kubiak"],
        "proteges": ["Mike McDaniel", "Robert Saleh", "DeMeco Ryans", "Kevin O'Connell"],
        "scheme_family": "Shanahan Wide Zone",
        "known_for": ["Run game creativity", "Play-action passing", "Motion/misdirection"]
    },
    "Sean McVay": {
        "mentors": ["Jay Gruden", "Kyle Shanahan"],
        "proteges": ["Zac Taylor", "Kevin O'Connell", "Brandon Staley", "Raheem Morris"],
        "scheme_family": "McVay Offense",
        "known_for": ["Pre-snap motion", "Quick passing game", "Young coach development"]
    },
    "Mike Tomlin": {
        "mentors": ["Tony Dungy", "Jon Gruden"],
        "proteges": ["Todd Bowles", "Mike Munchak"],
        "scheme_family": "Tampa 2 Defense",
        "known_for": ["Leadership", "Team culture", "Consistency"]
    },
    "Sean Payton": {
        "mentors": ["Bill Parcells", "Dan Reeves"],
        "proteges": ["Dan Campbell", "Dennis Allen", "Aaron Glenn", "Pete Carmichael Jr."],
        "scheme_family": "Erhardt-Perkins/West Coast Hybrid",
        "known_for": ["Offensive creativity", "QB development", "Aggressive play-calling"]
    }
}


async def get_coaching_tree(coach_name: str) -> dict:
    """
    Get coaching tree information for a known NFL coach.
    
    This tool provides information about a coach's mentors, proteges,
    and scheme family. Based on historical coaching lineage data.
    
    Args:
        coach_name: The coach's name (e.g., 'Andy Reid', 'Bill Belichick')
        
    Returns:
        A dictionary containing:
        - coach_name: The coach name queried
        - mentors: List of known mentors
        - proteges: List of known proteges/disciples
        - scheme_family: The offensive/defensive scheme family
        - known_for: What the coach is known for developing
        - found: Whether the coach was found in the database
        - success: Whether the request was successful
    """
    if not coach_name or not isinstance(coach_name, str):
        return handle_validation_error(
            "Coach name is required and must be a string",
            {"coach_name": coach_name, "found": False}
        )
    
    # Normalize coach name for lookup
    coach_name_normalized = coach_name.strip().title()
    
    # Look up in known coaching trees
    tree_data = COACHING_TREES.get(coach_name_normalized)
    
    if tree_data:
        return create_success_response({
            "coach_name": coach_name_normalized,
            "mentors": tree_data.get("mentors", []),
            "proteges": tree_data.get("proteges", []),
            "scheme_family": tree_data.get("scheme_family", "Unknown"),
            "known_for": tree_data.get("known_for", []),
            "found": True
        })
    
    # Check if the name appears as a protege in any tree
    for head_coach, data in COACHING_TREES.items():
        if coach_name_normalized in data.get("proteges", []):
            return create_success_response({
                "coach_name": coach_name_normalized,
                "mentors": [head_coach],
                "proteges": [],
                "scheme_family": data.get("scheme_family", "Unknown"),
                "known_for": [],
                "found": True,
                "note": f"Coach found as protege of {head_coach}"
            })
    
    return create_success_response({
        "coach_name": coach_name_normalized,
        "mentors": [],
        "proteges": [],
        "scheme_family": None,
        "known_for": [],
        "found": False,
        "message": f"Coach '{coach_name}' not found in coaching tree database. Available coaches: {list(COACHING_TREES.keys())}"
    })


# Scheme classifications for all 32 NFL teams
TEAM_SCHEMES = {
    "ARI": {"offense": "Spread/Air Raid", "defense": "3-4 Base"},
    "ATL": {"offense": "Shanahan Wide Zone", "defense": "3-4 Base"},
    "BAL": {"offense": "RPO/Power Run", "defense": "3-4 Base"},
    "BUF": {"offense": "Spread/West Coast", "defense": "4-3 Base"},
    "CAR": {"offense": "West Coast", "defense": "3-4 Base"},
    "CHI": {"offense": "West Coast", "defense": "4-3 Base"},
    "CIN": {"offense": "McVay/West Coast", "defense": "4-3 Base"},
    "CLE": {"offense": "Shanahan Wide Zone", "defense": "4-3 Base"},
    "DAL": {"offense": "Spread/Erhardt-Perkins", "defense": "3-4 Base"},
    "DEN": {"offense": "Shanahan Wide Zone", "defense": "3-4 Base"},
    "DET": {"offense": "McVay Offense", "defense": "4-3 Base"},
    "GB": {"offense": "West Coast/Spread", "defense": "3-4 Base"},
    "HOU": {"offense": "Shanahan Wide Zone", "defense": "4-3 Base"},
    "IND": {"offense": "West Coast", "defense": "4-3 Base"},
    "JAX": {"offense": "West Coast/Spread", "defense": "3-4 Base"},
    "KC": {"offense": "West Coast/Spread", "defense": "4-3 Base"},
    "LV": {"offense": "West Coast", "defense": "4-3 Base"},
    "LAC": {"offense": "Erhardt-Perkins", "defense": "3-4 Base"},
    "LAR": {"offense": "McVay Offense", "defense": "3-4 Base"},
    "MIA": {"offense": "Shanahan Wide Zone", "defense": "4-3 Base"},
    "MIN": {"offense": "McVay/Shanahan", "defense": "3-4 Base"},
    "NE": {"offense": "Erhardt-Perkins", "defense": "Multiple"},
    "NO": {"offense": "Erhardt-Perkins/West Coast", "defense": "4-3 Base"},
    "NYG": {"offense": "West Coast", "defense": "3-4 Base"},
    "NYJ": {"offense": "Shanahan Wide Zone", "defense": "4-3 Base"},
    "PHI": {"offense": "Shanahan Wide Zone", "defense": "Multiple"},
    "PIT": {"offense": "West Coast", "defense": "3-4 Base"},
    "SF": {"offense": "Shanahan Wide Zone", "defense": "4-3 Base"},
    "SEA": {"offense": "West Coast", "defense": "3-4 Base"},
    "TB": {"offense": "Coryell/Vertical", "defense": "3-4 Base"},
    "TEN": {"offense": "Power Run/Play Action", "defense": "3-4 Base"},
    "WAS": {"offense": "West Coast", "defense": "4-3 Base"}
}


async def get_scheme_classification(team_id: str) -> dict:
    """
    Get the offensive and defensive scheme classification for an NFL team.
    
    This tool provides the general scheme philosophy for a team's
    offense and defense, useful for player fit analysis.
    
    Args:
        team_id: The team abbreviation (e.g., 'KC', 'TB', 'NE')
        
    Returns:
        A dictionary containing:
        - team_id: The team identifier
        - offensive_scheme: The team's offensive scheme family
        - defensive_scheme: The team's defensive base alignment
        - scheme_notes: Additional notes about scheme tendencies
        - success: Whether the request was successful
    """
    if not team_id or not isinstance(team_id, str):
        return handle_validation_error(
            "Team ID is required and must be a string",
            {"team_id": team_id}
        )
    
    team_id_upper = team_id.upper().strip()
    
    scheme_data = TEAM_SCHEMES.get(team_id_upper)
    
    if scheme_data:
        # Generate scheme notes based on classification
        notes = []
        offense = scheme_data["offense"]
        defense = scheme_data["defense"]
        
        if "Shanahan" in offense:
            notes.append("Emphasizes outside zone running and play-action")
        if "McVay" in offense:
            notes.append("Heavy pre-snap motion and quick passing game")
        if "West Coast" in offense:
            notes.append("Short/intermediate passing, timing routes")
        if "Spread" in offense:
            notes.append("Multiple receiver sets, space creation")
        if "Erhardt-Perkins" in offense:
            notes.append("Concept-based playcalling, flexibility")
        
        if "3-4" in defense:
            notes.append("Two-gap technique, versatile edge rushers")
        if "4-3" in defense:
            notes.append("One-gap technique, penetrating defensive line")
        if "Multiple" in defense:
            notes.append("Situational base changes, versatile personnel")
        
        return create_success_response({
            "team_id": team_id_upper,
            "offensive_scheme": offense,
            "defensive_scheme": defense,
            "scheme_notes": notes,
            "found": True
        })
    
    return create_success_response({
        "team_id": team_id_upper,
        "offensive_scheme": None,
        "defensive_scheme": None,
        "scheme_notes": [],
        "found": False,
        "message": f"Team '{team_id}' not found in scheme database. Valid abbreviations: {list(TEAM_SCHEMES.keys())}"
    })
