"""The briefing's `add_now` block: free-agent upgrades to make before they lock.

Week 9, 2026: KC DEF was an instant-add free agent worth +2.0 while the
user's CIN played in Madrid at 15:30 Vienna -- and the briefing said nothing.
"""
import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nfl_mcp import add_now
from nfl_mcp.database import NFLDatabase

NOW = datetime(2026, 11, 8, 10, tzinfo=UTC)
EARLY = "2026-11-08T14:30:00Z"   # Madrid, 15:30 Vienna
LATE = "2026-11-09T01:20:00Z"    # Sunday night


def _target(name="KC", **kw):
    t = {
        "name": name, "player_id": name, "position": "DEF", "team": name, "opponent": "BUF",
        "projected_points": 8.0, "upgrade_points": 2.0, "verdict": "upgrade", "locked": False,
        "kickoff": LATE, "kickoff_local": "2026-11-09T02:20:00+01:00",
        "waiver_timing": {"instant_add": True, "in_time_for_kickoff": True},
        "waiver_strategy": {"recommendation": "add_now"},
        "replaces": {"player_id": "CIN", "name": "CIN", "position": "DEF",
                     "projected_points": 6.0, "kickoff": EARLY},
        "drop": {"player_id": "CIN", "name": "CIN", "kickoff": EARLY},
        "drop_note": "Drop CIN",
    }
    t.update(kw)
    return t


def _resp(*targets, open_spots=0):
    return {"success": True, "targets": list(targets), "open_roster_spots": open_spots,
            "pool_size": 300}


class TestFromTargets:
    def test_kc_for_cin_before_the_madrid_kickoff(self):
        [move] = add_now.from_targets(_resp(_target()), NOW)
        assert (move["player"], move["replaces"], move["drop"]) == ("KC", "CIN", "CIN")
        assert move["gain"] == 2.0 and move["projected_points"] == 8.0
        assert move["deadline"] == EARLY
        assert move["deadline_local"] == "2026-11-08T15:30:00+01:00"
        assert "CIN" in move["deadline_reason"]
        assert "neither benched nor dropped" in move["deadline_reason"]
        assert "Sun 15:30" in move["summary"]

    def test_the_add_s_own_kickoff_when_it_comes_first(self):
        t = _target(kickoff=EARLY, replaces={"player_id": "x", "name": "X", "kickoff": LATE},
                    drop={"player_id": "y", "name": "Y", "kickoff": LATE})
        [move] = add_now.from_targets(_resp(t), NOW)
        assert move["deadline"] == EARLY and move["deadline_reason"].startswith("KC kicks off")

    def test_a_separate_drop_can_set_the_deadline(self):
        t = _target(replaces={"player_id": "x", "name": "X", "kickoff": LATE},
                    drop={"player_id": "y", "name": "Bench Guy", "kickoff": EARLY})
        [move] = add_now.from_targets(_resp(t), NOW)
        assert move["deadline"] == EARLY
        assert "Bench Guy (the drop)" in move["deadline_reason"]

    def test_open_spot_needs_no_drop(self):
        [move] = add_now.from_targets(_resp(_target(drop=None), open_spots=1), NOW)
        assert move["drop"] is None

    @pytest.mark.parametrize("change", [
        {"verdict": "speculative"},
        {"waiver_timing": {"instant_add": False, "in_time_for_kickoff": True}},
        {"waiver_timing": {"instant_add": True, "in_time_for_kickoff": False}},
        {"waiver_timing": {"instant_add": True, "in_time_for_kickoff": None}},
        {"waiver_strategy": {"recommendation": "dont_bother"}},
        {"locked": True},
        # Full roster and nobody worth dropping: not a move to push.
        {"drop": None},
    ])
    def test_only_real_instant_upgrades(self, change):
        assert add_now.from_targets(_resp(_target(**change)), NOW) == []

    def test_faab_leagues_have_no_strategy_and_still_count(self):
        assert add_now.from_targets(_resp(_target(waiver_strategy=None)), NOW)

    def test_nothing_once_the_deadline_has_passed(self):
        assert add_now.from_targets(_resp(_target()), NOW + timedelta(hours=5)) == []

    def test_biggest_gain_first(self):
        small = _target("DEN", upgrade_points=1.6)
        big = _target("KC", upgrade_points=3.1)
        assert [m["player"] for m in add_now.from_targets(_resp(small, big), NOW)] == ["KC", "DEN"]


class TestBuildBlock:
    @pytest.mark.asyncio
    async def test_reads_the_waiver_targets(self, monkeypatch):
        async def _read(*_a, **_k):
            return _resp(_target())
        monkeypatch.setattr(add_now, "read_targets", _read)
        block = await add_now.build_block("L", 1, 2026, 9, now=NOW)
        assert [m["player"] for m in block["moves"]] == ["KC"]
        assert block["free_agents_checked"] == 300 and "error" not in block

    @pytest.mark.asyncio
    async def test_a_failed_read_is_an_empty_block_with_an_error(self, monkeypatch):
        async def _boom(*_a, **_k):
            raise RuntimeError("sleeper down")
        monkeypatch.setattr(add_now, "read_targets", _boom)
        assert await add_now.build_block("L", 1, 2026, 9) == {"moves": [], "error": "sleeper down"}

    @pytest.mark.asyncio
    async def test_an_unsuccessful_response_is_reported(self, monkeypatch):
        async def _stale(*_a, **_k):
            return {"success": False, "error": "rosters stale"}
        monkeypatch.setattr(add_now, "read_targets", _stale)
        assert await add_now.build_block("L", 1, 2026, 9) == {"moves": [], "error": "rosters stale"}

    @pytest.mark.asyncio
    async def test_bounded_in_time(self, monkeypatch):
        async def _slow(*_a, **_k):
            await asyncio.sleep(5)
        monkeypatch.setattr(add_now, "read_targets", _slow)
        block = await add_now.build_block("L", 1, 2026, 9, budget=0.05)
        assert block["moves"] == [] and "not read within" in block["error"]


