"""Slow upstreams no longer hold a tool call (or a projection) hostage.

`get_gameday_inactives` ran past the MCP client's 300 s on 2026-09-27: the
injury crawl, the ESPN gameday notes and the 5 MB Sleeper dump all ran inline,
one after another. Each read is now bounded, runs in the background past its
budget, and the answer says which sources timed out. The upstreams here are
httpx transports that answer late.
"""
import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nfl_mcp import gameday_inactives as gi
from nfl_mcp import injury_service, tool_registry, upstream

KICKOFF = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
IN_WINDOW = KICKOFF - timedelta(minutes=60)
NOTES = {"injuries": [{"displayName": "Jacksonville Jaguars", "injuries": [
    {"shortComment": "Koziol (coach's decision) is inactive for Sunday's game.",
     "date": "2026-09-27T15:27Z", "athlete": {"displayName": "Tanner Koziol"}}]}]}
DUMP = {"9": {"full_name": "Late Scratch", "team": "JAX", "injury_status": "Inactive"}}


def delayed_client(delays: dict[str, float], calls: list[str] | None = None) -> httpx.AsyncClient:
    """An httpx client whose upstreams answer after ``delays[host part]`` seconds."""
    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        for part, delay in delays.items():
            if part in url:
                await asyncio.sleep(delay)
        body = DUMP if "sleeper" in url else NOTES
        return httpx.Response(200, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def window_db():
    db = MagicMock()
    db.get_week_kickoffs = MagicMock(return_value={"JAX": KICKOFF.isoformat()})
    db.get_athletes_by_team = MagicMock(return_value=[])  # no fresh stored copy
    return db


class TestSingleFlight:
    @pytest.mark.asyncio
    async def test_callers_share_one_read_and_a_timeout_leaves_it_running(self):
        started = []

        async def read():
            started.append(1)
            await asyncio.sleep(0.2)
            return "done"

        t1 = upstream.single_flight("k", read)
        t2 = upstream.single_flight("k", read)
        assert t1 is t2
        assert await upstream.wait_bounded(t1, 0.01) == (False, None)
        assert not t1.done()
        assert await upstream.wait_bounded(t1, 1.0) == (True, "done")
        assert started == [1]

    def test_budget(self):
        b = upstream.Budget(10)
        assert 9 < b.remaining() <= 10
        assert b.remaining(cap=2) == 2
        assert upstream.Budget(0).remaining(floor=5) == 5


class TestOfficialInactivesBudget:
    @pytest.mark.asyncio
    async def test_a_slow_sleeper_dump_is_reported_not_waited_for(self):
        calls: list[str] = []
        async with delayed_client({"sleeper": 1.0}, calls) as client:
            t0 = time.perf_counter()
            out = await gi.get_official_inactives(window_db(), 2026, 4, now=IN_WINDOW,
                                                  client=client, budget_seconds=0.3)
            elapsed = time.perf_counter() - t0
            assert elapsed < 1.0
            assert out["partial"] is True
            assert out["timed_out_sources"] == [gi.SOURCE_SLEEPER]
            assert out["sources_checked"] == [gi.SOURCE_ESPN_NOTE]
            assert [r["player_name"] for r in out["inactives"]] == ["Tanner Koziol"]
            # The dump keeps downloading; once in, the next call has it
            # without fetching again.
            await asyncio.sleep(1.2)
            again = await gi.get_official_inactives(window_db(), 2026, 4, now=IN_WINDOW,
                                                    client=client, budget_seconds=0.3)
        assert again["partial"] is False
        assert {r["player_name"] for r in again["inactives"]} == {"Tanner Koziol", "Late Scratch"}
        assert sum("sleeper" in c for c in calls) == 1

    @pytest.mark.asyncio
    async def test_both_feeds_are_read_at_once(self):
        async with delayed_client({"sleeper": 0.4, "espn": 0.4}) as client:
            t0 = time.perf_counter()
            out = await gi.get_official_inactives(window_db(), 2026, 4, now=IN_WINDOW,
                                                  client=client, budget_seconds=5)
            elapsed = time.perf_counter() - t0
        assert out["partial"] is False
        assert elapsed < 0.75  # not 0.8 s back to back

    @pytest.mark.asyncio
    async def test_an_expired_dump_is_served_stale_while_it_refreshes(self):
        gi._sleeper_dump = (IN_WINDOW - gi.SLEEPER_FRESH_TTL - timedelta(minutes=10), DUMP)
        async with delayed_client({"sleeper": 3.0}) as client:
            t0 = time.perf_counter()
            out = await gi.get_official_inactives(window_db(), 2026, 4, now=IN_WINDOW,
                                                  client=client, budget_seconds=5)
            elapsed = time.perf_counter() - t0
        assert elapsed < gi.STALE_GRACE_SECONDS + 0.5
        assert "Late Scratch" in {r["player_name"] for r in out["inactives"]}
        assert out["sleeper_freshness"]["stale"] is True
        assert gi.SOURCE_SLEEPER in out["stale_sources"]
        assert gi.SOURCE_SLEEPER not in out["sources_checked"]
        assert out["partial"] is True

    @pytest.mark.asyncio
    async def test_a_fresh_dump_is_never_refetched_inline(self):
        gi._sleeper_dump = (IN_WINDOW - timedelta(minutes=5), DUMP)
        calls: list[str] = []
        async with delayed_client({}, calls) as client:
            await gi.get_official_inactives(window_db(), 2026, 4, now=IN_WINDOW, client=client)
        assert not any("sleeper" in c for c in calls)


class TestProjectionPathNeverBlocks:
    @pytest.mark.asyncio
    async def test_cold_read_gives_up_quickly(self, monkeypatch):
        async def slow(*_a, **_k):
            await asyncio.sleep(5)
            return {"inactives": [{"player_name": "X", "team_id": "JAX"}]}

        monkeypatch.setattr(gi, "get_official_inactives", slow)
        monkeypatch.setattr(gi, "GAMEDAY_PROJECTION_WAIT", 0.2)
        t0 = time.perf_counter()
        assert await gi.gameday_statuses(window_db(), 2026, 4, now=IN_WINDOW) == {}
        assert time.perf_counter() - t0 < 1.0

    @pytest.mark.asyncio
    async def test_expired_read_serves_the_older_copy(self, monkeypatch):
        async def slow(*_a, **_k):
            await asyncio.sleep(5)
            return {}

        monkeypatch.setattr(gi, "get_official_inactives", slow)
        monkeypatch.setattr(gi, "STALE_GRACE_SECONDS", 0.1)
        gi._gameday_cache[(2026, 4)] = (IN_WINDOW - gi.GAMEDAY_CACHE_TTL - timedelta(minutes=1),
                                        {"inactives": [{"player_name": "Tanner Koziol",
                                                        "team_id": "JAX"}]})
        t0 = time.perf_counter()
        out = await gi.gameday_statuses(window_db(), 2026, 4, now=IN_WINDOW)
        assert time.perf_counter() - t0 < 1.0
        assert out[("tanner koziol", "JAX")]["status"] == "inactive"


class TestToolBudget:
    @pytest.mark.asyncio
    async def test_a_cold_injury_crawl_answers_from_the_stored_reports(self, monkeypatch):
        async def slow_crawl(*_a, **_k):
            await asyncio.sleep(5)
            return []

        stored = [{"player_id": "1", "player_name": "Hurt Guy", "team_id": "KC",
                   "injury_status": "Out", "severity": 4, "confidence": 80}]
        db = MagicMock()
        db.get_team_injuries_from_cache = MagicMock(
            side_effect=lambda team, _age: stored if team == "KC" else [])
        monkeypatch.setattr(tool_registry, "GAMEDAY_INJURY_WAIT_SECONDS", 0.2)
        official = {"games": {}, "window_teams": [], "inactives": [], "confirmed_active": []}
        with patch.object(tool_registry, "get_db", return_value=db), \
             patch("nfl_mcp.gameday_inactives.get_official_inactives", AsyncMock(return_value=official)), \
             patch.object(injury_service, "get_injury_reports", slow_crawl):
            t0 = time.perf_counter()
            out = await tool_registry.get_gameday_inactives(season=2026, week=4)
            elapsed = time.perf_counter() - t0
        assert elapsed < 1.5
        assert out["success"] is True
        assert out["partial"] is True
        assert out["timed_out_sources"] == [injury_service.SOURCE_INJURY_CRAWL]
        assert out["injury_report_stale"] is True
        assert [r["player_name"] for r in out["inactives"]] == ["Hurt Guy"]

    @pytest.mark.asyncio
    async def test_a_warm_cache_is_not_partial(self):
        official = {"games": {}, "window_teams": [], "inactives": [], "confirmed_active": []}
        with patch.object(tool_registry, "get_db", return_value=MagicMock()), \
             patch("nfl_mcp.gameday_inactives.get_official_inactives", AsyncMock(return_value=official)), \
             patch("nfl_mcp.injury_service.get_injury_reports", AsyncMock(return_value=[])):
            out = await tool_registry.get_gameday_inactives(season=2026, week=4)
        assert out["partial"] is False
        assert out["timed_out_sources"] == []


class TestHandcuffDepthCharts:
    @pytest.mark.asyncio
    async def test_a_hung_depth_chart_costs_one_team_and_an_old_copy_stands_in(self, monkeypatch):
        from nfl_mcp import handcuff_tools

        async def chart(team):
            if team == "SF":
                await asyncio.sleep(5)
            return {"depth_chart": [{"position": "RB", "players": [f"{team} Star", f"{team} Backup"]}]}

        monkeypatch.setattr(handcuff_tools, "DEPTH_CHART_TIMEOUT_SECONDS", 0.2)
        nfl = MagicMock(get_depth_chart=chart)
        sem = asyncio.Semaphore(6)
        timed_out: list[str] = []
        t0 = time.perf_counter()
        sf, kc = await asyncio.gather(handcuff_tools._depth_chart(nfl, "SF", sem, timed_out),
                                      handcuff_tools._depth_chart(nfl, "KC", sem, timed_out))
        assert time.perf_counter() - t0 < 1.0
        assert sf == [] and kc[0]["players"][0] == "KC Star"
        assert timed_out == ["SF"]
        # An expired copy is served when the refetch hangs.
        old = [{"position": "RB", "players": ["Old Star", "Old Backup"]}]
        handcuff_tools._depth_chart_cache["SF"] = (
            time.monotonic() - handcuff_tools.DEPTH_CHART_TTL_SECONDS - 60, old)
        assert await handcuff_tools._depth_chart(nfl, "SF", sem, []) == old


class TestSharedReads:
    @pytest.mark.asyncio
    async def test_the_nflverse_csv_is_downloaded_once_for_both_rankings(self):
        from nfl_mcp import matchup_tools

        calls = []

        async def handler(request):
            calls.append(str(request.url))
            await asyncio.sleep(0.05)
            return httpx.Response(200, text="season_type,position,team,opponent_team,week,"
                                            "fantasy_points_ppr\nREG,RB,KC,DEN,1,20.0\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            texts = await asyncio.gather(*(matchup_tools.nflverse_week_csv(2031, client)
                                           for _ in range(4)))
            again = await matchup_tools.nflverse_week_csv(2031, client)
        assert len(calls) == 1
        assert len(set(texts)) == 1 and again == texts[0]

    @pytest.mark.asyncio
    async def test_the_same_week_of_matchups_is_fetched_once(self, monkeypatch):
        from nfl_mcp import sleeper_tools

        fetches = []

        async def fetch(league_id, week):
            fetches.append(week)
            await asyncio.sleep(0.05)
            return {"success": True, "matchups": [{"roster_id": 1, "matchup_id": 1}], "stale": False}

        monkeypatch.setattr(sleeper_tools, "_get_matchups_uncached", fetch)
        first = await asyncio.gather(*(sleeper_tools.get_matchups("L", 5) for _ in range(3)))
        first[0]["matchups"].append({"mutated": True})  # every caller owns its copy
        later = await sleeper_tools.get_matchups("L", 5)
        assert fetches == [5]
        assert later["matchups"] == [{"roster_id": 1, "matchup_id": 1}]

    @pytest.mark.asyncio
    async def test_a_failed_matchups_answer_is_not_cached(self, monkeypatch):
        from nfl_mcp import sleeper_tools

        answers = iter([{"success": False, "matchups": []}, {"success": True, "matchups": []}])
        monkeypatch.setattr(sleeper_tools, "_get_matchups_uncached",
                            AsyncMock(side_effect=lambda *_a: next(answers)))
        assert (await sleeper_tools.get_matchups("L", 6))["success"] is False
        assert (await sleeper_tools.get_matchups("L", 6))["success"] is True
