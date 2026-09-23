"""get_bye_week_plan: per-week lineup with byes, holes, and what to add."""
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import bye_week_tools

LEAGUE = {
    "name": "Test League",
    "roster_positions": ["QB", "RB", "RB", "WR", "FLEX", "BN", "BN", "BN"],
    "settings": {"playoff_week_start": 15, "trade_deadline": 11},
}
ROSTERS = [{"roster_id": 7, "owner_id": "u7", "players": ["q", "r1", "r2", "r3", "w1", "w2"],
            "reserve": [], "taxi": []}]


def _p(pid, pos, rate, byes=()):
    weekly = {w: (0.0 if w in byes else rate) for w in range(5, 18)}
    return {"player_id": pid, "player": pid.upper(), "position": pos, "team": "T" + pid,
            "per_game": rate, "weekly_points": weekly, "bye_weeks": list(byes),
            "injury_weeks": [], "injury_window": None}


ENTRIES = {
    "q": _p("q", "QB", 20),
    "r1": _p("r1", "RB", 15, byes=(7,)),
    "r2": _p("r2", "RB", 12, byes=(7,)),
    "r3": _p("r3", "RB", 6),
    "w1": _p("w1", "WR", 14),
    "w2": _p("w2", "WR", 9, byes=(9,)),
}
META = {"windows": {"regular": list(range(5, 15)), "playoff": [15, 16, 17],
                    "playoff_week_start": 15, "last_week": 17},
        "schedule_unknown_weeks": []}


async def _plan(**kw):
    with patch("nfl_mcp.sleeper_tools.get_league", AsyncMock(return_value={"league": LEAGUE})), \
         patch("nfl_mcp.sleeper_tools.get_rosters", AsyncMock(return_value={"rosters": ROSTERS})), \
         patch("nfl_mcp.ros.ros_for_ids", AsyncMock(return_value=(dict(ENTRIES), META))):
        return await bye_week_tools.get_bye_week_plan(
            "L", roster_id=7, week=5, season=2026, include_free_agents=False, db=object(), **kw)


@pytest.mark.asyncio
async def test_crunch_week_names_the_hole_and_position_to_add():
    res = await _plan(weeks_ahead=6)
    assert res["success"] is True
    assert [r["week"] for r in res["weeks"]] == [5, 6, 7, 8, 9, 10]
    week7 = next(r for r in res["weeks"] if r["week"] == 7)
    assert week7["status"] == "crunch"
    assert week7["starters_on_bye"] == 2
    assert week7["available_by_position"]["RB"] == 1
    # Two RB slots + FLEX, one RB and one spare WR: one slot stays empty.
    assert len(week7["holes"]) == 1
    assert week7["positions_to_add"] == ["RB"]
    assert 7 in res["crunch_weeks"]
    assert any(s.startswith("Week 7:") and "RB" in s for s in res["suggestions"])
    assert res["trade_deadline"]["deadline_week"] == 11


@pytest.mark.asyncio
async def test_full_weeks_are_ok_and_bye_cost_is_zero():
    res = await _plan(weeks_ahead=2)
    for row in res["weeks"]:
        assert row["status"] == "ok"
        assert row["bye_cost"] == 0
        assert row["holes"] == []


@pytest.mark.asyncio
async def test_bench_bye_is_not_a_crunch():
    res = await _plan(weeks_ahead=6)
    week9 = next(r for r in res["weeks"] if r["week"] == 9)
    # w2 starts in FLEX at full strength; r3 fills in. Costs 3 pts: below the bar.
    assert week9["status"] in ("thin", "ok")
    assert week9["holes"] == []


@pytest.mark.asyncio
async def test_unknown_roster_is_an_error():
    with patch("nfl_mcp.sleeper_tools.get_league", AsyncMock(return_value={"league": LEAGUE})), \
         patch("nfl_mcp.sleeper_tools.get_rosters", AsyncMock(return_value={"rosters": ROSTERS})):
        res = await bye_week_tools.get_bye_week_plan("L", roster_id=99, week=5, season=2026, db=object())
    assert res["success"] is False
