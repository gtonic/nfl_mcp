"""Waiver and trade tools must know who is hurt.

`get_waiver_targets` and `find_trade_targets` built their projection inputs
with no injury, so every player projected at full health: Jayden Daniels (Out)
set the bar a waiver claim had to clear, and an Out free agent could rank as
an upgrade. `analyze_trade` only warned on a DNP practice code.

A one-week projection of an Out player is zero, which is no better for a drop
or a trade — so those decisions leave such players out rather than act on it.
"""
import json
from unittest.mock import patch

import pytest

from nfl_mcp.injury_match import build_injury_index, misses_this_week
from nfl_mcp.trade_finder_tools import _projection_input as trade_input
from nfl_mcp.trade_finder_tools import _top_candidates
from nfl_mcp.waiver_target_tools import _to_projection_input as waiver_input

OPPONENTS = {"LV": "NO", "WSH": "SEA", "KC": "MIA"}


def _row(pid, name, team, position="TE", sleeper_status=None):
    return {"id": pid, "full_name": name, "team_id": team, "position": position,
            "raw": json.dumps({"injury_status": sleeper_status})}


def _index(*reports):
    return build_injury_index([
        {"player_id": f"e{i}", "player_name": n, "team_id": t, "injury_status": s}
        for i, (n, t, s) in enumerate(reports)
    ])


class TestProjectionInputs:
    @pytest.mark.parametrize("build", [waiver_input, trade_input])
    def test_report_status_reaches_the_projection(self, build):
        index = _index(("Brock Bowers", "LV", "Out"))
        got = build(_row("11604", "Brock Bowers", "LV"), OPPONENTS, index)
        assert got["injury"] == {"status": "Out"}

    @pytest.mark.parametrize("build", [waiver_input, trade_input])
    def test_sleeper_status_without_a_report(self, build):
        got = build(_row("11566", "Jayden Daniels", "WSH", "QB", "Out"), OPPONENTS, {})
        assert got["injury"] == {"status": "Out"}

    @pytest.mark.parametrize("build", [waiver_input, trade_input])
    def test_healthy_player_carries_no_injury(self, build):
        got = build(_row("1", "Healthy Guy", "KC", "WR"), OPPONENTS, {})
        assert "injury" not in got


class TestMissesThisWeek:
    @pytest.mark.parametrize("status", ["Out", "Doubtful", "IR", "Sus", "PUP"])
    def test_statuses_that_rule_a_player_out(self, status):
        assert misses_this_week(status)

    @pytest.mark.parametrize("status", [None, "", "Active", "Questionable"])
    def test_statuses_that_do_not(self, status):
        assert not misses_this_week(status)


class TestTradeCandidates:
    def test_injured_players_are_not_trade_candidates(self):
        players = [
            {"name": "Bowers", "projected_points": 0.0, "injury_status": "Out"},
            {"name": "Daniels", "projected_points": 6.0, "injury_status": "Doubtful"},
            {"name": "Evans", "projected_points": 6.4, "injury_status": "Questionable"},
            {"name": "Gibbs", "projected_points": 23.9, "injury_status": None},
        ]
        assert [p["name"] for p in _top_candidates(players)] == ["Gibbs", "Evans"]


class TestTradeAnalyzerWarning:
    @pytest.mark.asyncio
    async def test_an_out_player_is_named_in_the_warnings(self):
        from nfl_mcp.trade_analyzer_tools import analyze_trade

        class _Values:
            async def get_values(self, *a, **k):
                return {}

            def lookup(self, index, player_id=None, name=None, position=None):
                return {"value": 6000, "overall_rank": 5, "position_rank": 2}

        rosters = [
            {"roster_id": 1, "starters_enriched": [], "players_enriched": [
                {"player_id": "1", "full_name": "Brock Bowers", "position": "TE",
                 "injury_status": "Out", "injury_type": "Knee", "practice_status": "FP"}]},
            {"roster_id": 2, "starters_enriched": [], "players_enriched": [
                {"player_id": "2", "full_name": "Player 2", "position": "TE"}]},
        ]

        async def _rosters(league_id):
            return {"success": True, "rosters": rosters}

        async def _trending(*a):
            return {"success": True, "trending_players": []}

        async def _league(league_id):
            return {"success": True, "league": {"scoring_settings": {"rec": 0.5},
                    "roster_positions": ["QB", "TE"], "total_rosters": 10,
                    "settings": {"type": 0}}}

        with patch("nfl_mcp.trade_analyzer_tools.get_rosters", side_effect=_rosters), \
             patch("nfl_mcp.trade_analyzer_tools.get_league", side_effect=_league), \
             patch("nfl_mcp.trade_analyzer_tools.get_trending_players", side_effect=_trending), \
             patch("nfl_mcp.trade_analyzer_tools.get_values_service", return_value=_Values()):
            result = await analyze_trade("league1", 1, 2, ["1"], ["2"])

        assert any("Brock Bowers is listed Out (Knee)" in w for w in result["warnings"])
