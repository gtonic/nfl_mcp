"""Rostered players with no NFL team (Kendre Miller, released 2026-11-07)."""
import json

from nfl_mcp import league_changes_tools as lc
from nfl_mcp import teamless
from nfl_mcp.waiver_target_tools import _pair_drop

ATHLETES = {
    "9757": {"full_name": "Kendre Miller", "position": "RB", "team_id": None,
             "raw": json.dumps({"team": None, "status": "Active"})},
    "4046": {"full_name": "Patrick Mahomes", "position": "QB", "team_id": "KC"},
    "CIN": {"full_name": None, "position": "DEF", "team_id": None},
    "old": {"full_name": "Raw Team Only", "position": "WR", "team_id": None,
            "raw": json.dumps({"team": "WAS"})},
}


class TestTeamless:
    def test_only_players_without_a_team(self):
        got = teamless.teamless_rostered(["9757", "4046", "CIN", "old", "missing"], ATHLETES)
        assert [t["name"] for t in got] == ["Kendre Miller"]
        assert got[0]["position"] == "RB" and not got[0]["in_reserve"]

    def test_reserve_is_marked(self):
        got = teamless.teamless_rostered(["9757"], ATHLETES, reserve=["9757"])
        assert got[0]["in_reserve"]

    def test_drop_shape(self):
        drop = teamless.as_drop(teamless.teamless_rostered(["9757"], ATHLETES)[0])
        assert drop["teamless"] and drop["projected_points"] == 0.0 and drop["kickoff"] is None


class TestWaiverPairing:
    def test_a_teamless_player_is_the_drop(self):
        target = {"player_id": "x", "name": "Audric Estime", "position": "RB",
                  "projected_points": 6.0, "value": 100, "ros_total": 40.0}
        players = [{"player_id": "4046", "name": "Patrick Mahomes", "position": "QB",
                    "projected_points": 20.0, "value": 5000, "ros_total": 150.0}]
        cut = teamless.teamless_rostered(["9757"], ATHLETES)
        drop, note = _pair_drop(target, players, {"QB": 1, "RB": 1}, set(), 0, set(), cut)
        assert drop["name"] == "Kendre Miller" and "no NFL team" in note
        # An open spot still needs no drop; a reserve-slot player is not offered.
        assert _pair_drop(target, players, {"QB": 1}, set(), 1, set(), cut)[0] is None
        stashed = teamless.teamless_rostered(["9757"], ATHLETES, reserve=["9757"])
        drop, _ = _pair_drop(target, players, {"QB": 1, "RB": 1}, set(), 0, set(), stashed)
        assert drop is None or drop["name"] != "Kendre Miller"


class TestLeagueChanges:
    def test_reported_with_role_weighted_importance(self):
        roles = {"9757": "my_bench", "4046": "my_starter", "opp": "opp_starter"}
        items = lc.teamless_items(roles, ATHLETES)
        assert [(i["kind"], i["player"], i["importance"]) for i in items] == [
            ("teamless", "Kendre Miller", lc.TEAMLESS_IMPORTANCE["my_bench"])]
        assert "drop him now" in items[0]["summary"]
        assert lc.teamless_items({"9757": "my_starter"}, ATHLETES)[0]["importance"] == 90

    def test_opponents_players_are_not_mine_to_drop(self):
        assert lc.teamless_items({"9757": "opp_starter"}, ATHLETES) == []
