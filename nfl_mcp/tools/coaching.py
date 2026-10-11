"""Coaching staff, coaching trees and scheme classification.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import coaching_tools
from ..config import validate_string_input
from ..metrics import timing_decorator

# =============================================================================
# COACHING INTELLIGENCE TOOLS
# =============================================================================

@timing_decorator("get_coaching_staff", tool_type="nfl")
async def get_coaching_staff(team_id: str) -> dict:
    """Get coaching staff for a specific NFL team from ESPN API.

    Parameters:
        team_id (str, required): Team abbreviation (e.g. 'KC', 'NE', 'DAL').
    Returns: {team_id, team_name, coaches:[...], head_coach, offensive_coordinator, defensive_coordinator, total_coaches, success, error?}
    Example: get_coaching_staff(team_id="KC")
    """
    try:
        team_id = validate_string_input(team_id, 'team_id', max_length=10, required=True)
        return await coaching_tools.get_coaching_staff(team_id)
    except ValueError as e:
        return {"team_id": team_id, "team_name": None, "coaches": [], "head_coach": None, "success": False, "error": str(e)}


@timing_decorator("get_all_coaching_staffs", tool_type="nfl")
async def get_all_coaching_staffs() -> dict:
    """Get coaching staff information for all 32 NFL teams.

    Returns: {teams:[{team_id, team_name, head_coach, coach_count}...], total_teams, success, error?}
    Example: get_all_coaching_staffs()
    """
    return await coaching_tools.get_all_coaching_staffs()


@timing_decorator("get_coaching_tree", tool_type="nfl")
async def get_coaching_tree(coach_name: str) -> dict:
    """Get coaching tree information for a known NFL coach.

    Parameters:
        coach_name (str, required): Coach's full name (e.g. 'Andy Reid', 'Bill Belichick').
    Returns: {coach_name, mentors:[...], proteges:[...], scheme_family, known_for:[...], found, success, error?}
    Example: get_coaching_tree(coach_name="Andy Reid")
    """
    try:
        coach_name = validate_string_input(coach_name, 'coach_name', max_length=100, required=True)
        return await coaching_tools.get_coaching_tree(coach_name)
    except ValueError as e:
        return {"coach_name": coach_name, "found": False, "success": False, "error": str(e)}


@timing_decorator("get_scheme_classification", tool_type="nfl")
async def get_scheme_classification(
    team_id: str, season: int | None = None, use_live_staff: bool = True
) -> dict:
    """Get offensive and defensive scheme classification for an NFL team.

    Resolved from the team's CURRENT staff — a scheme belongs to the play-caller,
    not the franchise — so a coordinator change is picked up automatically. Check
    `offense.source`/`defense.source`: `coach` means it was read off the named,
    live-fetched coach; `team_table` means it is a dated guess (see `as_of` and
    `warnings`) and may be a regime out of date.

    Parameters:
        team_id (str, required): Team abbreviation (e.g. 'KC', 'NE', 'DAL').
        season (int, optional): Season for the staff lookup (default: current).
        use_live_staff (bool, default True): False skips the network call.
    Returns: {team_id, offensive_scheme, defensive_scheme, offense{scheme,source,
        attributed_to}, defense{...}, head_coach, scheme_notes:[...], found,
        is_fallback, as_of, warnings:[...], success, error?}
    Example: get_scheme_classification(team_id="SF")
    """
    try:
        team_id = validate_string_input(team_id, 'team_id', max_length=10, required=True)
        return await coaching_tools.get_scheme_classification(
            team_id, season=season, use_live_staff=use_live_staff
        )
    except ValueError as e:
        return {"team_id": team_id, "found": False, "success": False, "error": str(e)}
