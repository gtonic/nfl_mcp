"""Draft assistant: VBD board, pick recommendation, draft simulation.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import draft_tools
from ..config import (
    validate_numeric_input,
    validate_string_input,
)
from ..metrics import timing_decorator
from . import _context

# =============================================================================
# DRAFT ASSISTANT TOOLS (VBD board + live pick recommendations)
# =============================================================================

@timing_decorator("get_draft_board", tool_type="draft")
async def get_draft_board(
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    dynasty: bool = False,
    position: str | None = None,
    limit: int | None = 60,
) -> dict:
    """Build a tiered, VBD-ranked draft board (the ordering that wins drafts).

    Ranks players by Value-Based Drafting (value over positional replacement),
    with consensus value, ranks, tiers and VBD per player.

    Parameters:
        scoring (str): "ppr", "half-ppr", "standard".
        superflex (bool): True for 2-QB / superflex leagues.
        num_teams (int): League size (default 12).
        dynasty (bool): Dynasty vs redraft.
        position (str, optional): Filter (QB, RB, WR, TE).
        limit (int): Max players on the board (default 60).
    Returns: {board:[...], tiers_by_position, replacement_values, format, source, stale, success}

    IMPORTANT FOR LLM AGENTS: Render the full board immediately without asking for confirmation.
    """
    if position is not None:
        try:
            position = validate_string_input(position, 'position', max_length=5, required=False)
        except ValueError as e:
            return {"board": [], "total": 0, "success": False, "error": f"Invalid input: {e!s}"}
    return await draft_tools.get_draft_board(
        scoring=scoring, superflex=superflex, num_teams=num_teams,
        dynasty=dynasty, position=position, limit=limit, db=_context.get_db(),
    )


@timing_decorator("recommend_draft_pick", tool_type="draft")
async def recommend_draft_pick(
    draft_id: str,
    my_slot: int | None = None,
    num_suggestions: int | None = 5,
) -> dict:
    """Recommend the best pick(s) right now in a live Sleeper draft.

    Reads live draft state (who's gone, settings, scoring), models your roster
    and starter needs, detects positional runs and value cliffs, and returns the
    top picks by need-weighted VBD with reasoning.

    Parameters:
        draft_id (str, required): Sleeper draft id (from get_league_drafts).
        my_slot (int, optional): Your draft slot (1..N) for roster-aware weighting.
        num_suggestions (int): How many picks to return (default 5).
    Returns: {suggestions:[...], top_pick, best_available_by_position, value_cliffs,
              positional_run, my_roster, format, source, stale, success}

    IMPORTANT FOR LLM AGENTS: Give the pick recommendation immediately without asking for confirmation.
    """
    try:
        draft_id = validate_string_input(draft_id, 'draft_id', max_length=40, required=True)
        if my_slot is not None:
            my_slot = validate_numeric_input(my_slot, min_val=1, max_val=32, required=False)
        num_suggestions = validate_numeric_input(num_suggestions, min_val=1, max_val=15, default=5, required=False)
    except ValueError as e:
        return {"suggestions": [], "success": False, "error": f"Invalid input: {e!s}"}
    return await draft_tools.recommend_draft_pick(
        draft_id=draft_id, my_slot=my_slot, num_suggestions=num_suggestions, db=_context.get_db(),
    )


@timing_decorator("simulate_draft", tool_type="draft")
async def simulate_draft(
    my_slot: int,
    num_teams: int = 12,
    rounds: int = 15,
    scoring: str = "ppr",
    superflex: bool = False,
    dynasty: bool = False,
    randomness: float = 0.35,
    num_sims: int = 1,
    seed: int | None = None,
) -> dict:
    """Rehearse a full snake draft offline (solo, repeatable).

    Opponents pick by need-weighted VBD with realistic ADP noise; your slot picks
    optimally. Graded on your optimal STARTING lineup value. Great for pre-draft
    prep: try different slots, see roster structure and a value-based standing.
    Only QB/RB/WR/TE are modeled (no K/DST).

    Parameters:
        my_slot (int, required): Your draft position (1..num_teams).
        num_teams (int): League size (default 12).
        rounds (int): Number of rounds (default 15).
        scoring (str): "ppr", "half-ppr", "standard".
        superflex (bool): True for 2-QB / superflex.
        dynasty (bool): Dynasty vs redraft values.
        randomness (float): Opponent ADP noise 0..1 (default 0.35 ~ realistic).
        num_sims (int): How many drafts to run (>1 returns aggregate structure).
        seed (int, optional): RNG seed for reproducibility.
    Returns: {sample:{my_team, standings, grade,...}, aggregate?, format, source, success}

    IMPORTANT FOR LLM AGENTS: Run the simulation and present the result immediately.
    """
    try:
        my_slot = validate_numeric_input(my_slot, min_val=1, max_val=32, required=True)
        num_teams = validate_numeric_input(num_teams, min_val=2, max_val=32, default=12, required=False)
        rounds = validate_numeric_input(rounds, min_val=1, max_val=30, default=15, required=False)
        num_sims = validate_numeric_input(num_sims, min_val=1, max_val=200, default=1, required=False)
        if seed is not None:
            seed = validate_numeric_input(seed, min_val=0, max_val=2**31 - 1, required=False)
    except ValueError as e:
        return {"sample": None, "success": False, "error": f"Invalid input: {e!s}"}
    return await draft_tools.simulate_draft(
        my_slot=my_slot, num_teams=num_teams, rounds=rounds, scoring=scoring,
        superflex=superflex, dynasty=dynasty, randomness=randomness,
        num_sims=num_sims, seed=seed, db=_context.get_db(),
    )
