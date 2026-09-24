"""Roster freshness for availability questions, co-owners, and the ROS roster
behind a claim's rest-of-season gain (bye and IR players included)."""
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import faab_tools as ft
from nfl_mcp import handcuff_tools, ros, sleeper_tools, waiver_target_tools
from nfl_mcp.briefing_tools import find_roster
from nfl_mcp.sleeper_tools import (
    availability_error,
    mark_roster_staleness,
    owns_roster,
    roster_freshness,
)
from nfl_mcp.waiver_target_tools import get_waiver_targets
from tests.test_faab_tools import _run
from tests.test_waiver_targets import LEAGUE, _stub_sleeper, db  # noqa: F401

ROSTERS = [{"roster_id": 1, "owner_id": "a", "co_owners": ["b"], "players": ["x"]}]


def _snapshot(age_seconds):
    return {"success": False, "error": "Roster fetch failed after retries (serving snapshot)",
            "rosters": ROSTERS, "stale": True, "failure_reason": "timeout",
            "snapshot_age_seconds": age_seconds}


class TestRosterFreshness:
    def test_fresh(self):
        f = roster_freshness({"success": True, "rosters": ROSTERS, "stale": False})
        assert f["available"] and f["usable_for_availability"] and not f["stale"]
        assert f["warning"] is None and availability_error(f) is None

    def test_recent_snapshot_is_usable_with_a_warning(self):
        f = roster_freshness(_snapshot(600))
        assert f["stale"] and f["snapshot_age_seconds"] == 600
        assert f["usable_for_availability"] and "10 min old" in f["warning"]

    def test_old_snapshot_cannot_answer_availability(self):
        f = roster_freshness(_snapshot(5 * 3600))
        assert f["available"] and not f["usable_for_availability"]
        assert "too old" in availability_error(f)

    def test_failed_fetch_without_rosters(self):
        f = roster_freshness({"success": False, "error": "boom", "rosters": []})
        assert not f["available"] and "boom" in f["error"]
        assert availability_error(f)

    def test_mark_staleness_appends_the_warning(self):
        out = mark_roster_staleness({"warnings": ["x"]}, roster_freshness(_snapshot(60)))
        assert out["stale"] is True and len(out["warnings"]) == 2

    def test_co_owner_owns_the_roster(self):
        assert owns_roster(ROSTERS[0], "b") and owns_roster(ROSTERS[0], "a")
        assert not owns_roster(ROSTERS[0], "c")
        mine, err = find_roster(ROSTERS, "L", None, "b")
        assert err is None and mine["roster_id"] == 1


class TestSnapshotAgeLimit:
    @pytest.mark.asyncio
    async def test_a_day_old_snapshot_is_not_served(self, monkeypatch):
        class _DB:
            def load_roster_snapshot(self, league_id):
                return {"rosters": ROSTERS, "stale": True, "fetched_at": "x",
                        "age_seconds": 3 * 86400}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                import httpx
                raise httpx.TimeoutException("slow")

        monkeypatch.setattr(sleeper_tools, "_init_db", lambda: _DB(), raising=False)
        from nfl_mcp import database
        monkeypatch.setattr(database, "get_shared_db", lambda *a, **k: _DB())
        monkeypatch.setattr(sleeper_tools, "create_http_client", lambda *a, **k: _Client())
        monkeypatch.setattr(sleeper_tools.asyncio, "sleep", AsyncMock())
        res = await sleeper_tools.get_rosters("L")
        assert res["success"] is False
        assert res["rosters"] == []
        assert "too old" in res["failure_reason"]


