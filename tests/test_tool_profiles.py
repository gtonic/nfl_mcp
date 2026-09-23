"""Tool profiles: how many tools each exposes, and no duplicate names.

The counts are pinned on purpose. A new tool should be a deliberate choice
about which profile it belongs to (and the README/AGENT.md counts updated),
not a silent growth of the surface an assistant has to route across.
"""
import asyncio
import json
from collections import Counter

import pytest

from nfl_mcp import tool_registry

EXPECTED = {"season": 55, "full": 72, "offseason": 43}


def expected(profile: str) -> int:
    """Pinned count; get_league_leaders is feature-flagged (full/offseason only)."""
    flagged = "get_league_leaders" in {t.__name__ for t in tool_registry._registered_tools()}
    return EXPECTED[profile] - (0 if flagged or profile == "season" else 1)

REMOVED = {
    "get_strategic_matchup_preview", "get_season_bye_week_coordination",
    "get_trade_deadline_analysis", "get_playoff_preparation_plan",
    "get_team_injuries", "get_high_confidence_injuries",
    "get_waiver_wire_dashboard", "check_re_entry_status",
    "get_game_environment", "analyze_roster_vegas",
    "project_player", "get_player_value", "get_playoff_sos",
    "get_roster_recommendations", "analyze_full_lineup", "get_matchup_difficulty",
}


@pytest.mark.parametrize("profile", sorted(EXPECTED))
def test_tool_count_per_profile(profile):
    assert len(tool_registry.get_all_tools(profile)) == expected(profile)


@pytest.mark.parametrize("profile", sorted(EXPECTED))
def test_no_two_tools_share_a_name(profile):
    names = [t.__name__ for t in tool_registry.get_all_tools(profile)]
    dupes = [n for n, c in Counter(names).items() if c > 1]
    assert not dupes


def test_removed_tools_are_gone_from_every_profile():
    for profile in EXPECTED:
        names = {t.__name__ for t in tool_registry.get_all_tools(profile)}
        assert not names & REMOVED, profile


def test_profile_membership():
    season = {t.__name__ for t in tool_registry.get_all_tools("season")}
    offseason = {t.__name__ for t in tool_registry.get_all_tools("offseason")}
    assert "get_bye_week_plan" in season and "get_weekly_briefing" in season
    assert not season & (tool_registry.DRAFT_TOOLS | tool_registry.COACHING_TOOLS
                         | tool_registry.ADMIN_TOOLS)
    assert "get_draft_board" in offseason and "get_coaching_staff" in offseason
    assert not offseason & tool_registry.IN_SEASON_TOOLS
    full = {t.__name__ for t in tool_registry.get_all_tools("full")}
    assert season | offseason <= full


def test_profile_sets_only_name_real_tools():
    real = {t.__name__ for t in tool_registry._registered_tools()}
    named = (tool_registry.DRAFT_TOOLS | tool_registry.COACHING_TOOLS | tool_registry.ADMIN_TOOLS
             | tool_registry.IN_SEASON_TOOLS | {"get_cbs_expert_picks"})
    assert named <= real


def test_profile_from_env_and_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("NFL_MCP_TOOL_PROFILE", "Offseason")
    assert tool_registry.tool_profile() == "offseason"
    assert len(tool_registry.get_all_tools()) == expected("offseason")
    monkeypatch.setenv("NFL_MCP_TOOL_PROFILE", "bogus")
    assert tool_registry.tool_profile() == "season"
    monkeypatch.delenv("NFL_MCP_TOOL_PROFILE")
    assert tool_registry.tool_profile() == "season"


def test_health_reports_profile_and_count(monkeypatch):
    from nfl_mcp.health import health_check

    monkeypatch.setenv("NFL_MCP_TOOL_PROFILE", "full")
    resp = asyncio.run(health_check())
    body = json.loads(resp.body)
    assert body["tools"] == {"profile": "full", "count": expected("full")}
