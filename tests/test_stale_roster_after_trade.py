"""A trade between two roster fetches must reach every roster consumer.

Live 2026-10-08: right after a 2-for-2 trade (roster 7 gave Jaylen Warren and
Denzel Boston for Zay Flowers and Woody Marks) analyze_lineup still benched
Warren/Boston, never listed Flowers/Marks and said ``stale: false``, while
get_bye_week_plan a moment later had the new players. Sleeper serves
/rosters through Cloudflare with ``s-maxage=300, stale-while-revalidate=300``:
the "live" fetch got the pre-trade copy, the next request the new one. And the
week's matchup starters (a separate CDN copy) were mixed with the roster.
"""
import time

import httpx
import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp import lineup_tools, sleeper_tools

LEAGUE = "1312017357782155264"
PRE = ["8228", "13346", "4046", "6804"]      # Warren, Boston, QB, RB
POST = ["9997", "12474", "4046", "6804"]     # Flowers, Marks, QB, RB


def _rosters(players, starters):
    return [{"roster_id": 7, "owner_id": "u7", "players": list(players),
             "starters": list(starters), "reserve": [], "taxi": []},
            {"roster_id": 3, "owner_id": "u3", "players": ["8228", "13346"],
             "starters": ["8228", "13346"], "reserve": [], "taxi": []}]


class _Sleeper:
    """Sleeper behind its CDN: plain GETs see ``cdn`` (``age`` seconds old),
    cache-busted GETs (``?_cb=``) see the origin."""

    def __init__(self, cdn, origin, age):
        self.cdn, self.origin, self.age = cdn, origin, age
        self.roster_calls: list[str] = []
        self.transactions: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        busted = "_cb" in request.url.params
        if path.endswith("/rosters"):
            self.roster_calls.append(str(request.url))
            if busted:
                return httpx.Response(200, json=self.origin, headers={"age": "0"})
            return httpx.Response(200, json=self.cdn, headers={"age": str(self.age)})
        if "/transactions/" in path:
            return httpx.Response(200, json=self.transactions, headers={"age": "0"})
        return httpx.Response(200, json={})

    def install(self, monkeypatch):
        transport = httpx.MockTransport(self.handler)

        def _client(*a, **k):
            return httpx.AsyncClient(transport=transport)
        monkeypatch.setattr(sleeper_tools, "create_http_client", _client)
        from nfl_mcp import sleeper_transactions
        monkeypatch.setattr(sleeper_transactions, "create_http_client", _client)

        async def _state():
            return {"success": False}
        monkeypatch.setattr(sleeper_tools, "get_nfl_state", _state)
        monkeypatch.setattr(sleeper_transactions, "get_nfl_state", _state)


def _ids(resp, roster_id=7):
    return next(r for r in resp["rosters"] if r["roster_id"] == roster_id)["players"]


@pytest.mark.asyncio
async def test_an_old_cdn_copy_is_refetched_from_the_origin(monkeypatch):
    sleeper = _Sleeper(cdn=_rosters(PRE, PRE), origin=_rosters(POST, POST), age=250)
    sleeper.install(monkeypatch)
    resp = await sleeper_tools.get_rosters(LEAGUE)
    assert _ids(resp) == POST
    assert resp["stale"] is False
    assert resp["snapshot_age_seconds"] == 0
    assert any("_cb=" in u for u in sleeper.roster_calls)
    # The freshness read says how old the data is, live or not.
    state = sleeper_tools.roster_freshness(resp)
    assert state["snapshot_age_seconds"] == 0 and state["stale"] is False


@pytest.mark.asyncio
async def test_a_young_cdn_copy_is_used_and_its_age_reported(monkeypatch):
    sleeper = _Sleeper(cdn=_rosters(POST, POST), origin=_rosters(POST, POST), age=12)
    sleeper.install(monkeypatch)
    resp = await sleeper_tools.get_rosters(LEAGUE)
    assert _ids(resp) == POST
    assert resp["snapshot_age_seconds"] == 12
    assert not any("_cb=" in u for u in sleeper.roster_calls)


@pytest.mark.asyncio
async def test_old_data_that_cannot_be_refreshed_is_flagged_stale(monkeypatch):
    sleeper = _Sleeper(cdn=_rosters(PRE, PRE), origin=_rosters(PRE, PRE), age=400)

    cdn_only = sleeper.handler

    def handler(request):
        if "_cb" in request.url.params:
            return httpx.Response(503)
        return cdn_only(request)
    sleeper.handler = handler
    sleeper.install(monkeypatch)
    state = await sleeper_tools.load_rosters(LEAGUE, "lineup")
    assert state["stale"] is True
    assert state["snapshot_age_seconds"] == 400
    assert "CDN copy" in state["warning"]


