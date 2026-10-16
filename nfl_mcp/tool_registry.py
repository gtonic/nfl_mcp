"""Tool registry for the NFL MCP Server: which tools exist, in which order,
and which are exposed per profile (``NFL_MCP_TOOL_PROFILE``).

The tool functions themselves live in ``nfl_mcp.tools.<domain>`` (thin,
validated wrappers around the service modules); this module only assembles
them. Every tool is re-exported here, so ``from nfl_mcp.tool_registry import
get_rosters`` keeps working.

Database access uses a ContextVar for async-safe dependency injection (see
``nfl_mcp.tools._context``); ``initialize_shared`` / ``get_db`` are re-exported.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from .config import FEATURE_LEAGUE_LEADERS

# Re-exported (``X as X``): the DB context API used by server.py, health.py and tests.
from .tools._context import _current_season_week as _current_season_week
from .tools._context import _db_token as _db_token
from .tools._context import get_db as get_db
from .tools._context import initialize_shared as initialize_shared
from .tools.athletes import (
    fetch_athletes,
    get_athletes_by_team,
    lookup_athlete,
    refresh_data,
    search_athletes,
)
from .tools.coaching import (
    get_all_coaching_staffs,
    get_coaching_staff,
    get_coaching_tree,
    get_scheme_classification,
)
from .tools.draft import (
    get_draft_board,
    recommend_draft_pick,
    simulate_draft,
)
from .tools.injury import (
    get_gameday_inactives,
    get_injury_report,
    get_injury_trends,
)
from .tools.league import (
    fetch_all_players,
    get_bye_week_plan,
    get_draft,
    get_draft_picks,
    get_draft_traded_picks,
    get_fantasy_context,
    get_league,
    get_league_drafts,
    get_league_users,
    get_matchups,
    get_nfl_state,
    get_playoff_bracket,
    get_playoff_odds,
    get_rosters,
    get_traded_picks,
    get_transactions,
    get_trending_players,
    get_user,
    get_user_leagues,
)
from .tools.lineup import (
    analyze_lineup,
    compare_players_for_slot,
    get_lineup_contingencies,
    get_start_sit_recommendation,
    get_win_probability_lineup,
)
from .tools.matchup import (
    analyze_opponent,
    analyze_roster_matchups,
    get_defense_rankings,
    get_stack_opportunities,
    get_streaming_options,
    get_strength_of_schedule,
    get_vegas_lines,
    get_weather_forecast,
)
from .tools.news import (
    crawl_url,
    get_cbs_expert_picks,
    get_cbs_player_news,
    get_cbs_projections,
    get_player_news,
)
from .tools.nfl_info import (
    fetch_teams,
    get_depth_chart,
    get_nfl_news,
    get_nfl_standings,
    get_team_player_stats,
    get_team_schedule,
    get_teams,
)
from .tools.projection import (
    get_opportunity_projections,
    get_ros_projections,
    get_usage_trends,
    project_players,
)
from .tools.reports import (
    get_league_changes,
    get_projection_accuracy,
    get_weekly_briefing,
    get_weekly_retro,
    get_weekly_signal_review,
)
from .tools.trade import (
    analyze_trade,
    find_trade_targets,
    get_player_values,
    get_trade_market,
)
from .tools.waiver import (
    audit_ir_slots,
    get_handcuff_map,
    get_player_ownership,
    get_waiver_log,
    get_waiver_targets,
    plan_roster_moves,
    recommend_faab_bid,
)

if FEATURE_LEAGUE_LEADERS:
    from .tools.nfl_info import get_league_leaders

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool profiles (NFL_MCP_TOOL_PROFILE). Too many overlapping tools hurt an
# assistant's tool routing, so the in-season default hides what a weekly
# check-in never needs. `full` registers everything.
# ---------------------------------------------------------------------------
TOOL_PROFILES = ("season", "full", "offseason")
DEFAULT_TOOL_PROFILE = "season"

DRAFT_TOOLS = frozenset({
    "get_league_drafts", "get_draft", "get_draft_picks", "get_draft_traded_picks",
    "get_traded_picks", "get_draft_board", "recommend_draft_pick", "simulate_draft",
})
COACHING_TOOLS = frozenset({
    "get_coaching_staff", "get_all_coaching_staffs", "get_coaching_tree",
    "get_scheme_classification",
})
# Cache refreshes the prefetch loop already runs.
ADMIN_TOOLS = frozenset({"fetch_athletes", "fetch_all_players", "fetch_teams"})
# Rarely the right answer to a fantasy question.
NICHE_TOOLS = frozenset({"get_league_leaders", "get_cbs_expert_picks"})
# Only meaningful while games are being played.
IN_SEASON_TOOLS = frozenset({
    "get_weekly_briefing", "get_weekly_retro", "get_projection_accuracy",
    "get_weekly_signal_review", "get_league_changes", "get_bye_week_plan",
    "get_playoff_odds", "get_playoff_bracket", "get_matchups", "get_waiver_targets",
    "recommend_faab_bid", "get_waiver_log", "audit_ir_slots", "plan_roster_moves",
    "get_start_sit_recommendation",
    "compare_players_for_slot", "analyze_lineup", "get_lineup_contingencies",
    "get_win_probability_lineup",
    "get_gameday_inactives", "get_streaming_options", "get_weather_forecast",
    "analyze_opponent", "analyze_roster_matchups", "get_stack_opportunities",
    "get_vegas_lines", "find_trade_targets", "get_trade_market", "get_opportunity_projections",
    "get_usage_trends",
})

_PROFILE_HIDDEN = {
    "season": DRAFT_TOOLS | COACHING_TOOLS | ADMIN_TOOLS | NICHE_TOOLS,
    "offseason": IN_SEASON_TOOLS | ADMIN_TOOLS | {"get_cbs_expert_picks"},
    "full": frozenset(),
}


def tool_profile(profile: str | None = None) -> str:
    """The active profile: `profile`, else NFL_MCP_TOOL_PROFILE, else season."""
    import os

    raw = (profile or os.getenv("NFL_MCP_TOOL_PROFILE") or DEFAULT_TOOL_PROFILE).strip().lower()
    if raw not in TOOL_PROFILES:
        logger.warning(f"Unknown NFL_MCP_TOOL_PROFILE={raw!r}; using {DEFAULT_TOOL_PROFILE!r} "
                       f"(valid: {', '.join(TOOL_PROFILES)})")
        return DEFAULT_TOOL_PROFILE
    return raw


def get_all_tools(profile: str | None = None) -> list[Callable]:
    """Tool functions to register with FastMCP for a profile (see TOOL_PROFILES).

    `profile` defaults to NFL_MCP_TOOL_PROFILE, else "season".
    """
    hidden = _PROFILE_HIDDEN[tool_profile(profile)]
    return [t for t in _registered_tools() if t.__name__ not in hidden]


def _registered_tools() -> list[Callable]:
    """Every tool, in registration order (the `full` profile)."""
    tools = [
        # NFL News and Info
        get_nfl_news,
        get_teams,
        fetch_teams,
        get_depth_chart,
        get_team_player_stats,
        get_nfl_standings,
        get_team_schedule,

        # Player news (ESPN fantasy feed, NBC Sports / Rotoworld, CBS)
        get_player_news,

        # CBS Fantasy Tools
        get_cbs_player_news,
        get_cbs_projections,
        get_cbs_expert_picks,

        # Web Tools
        crawl_url,

        # Athlete Tools
        fetch_athletes,
        lookup_athlete,
        search_athletes,
        get_athletes_by_team,
        refresh_data,

        # Sleeper API Tools - Basic
        get_league,
        get_rosters,
        get_league_users,
        get_matchups,
        get_playoff_bracket,
        get_transactions,
        get_traded_picks,
        get_nfl_state,
        get_trending_players,
    get_fantasy_context,
        get_weekly_briefing,
        get_weekly_retro,
        get_projection_accuracy,
        get_weekly_signal_review,
        get_league_changes,

        # Season planning
        get_bye_week_plan,
        get_playoff_odds,

    # Sleeper Additional Core Endpoints
    get_user,
    get_user_leagues,
    get_league_drafts,
    get_draft,
    get_draft_picks,
    get_draft_traded_picks,
    fetch_all_players,

        # Waiver Wire Analysis Tools (New from main)
        get_waiver_log,
        get_waiver_targets,
        get_player_ownership,
        audit_ir_slots,
        plan_roster_moves,
        recommend_faab_bid,
        get_handcuff_map,

        # Trade Analyzer Tools
        analyze_trade,
        find_trade_targets,
        get_trade_market,

        # Player Value Tools (real market-consensus values)
        get_player_values,

        # Draft Assistant Tools (VBD board + live pick recommendations)
        get_draft_board,
        recommend_draft_pick,
        simulate_draft,

        # Projection Tools (transparent weekly projections)
        project_players,
        get_opportunity_projections,
        get_ros_projections,
        get_usage_trends,

        # Opponent Analysis Tools
        analyze_opponent,

        # Matchup Analysis Tools (Lineup Optimization)
        get_defense_rankings,
        analyze_roster_matchups,

        # Strength-of-Schedule Tools (ROS / playoff-week planning)
        get_strength_of_schedule,

        # Streaming Planner (weekly DST/K/QB/TE matchup lookahead)
        get_streaming_options,

        # Weather / wind (game-environment analysis)
        get_weather_forecast,

        # Lineup Optimizer Tools (Start/Sit Recommendations)
        get_start_sit_recommendation,
        compare_players_for_slot,
        analyze_lineup,
        get_lineup_contingencies,
        get_win_probability_lineup,

        # Vegas Lines Tools (Game Environment Analysis)
        get_vegas_lines,
        get_stack_opportunities,

        # Injury Report Tools (ESPN reports + practice + timeline)
        get_injury_report,
        get_injury_trends,
        get_gameday_inactives,

        # Coaching Intelligence Tools
        get_coaching_staff,
        get_all_coaching_staffs,
        get_coaching_tree,
        get_scheme_classification,
    ]

    # Add feature-flagged tools
    if FEATURE_LEAGUE_LEADERS:
        tools.append(get_league_leaders)

    return tools


