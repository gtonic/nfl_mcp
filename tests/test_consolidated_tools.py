"""Merged MCP tools: the removed tools' behaviour lives on as parameters/sections."""
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import tool_registry

INJURIES = [
    {"player_name": "A", "team_id": "KC", "injury_status": "Out", "severity": 4,
     "confidence": 80, "date_reported": "2026-09-21T12:00:00Z"},
    {"player_name": "B", "team_id": "KC", "injury_status": "Questionable", "severity": 2,
     "confidence": 60, "date_reported": "2026-09-18T12:00:00Z"},
    {"player_name": "C", "team_id": "PHI", "injury_status": "IR", "severity": 5,
     "confidence": 80, "date_reported": "2026-09-10T12:00:00Z"},
]


async def _report(**kw):
    with patch("nfl_mcp.injury_service.get_injury_reports",
               AsyncMock(return_value=[dict(r) for r in INJURIES])) as m:
        out = await tool_registry.get_injury_report(include_practice=False, **kw)
    return out, m


@pytest.mark.asyncio
async def test_injury_report_min_confidence_replaces_high_confidence_tool():
    out, _ = await _report(min_confidence=70)
    assert [r["player_name"] for r in out["injuries"]] == ["A", "C"]
    assert out["filters"] == {"min_confidence": 70}


@pytest.mark.asyncio
async def test_injury_report_severity_and_since_filters():
    out, _ = await _report(severity=3, since="2026-09-15")
    assert [r["player_name"] for r in out["injuries"]] == ["A"]


@pytest.mark.asyncio
async def test_injury_report_teams_replace_team_injuries_tool():
    out, m = await _report(teams=["kc"], limit=1)
    assert m.await_args.kwargs["teams"] == ["KC"]
    assert out["total_injuries"] == 1
    # The deprecated alias still works.
    out, m = await _report(team_ids=["PHI"])
    assert m.await_args.kwargs["teams"] == ["PHI"]


LOG = {"success": True, "waiver_log": [
    {"transaction_id": "1", "adds": {"111": 3}, "drops": None},
    {"transaction_id": "2", "adds": {"222": 4}, "drops": {"111": 3}},
], "duplicates_found": [], "total_transactions": 3, "unique_transactions": 2,
    "deduplication_enabled": True, "failed_claims": [{"wanted": ["222"]}], "failed_claims_count": 1}
REENTRY = {"success": True, "re_entry_players": {"111": {"is_volatile": True}},
           "volatile_players": ["111"], "total_players_analyzed": 2}


async def _waivers(**kw):
    with patch("nfl_mcp.waiver_tools.get_waiver_log", AsyncMock(return_value=LOG)), \
         patch("nfl_mcp.waiver_tools.check_re_entry_status", AsyncMock(return_value=REENTRY)):
        return await tool_registry.get_waiver_log("123456", round=3, **kw)


@pytest.mark.asyncio
async def test_waiver_log_default_has_log_summary_and_re_entries():
    out = await _waivers()
    assert out["success"] is True
    assert len(out["waiver_log"]) == 2
    assert out["dashboard_summary"]["duplicates_removed"] == 1
    assert out["dashboard_summary"]["failed_claims"] == 1
    assert out["volatile_players"] == ["111"]


@pytest.mark.asyncio
async def test_waiver_log_sections_and_player_filter():
    out = await _waivers(sections=["re_entries"])
    assert "waiver_log" not in out and "dashboard_summary" not in out
    assert out["players_with_re_entries"] == 1
    out = await _waivers(player="222")
    assert [t["transaction_id"] for t in out["waiver_log"]] == ["2"]
    assert out["failed_claims_count"] == 1
    assert out["re_entry_players"] == {}
    out = await _waivers(sections=["bogus"])
    assert out["success"] is False


ROSTER_CTX = {"error": None, "roster_id": 7, "season": 2026, "week": 3, "players": [
    {"player_id": "1", "name": "QB One", "position": "QB", "team": "KC", "opponent": "LV",
     "starter": True, "slot": "QB"},
    {"player_id": "2", "name": "WR Bye", "position": "WR", "team": "SF", "opponent": "BYE",
     "starter": False, "slot": "BN"},
]}


@pytest.mark.asyncio
async def test_vegas_lines_team_and_roster_modes():
    lines = AsyncMock(return_value={"success": True, "games": [], "summary": []})
    env = AsyncMock(return_value={"success": True, "team": "KC", "implied_total": 27.5, "game": {}})
    roster = AsyncMock(return_value={"success": True, "analysis": [{"player": "QB One"}]})
    with patch("nfl_mcp.vegas_tools.get_vegas_lines", lines), \
         patch("nfl_mcp.vegas_tools.get_game_environment", env), \
         patch("nfl_mcp.vegas_tools.analyze_roster_vegas", roster), \
         patch("nfl_mcp.roster_context.load_roster_players", AsyncMock(return_value=ROSTER_CTX)):
        out = await tool_registry.get_vegas_lines(teams=["KC"], league_id="123456", roster_id=7)
    assert out["team_environments"]["KC"]["implied_total"] == 27.5
    assert "game" not in out["team_environments"]["KC"]
    sent = roster.await_args.kwargs["players"]
    assert [p["name"] for p in sent] == ["QB One"] and sent[0]["opponent"] == "LV"
    assert out["roster"]["on_bye"] == ["WR Bye"]
    assert out["roster"]["analysis"] == [{"player": "QB One"}]


@pytest.mark.asyncio
async def test_player_values_accepts_one_or_more_players():
    async def one(player_id=None, name=None, **kw):
        found = {"4046": "Bijan Robinson", None: None}.get(player_id) or (name if name == "Puka Nacua" else None)
        return {"value": {"name": found, "value": 9000} if found else None, "source": "fantasycalc"}
    with patch("nfl_mcp.player_values.get_player_value", side_effect=one):
        out = await tool_registry.get_player_values(players=["4046", "Puka Nacua", "Nobody"])
    assert [v["name"] for v in out["values"]] == ["Bijan Robinson", "Puka Nacua"]
    assert out["not_found"] == ["Nobody"]