class TestStartAndCollect:
    """The briefing starts the read first and collects it last: the read gets
    the briefing's own duration for free, then a grace period."""

    @pytest.mark.asyncio
    async def test_a_read_slower_than_the_grace_but_not_the_briefing_lands(self, monkeypatch):
        async def _read(*_a, **_k):
            await asyncio.sleep(0.2)  # longer than the grace alone
            return _resp(_target())
        monkeypatch.setattr(add_now, "read_targets", _read)
        task = add_now.start("L", 1, 2026, 9)
        await asyncio.sleep(0.3)  # the briefing's own work
        block = await add_now.collect(task, now=NOW, grace=0.05)
        assert [m["player"] for m in block["moves"]] == ["KC"] and "error" not in block

    @pytest.mark.asyncio
    async def test_past_the_grace_the_read_is_cancelled(self, monkeypatch):
        async def _slow(*_a, **_k):
            await asyncio.sleep(5)
        monkeypatch.setattr(add_now, "read_targets", _slow)
        task = add_now.start("L", 1, 2026, 9)
        block = await add_now.collect(task, grace=0.05)
        assert block["moves"] == [] and "not read within" in block["error"]
        await asyncio.wait([task])  # 3.11's wait_for needs a few turns to settle
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_an_uncollected_read_stops_at_the_cap(self, monkeypatch):
        async def _slow(*_a, **_k):
            await asyncio.sleep(5)
        monkeypatch.setattr(add_now, "read_targets", _slow)
        task = add_now.start("L", 1, 2026, 9, cap=0.05)
        with pytest.raises(TimeoutError):
            await task
        assert (await add_now.collect(task))["moves"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("offline_sources")
async def test_the_briefing_carries_the_block(monkeypatch):
    from nfl_mcp import briefing_tools, projections, sleeper_tools

    with tempfile.TemporaryDirectory() as tmp:
        db = NFLDatabase(str(Path(tmp) / "t.db"))
        db.upsert_schedule_games([
            {"season": 2026, "week": 9, "team": "BUF", "opponent": "NYJ",
             "is_home": 1, "kickoff": "2099-11-08T18:00Z"},
            {"season": 2026, "week": 9, "team": "NYJ", "opponent": "BUF",
             "is_home": 0, "kickoff": "2099-11-08T18:00Z"},
        ])
        db.upsert_athletes({"on": {"full_name": "Playing WR", "position": "WR", "team": "BUF"}})
        monkeypatch.setattr(briefing_tools, "get_shared_db", lambda *a, **k: db)

        async def _ret(value):
            return value

        monkeypatch.setattr(sleeper_tools, "get_league", lambda *_a, **_k: _ret(
            {"league": {"total_rosters": 10, "scoring_settings": {"rec": 1},
                        "roster_positions": ["WR", "BN"], "settings": {}}}))
        monkeypatch.setattr(sleeper_tools, "get_rosters", lambda *_a, **_k: _ret(
            {"rosters": [{"roster_id": 1, "players": ["on"], "starters": ["on"]}]}))
        monkeypatch.setattr(sleeper_tools, "get_matchups", lambda *_a, **_k: _ret(
            {"matchups": [{"roster_id": 1, "matchup_id": 1, "starters": ["on"]}]}))

        async def _project(players, **_):
            return {"projections": [
                {"player": p["name"], "position": p["position"], "team": p["team"],
                 "opponent": p["opponent"], "projected_points": 10.0, "floor": 5.0,
                 "ceiling": 15.0} for p in players]}

        monkeypatch.setattr(projections, "project_players", _project)
        seen = {}

        async def _read(league_id, roster_id, season, week):
            seen.update(league_id=league_id, roster_id=roster_id, season=season, week=week)
            return _resp(_target(kickoff="2099-11-09T01:20:00Z",
                                 replaces={"player_id": "CIN", "name": "CIN",
                                           "kickoff": "2099-11-08T14:30:00Z"},
                                 drop={"player_id": "CIN", "name": "CIN",
                                       "kickoff": "2099-11-08T14:30:00Z"}))

        monkeypatch.setattr(add_now, "read_targets", _read)
        out = await briefing_tools.get_weekly_briefing("L", roster_id=1, week=9, season=2026)
        assert seen == {"league_id": "L", "roster_id": 1, "season": 2026, "week": 9}
        [move] = out["add_now"]["moves"]
        assert move["player"] == "KC" and move["deadline"] == "2099-11-08T14:30:00Z"