class TestFaabRefusesUnknownAvailability:
    @pytest.mark.asyncio
    async def test_failed_rosters_are_an_error_not_a_bid(self):
        res = await _run(league_id="1", player_id="9509", my_roster_id=1,
                         rosters={"success": False, "error": "timeout", "rosters": []})
        assert res["success"] is False and res["recommendation"] is None

    @pytest.mark.asyncio
    async def test_an_old_snapshot_is_refused(self):
        res = await _run(league_id="1", player_id="9509", my_roster_id=1,
                         rosters=_snapshot(4 * 3600) | {"rosters": []} | {"rosters": [
                             {"roster_id": 1, "players": ["weak"]}]})
        assert res["success"] is False and res["stale"] is True

    @pytest.mark.asyncio
    async def test_a_recent_snapshot_still_catches_a_rostered_player(self):
        snap = _snapshot(300) | {"rosters": [
            {"roster_id": 2, "players": ["9509"]},
            {"roster_id": 1, "players": ["weak"], "players_enriched": [
                {"player_id": "weak", "full_name": "Weak RB", "position": "RB"}]}]}
        res = await _run(league_id="1", player_id="9509", my_roster_id=1, rosters=snap)
        assert res["recommendation"] is None and res["rostered_by"] == 2

    @pytest.mark.asyncio
    async def test_a_recent_snapshot_bids_with_a_warning(self):
        snap = _snapshot(300) | {"rosters": [
            {"roster_id": 1, "players": ["weak"], "players_enriched": [
                {"player_id": "weak", "full_name": "Weak RB", "position": "RB"}],
             "settings": {"waiver_budget_used": 0}}]}
        res = await _run(league_id="1", player_id="9509", my_roster_id=1, rosters=snap)
        assert res["recommendation"] is not None
        assert res["stale"] is True and res["snapshot_age_seconds"] == 300
        assert any("snapshot" in w for w in res["recommendation"]["warnings"])


class TestFaabWeeksLeft:
    @pytest.mark.asyncio
    async def test_weeks_left_follows_the_league_playoff_start(self):
        from tests.test_faab_tools import _league
        league = _league()
        league["league"]["settings"]["playoff_week_start"] = 16
        rosters = {"success": True, "rosters": []}
        res = await _run(league_id="1", player_id="9509", league=league, rosters=rosters)
        # Week 10 is still to be played, so weeks 10-15 are left.
        assert "6 regular-season weeks left" in res["recommendation"]["reasoning"]


class TestWaiverTargetsAvailability:
    @pytest.mark.asyncio
    async def test_failed_rosters_refuse(self, db, monkeypatch):  # noqa: F811
        _stub_sleeper(monkeypatch, {})
        monkeypatch.setattr(sleeper_tools, "get_rosters", AsyncMock(
            return_value={"success": False, "error": "down", "rosters": []}))
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        assert out["success"] is False and "down" in out["error"]

    @pytest.mark.asyncio
    async def test_old_snapshot_refuses(self, db, monkeypatch):  # noqa: F811
        _stub_sleeper(monkeypatch, {})
        snap = _snapshot(7200) | {"rosters": [{"roster_id": 7, "owner_id": "me",
                                               "players": ["m1"]}]}
        monkeypatch.setattr(sleeper_tools, "get_rosters", AsyncMock(return_value=snap))
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        assert out["success"] is False and out["stale"] is True

    @pytest.mark.asyncio
    async def test_co_owner_is_found_by_user_id(self, db, monkeypatch):  # noqa: F811
        _stub_sleeper(monkeypatch, {})
        monkeypatch.setattr(sleeper_tools, "get_rosters", AsyncMock(return_value={"rosters": [
            {"roster_id": 7, "owner_id": "other", "co_owners": ["me"], "players": ["m1"]}]}))
        out = await get_waiver_targets(LEAGUE, user_id="me")
        assert out["roster_id"] == 7 and out["stale"] is False


def _entry(pid, position, weekly):
    return {"player_id": pid, "position": position, "weekly_points": weekly,
            "total_points": sum(weekly.values())}


WEEKS = list(range(3, 18))