@pytest.mark.asyncio
async def test_a_trade_between_two_fetches_reaches_the_next_reader(monkeypatch):
    sleeper = _Sleeper(cdn=_rosters(PRE, PRE), origin=_rosters(PRE, PRE), age=0)
    sleeper.install(monkeypatch)
    first = await sleeper_tools.get_rosters(LEAGUE)
    assert _ids(first) == PRE
    # Within the reuse window every consumer reads that same copy.
    again = await sleeper_tools.load_rosters(LEAGUE, "lineup")
    assert _ids(again) == PRE and len(sleeper.roster_calls) == 1

    # The trade is processed; the transactions feed shows it.
    sleeper.cdn = sleeper.origin = _rosters(POST, POST)
    sleeper.transactions = [{
        "type": "trade", "status": "complete", "roster_ids": [7, 3],
        "status_updated": int((time.time() + 1) * 1000),
        "adds": {"9997": 7, "12474": 7, "8228": 3, "13346": 3},
        "drops": {"8228": 7, "13346": 7, "9997": 3, "12474": 3},
    }]
    await sleeper_tools.get_transactions(LEAGUE, week=5)
    after = await sleeper_tools.load_rosters(LEAGUE, "lineup")
    assert _ids(after) == POST
    assert len(sleeper.roster_calls) == 2


@pytest.mark.asyncio
async def test_the_reuse_window_expires(monkeypatch):
    sleeper = _Sleeper(cdn=_rosters(PRE, PRE), origin=_rosters(PRE, PRE), age=0)
    sleeper.install(monkeypatch)
    await sleeper_tools.get_rosters(LEAGUE)
    sleeper.cdn = sleeper.origin = _rosters(POST, POST)
    monkeypatch.setattr(sleeper_tools, "ROSTER_MEMO_TTL_SECONDS", 0.0)
    assert _ids(await sleeper_tools.get_rosters(LEAGUE)) == POST


def test_matchup_starters_from_before_the_trade_are_not_used():
    roster = _rosters(POST, POST)[0]
    stale_matchup = {"roster_id": 7, "starters": ["8228", "13346", "4046", "6804"]}
    assert sleeper_tools.set_starters(roster, stale_matchup) == (POST, "roster")
    fresh_matchup = {"roster_id": 7, "starters": ["4046", "0", "9997", "12474"]}
    assert sleeper_tools.set_starters(roster, fresh_matchup) == (
        ["4046", "0", "9997", "12474"], "matchup")


@pytest.mark.asyncio
async def test_analyze_lineup_grades_the_post_trade_roster(monkeypatch):
    """CDN copy pre-trade, matchup copy pre-trade, origin post-trade."""
    sleeper = _Sleeper(cdn=_rosters(PRE, PRE), origin=_rosters(POST, POST), age=250)
    sleeper.install(monkeypatch)
    names = {"8228": ("Jaylen Warren", "RB", "PIT"), "13346": ("Denzel Boston", "WR", "CLE"),
             "9997": ("Zay Flowers", "WR", "BAL"), "12474": ("Woody Marks", "RB", "HOU"),
             "4046": ("Patrick Mahomes", "QB", "KC"), "6804": ("Jordan Love", "QB", "GB")}

    class _DB:
        def get_athletes_by_ids(self, ids):
            return {i: {"full_name": names[i][0], "position": names[i][1], "team_id": names[i][2]}
                    for i in ids if i in names}

        def get_week_opponents(self, season, week):
            return {}

        def get_usage_for_week(self, *a):
            return []

    async def _league(league_id):
        return {"league": {"name": "L", "roster_positions": ["QB", "RB", "WR", "FLEX", "BN", "BN"]}}

    async def _matchups(league_id, week):
        return {"matchups": [{"roster_id": 7, "starters": ["4046", "8228", "13346", "6804"]}]}
    monkeypatch.setattr(sleeper_tools, "get_league", _league)
    monkeypatch.setattr(sleeper_tools, "get_matchups", _matchups)
    captured = {}

    async def _full(**kwargs):
        captured.update(kwargs)
        return {"success": True}
    monkeypatch.setattr(lo, "analyze_full_lineup", _full)

    out = await lineup_tools.analyze_lineup(LEAGUE, roster_id=7, week=5, season=2026, db=_DB())
    graded = {p["name"] for slot in captured["lineup"].values() for p in slot}
    assert {"Zay Flowers", "Woody Marks"} <= graded
    assert not {"Jaylen Warren", "Denzel Boston"} & graded
    bench = {p["name"] for p in captured["lineup"]["BENCH"]}
    assert "Jaylen Warren" not in bench
    assert out["occupied_unknown_slots"] == []
    assert out["stale"] is False and out["snapshot_age_seconds"] == 0
