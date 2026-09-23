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


@pytest.mark.asyncio
async def test_strength_of_schedule_playoff_weeks_from_league():
    sos = AsyncMock(return_value={"success": True})
    league = {"league": {"settings": {"playoff_week_start": 14, "playoff_teams": 4}}}
    with patch("nfl_mcp.sos_tools.get_strength_of_schedule", sos), \
         patch("nfl_mcp.sleeper_tools.get_league", AsyncMock(return_value=league)):
        out = await tool_registry.get_strength_of_schedule(season=2026, playoff_weeks=True, league_id="123")
        assert (sos.await_args.kwargs["start_week"], sos.await_args.kwargs["end_week"]) == (14, 15)
        assert out["window"] == "playoffs"
        await tool_registry.get_strength_of_schedule(season=2026, playoff_weeks=True)
        assert (sos.await_args.kwargs["start_week"], sos.await_args.kwargs["end_week"]) == (15, 17)
        await tool_registry.get_strength_of_schedule(season=2026, start_week=4, end_week=9)
        assert (sos.await_args.kwargs["start_week"], sos.await_args.kwargs["end_week"]) == (4, 9)


class _FakeDB:
    def search_athletes_by_name(self, name, limit=10):
        return [{"id": "4046", "full_name": "Bijan Robinson", "team_id": "ATL", "position": "RB"},
                {"id": "999", "full_name": "Bijan Robinson Sr", "team_id": None, "position": "RB"}]

    def get_athletes_by_ids(self, ids):
        return {}

    def get_usage_for_week(self, season, week):
        return [{"player_id": "4046", "snap_share": 71.5}] if week == 2 else []


@pytest.mark.asyncio
async def test_start_sit_looks_up_team_position_and_snaps():
    mock = AsyncMock(return_value={"success": True, "recommendation": {}})
    with patch("nfl_mcp.tool_registry.get_db", return_value=_FakeDB()), \
         patch("nfl_mcp.lineup_optimizer_tools.get_start_sit_recommendation", mock):
        out = await tool_registry.get_start_sit_recommendation(
            player_name="Bijan Robinson", season=2026, week=3)
    kw = mock.await_args.kwargs
    assert (kw["team"], kw["position"], kw["player_id"], kw["snap_percentage"]) == ("ATL", "RB", "4046", 71.5)
    assert out["resolved"]["team"] == "ATL"


@pytest.mark.asyncio
async def test_start_sit_list_mode_replaces_roster_recommendations():
    mock = AsyncMock(return_value={"success": True, "recommendations": []})
    with patch("nfl_mcp.tool_registry.get_db", return_value=_FakeDB()), \
         patch("nfl_mcp.lineup_optimizer_tools.get_roster_recommendations", mock):
        await tool_registry.get_start_sit_recommendation(
            players=["Bijan Robinson", {"name": "KC", "position": "DEF"}], season=2026, week=3)
    sent = mock.await_args.kwargs["players"]
    assert sent[0]["team"] == "ATL" and sent[0]["usage"] == {"snap_percentage": 71.5}
    assert (sent[1]["team"], sent[1]["position"]) == ("KC", "DEF")


@pytest.mark.asyncio
async def test_analyze_lineup_reads_the_set_lineup_from_the_league():
    from nfl_mcp import lineup_tools
    ctx = {"error": None, "roster_id": 7, "season": 2026, "week": 3,
           "league": {"name": "L", "roster_positions": ["QB", "RB", "FLEX", "BN", "BN"]},
           "roster": {"reserve": []}, "starters": ["1", "2", "0"],
           "players": [
               {"player_id": "1", "name": "Q", "position": "QB", "team": "KC", "opponent": "LV"},
               {"player_id": "2", "name": "R", "position": "RB", "team": "ATL", "opponent": "NO"},
               {"player_id": "3", "name": "W", "position": "WR", "team": "SF", "opponent": "BYE"},
           ]}
    grade = AsyncMock(return_value={"success": True, "lineup_grade": "B"})
    with patch("nfl_mcp.roster_context.load_roster_players", AsyncMock(return_value=ctx)), \
         patch("nfl_mcp.sleeper_tools.get_matchups", AsyncMock(return_value={"matchups": []})), \
         patch("nfl_mcp.lineup_optimizer_tools.analyze_full_lineup", grade):
        out = await lineup_tools.analyze_lineup("123", roster_id=7, db=None)
    lineup = grade.await_args.kwargs["lineup"]
    assert [p["name"] for p in lineup["QB"]] == ["Q"]
    assert [p["name"] for p in lineup["RB"]] == ["R"]
    assert [p["name"] for p in lineup["BENCH"]] == ["W"]
    assert out["empty_slots"] == ["FLEX"]
    assert out["lineup_grade"] == "B"
