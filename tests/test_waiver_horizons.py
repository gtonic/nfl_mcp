"""get_waiver_targets and recommend_faab_bid judge a claim on the same two horizons."""
import pytest

from nfl_mcp import faab_tools as ft
from nfl_mcp import ros, waiver_target_tools
from nfl_mcp.waiver_rules import horizon_worth
from nfl_mcp.waiver_target_tools import get_waiver_targets
from tests.test_waiver_targets import LEAGUE, _stub_sleeper, db  # noqa: F401


class TestHorizonWorth:
    def test_a_one_week_pickup_is_this_week_only(self):
        w = horizon_worth(4.0, 3.0, 15)
        assert w["worth"] == "high" and w["ros_worth"] == "low"
        assert w["this_week_only"] is True and w["driven_by"] == "this_week"

    def test_rest_of_season_value_survives_a_late_claim(self):
        w = horizon_worth(0.5, 30.0, 15)
        assert w["worth"] == "high" and w["ros_gain_per_week"] == 2.0
        assert w["this_week_only"] is False and w["driven_by"] == "rest_of_season"

    def test_equal_horizons(self):
        w = horizon_worth(1.6, 12.0, 15)
        assert w["worth"] == "medium" and w["driven_by"] == "both"
        assert w["this_week_only"] is False

    def test_without_ros_it_is_this_week(self):
        w = horizon_worth(2.0, None, None)
        assert w["worth"] == "medium" and w["this_week_only"] is True

    def test_nothing_known(self):
        assert horizon_worth(None, None, None)["worth"] is None


def _entry(pid, position, weekly):
    return {"player_id": pid, "position": position, "weekly_points": weekly,
            "total_points": sum(weekly.values())}


WEEKS = list(range(3, 18))


def _ros_mock(later_points: float):
    """My three players, and Free Good RB worth 12 this week, `later_points` after."""
    entries = {
        "m1": _entry("m1", "WR", dict.fromkeys(WEEKS, 14.0)),
        "m2": _entry("m2", "WR", dict.fromkeys(WEEKS, 9.0)),
        "m3": _entry("m3", "RB", dict.fromkeys(WEEKS, 3.0)),
        "f1": _entry("f1", "RB", {w: (12.0 if w == 3 else later_points) for w in WEEKS}),
    }

    async def _ros(ids, **_k):
        return ({i: entries[i] for i in ids if i in entries},
                {"windows": {"regular": WEEKS[:12], "playoff": WEEKS[12:]}})
    return _ros


def _spy(monkeypatch, module):
    calls = []
    real = module.priority_strategy

    def _capture(*a, **k):
        calls.append(k)
        return real(*a, **k)
    monkeypatch.setattr(module, "priority_strategy", _capture)
    return calls


class TestWaiverTargetsReportBothHorizons:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("later,this_week_only", [(0.0, True), (12.0, False)])
    async def test_claim_worth_and_strategy(self, db, monkeypatch, later, this_week_only):  # noqa: F811
        monkeypatch.setattr(ros, "ros_for_ids", _ros_mock(later))
        calls = _spy(monkeypatch, waiver_target_tools)
        _stub_sleeper(monkeypatch, {"My Starter WR": 14.0, "My Second WR": 9.0,
                                    "My Weak RB": 3.0, "Free Good RB": 12.0})
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        target = next(t for t in out["targets"] if t["name"] == "Free Good RB")
        worth = target["claim_worth"]
        assert worth["week_gain"] == 12.0
        assert worth["ros_gain"] == target["ros_gain"]
        assert worth["this_week_only"] is this_week_only
        assert out["horizons_reported"] == ["this_week", "rest_of_season"]
        call = next(c for c in calls if c["worth"] == worth["worth"])
        assert call["this_week"] is this_week_only


class _Values:
    async def get_values(self, *a, **k):
        return {"list": [{"player_id": "f1", "name": "Free Good RB", "position": "RB",
                          "value": 3000, "position_rank": 20}]}

    def lookup(self, idx, player_id=None, name=None, position=None):
        row = idx["list"][0]
        return row if str(player_id) == "f1" or name == "Free Good RB" else None


class TestFaabUsesTheSameRule:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("later", [0.0, 12.0])
    async def test_same_worth_and_horizon_as_waiver_targets(self, monkeypatch, later):
        monkeypatch.setattr(ros, "ros_for_ids", _ros_mock(later))
        calls = _spy(monkeypatch, ft)

        async def _league(_):
            return {"success": True, "league": {
                "scoring_settings": {"rec": 0.5}, "total_rosters": 12,
                "roster_positions": ["QB", "RB", "RB", "WR", "WR", "BN", "BN"],
                "settings": {"type": 0, "waiver_type": 0}}}

        async def _rosters(_):
            return {"success": True, "rosters": [
                {"roster_id": 7, "players": ["m1", "m2", "m3"], "players_enriched": []}]}

        async def _state():
            return {"success": True, "nfl_state": {"week": 3, "season": 2026}}

        async def _trending(*a, **k):
            return {"success": True, "trending_players": []}

        async def _txns(*a, **k):
            return {"success": True, "transactions": []}

        monkeypatch.setattr(ft, "get_league", _league)
        monkeypatch.setattr(ft, "get_rosters", _rosters)
        monkeypatch.setattr(ft, "get_nfl_state", _state)
        monkeypatch.setattr(ft, "get_trending_players", _trending)
        monkeypatch.setattr(ft, "get_transactions", _txns)
        monkeypatch.setattr(ft, "get_values_service", lambda *_a, **_k: _Values())

        class _DB:
            pass

        res = await ft.recommend_faab_bid("L", player_id="f1", my_roster_id=7, db=_DB())
        rec = res["recommendation"]
        expected = horizon_worth(12.0, rec["horizons"]["ros_gain"], 15)
        assert rec["horizons"]["week_gain"] == 12.0
        assert rec["priority_advice"] == expected["worth"]
        assert calls[0]["worth"] == expected["worth"]
        assert calls[0]["this_week"] is expected["this_week_only"]
        assert calls[0]["this_week"] is (later == 0.0)
