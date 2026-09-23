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