class TestRosRosterKeepsByeAndReservePlayers:
    """ROS roster = every active roster id: a bye (or IR) player still starts
    in the weeks he plays, so a claim is not credited for replacing him there."""

    @staticmethod
    def _ros(extra: dict):
        entries = {
            "m1": _entry("m1", "WR", dict.fromkeys(WEEKS, 14.0)),
            "m2": _entry("m2", "WR", dict.fromkeys(WEEKS, 9.0)),
            "m3": _entry("m3", "RB", dict.fromkeys(WEEKS, 3.0)),
            "f1": _entry("f1", "RB", dict.fromkeys(WEEKS, 12.0)),
            **extra,
        }
        seen = []

        async def _ros_for_ids(ids, **_k):
            seen.append(list(ids))
            return ({i: entries[i] for i in ids if i in entries},
                    {"windows": {"regular": WEEKS[:12], "playoff": WEEKS[12:]}})
        return _ros_for_ids, seen

    @pytest.mark.asyncio
    async def test_a_bye_player_stays_in_the_ros_roster(self, db, monkeypatch):  # noqa: F811
        # f4 (LAR) is on bye in week 3 — no projection input this week — and
        # worth 20 a week afterwards.
        fn, seen = self._ros({"f4": _entry("f4", "RB", {w: (0.0 if w == 3 else 20.0)
                                                        for w in WEEKS})})
        monkeypatch.setattr(ros, "ros_for_ids", fn)
        _stub_sleeper(monkeypatch, {"My Starter WR": 14.0, "My Second WR": 9.0,
                                    "My Weak RB": 3.0, "Free Good RB": 12.0},
                      mine=["m1", "m2", "m3", "f4"])
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        target = next(t for t in out["targets"] if t["name"] == "Free Good RB")
        assert "f4" in seen[0]
        # Week 3: replaces the bye (12); weeks 4-17: replaces m3 (12 - 3 = 9).
        assert target["ros_gain"] == pytest.approx(12 + 9 * 14)

    @pytest.mark.asyncio
    async def test_a_reserve_player_counts_after_this_week(self, db, monkeypatch):  # noqa: F811
        fn, seen = self._ros({"r1": _entry("r1", "RB", dict.fromkeys(WEEKS, 20.0))})
        monkeypatch.setattr(ros, "ros_for_ids", fn)
        _stub_sleeper(monkeypatch, {"My Starter WR": 14.0, "My Second WR": 9.0,
                                    "My Weak RB": 3.0, "Free Good RB": 12.0})
        monkeypatch.setattr(sleeper_tools, "get_rosters", AsyncMock(return_value={"rosters": [
            {"roster_id": 7, "owner_id": "me", "players": ["m1", "m2", "m3", "r1"],
             "reserve": ["r1"], "starters": []},
            {"roster_id": 2, "owner_id": "them", "players": ["o1"]}]}))
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        target = next(t for t in out["targets"] if t["name"] == "Free Good RB")
        assert "r1" in seen[0]
        # r1 sits out week 3 (the claim fills his seat: 12), then starts.
        assert target["ros_gain"] == pytest.approx(12 + 9 * 14)


class TestFaabHorizonsKeepReserve:
    @pytest.mark.asyncio
    async def test_reserve_player_is_in_the_ros_roster_but_not_this_week(self, monkeypatch):
        entries = {
            "m1": _entry("m1", "RB", dict.fromkeys(WEEKS, 3.0)),
            "r1": _entry("r1", "RB", dict.fromkeys(WEEKS, 20.0)),
            "f1": _entry("f1", "RB", dict.fromkeys(WEEKS, 12.0)),
        }

        async def _ros_for_ids(ids, **_k):
            return ({i: entries[i] for i in ids if i in entries},
                    {"windows": {"regular": WEEKS[:12], "playoff": WEEKS[12:]}})
        monkeypatch.setattr(ros, "ros_for_ids", _ros_for_ids)
        league = {"roster_positions": ["RB", "BN"]}
        roster = {"players": ["m1", "r1"], "reserve": ["r1"]}
        out = await ft._horizon_gains(league, roster, "f1", 2026, 3, db=None)
        assert out["week_gain"] == 9.0          # r1 cannot play this week
        assert out["ros_gain"] == pytest.approx(9.0)  # and blocks f1 after


class TestHandcuffAvailability:
    @pytest.mark.asyncio
    async def test_old_snapshot_is_refused(self):
        snap = _snapshot(9000) | {"rosters": [{"roster_id": 1, "players": ["x"]}]}
        with patch("nfl_mcp.sleeper_tools.get_rosters", new=AsyncMock(return_value=snap)):
            out = await handcuff_tools.get_handcuff_map("L", 1, db=object())
        assert out["success"] is False and out["stale"] is True


class TestWaiverRosPlumbing:
    @pytest.mark.asyncio
    async def test_attach_ros_requests_extra_ids_once(self, monkeypatch):
        seen = []

        async def _ros_for_ids(ids, **_k):
            seen.append(ids)
            return {}, {}
        monkeypatch.setattr(ros, "ros_for_ids", _ros_for_ids)
        await waiver_target_tools._attach_ros([{"player_id": "a"}], {}, 2026, 3, None,
                                              extra_ids=["a", "b", "b"])
        assert seen == [["a", "b"]]
