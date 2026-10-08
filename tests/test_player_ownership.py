"""get_player_ownership: a teamless player is still owned (Tyreek Hill), an
unmatched name is unknown (not a free agent), and waiver timing is the
waiver tools' own."""
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nfl_mcp import lineup_tools, ownership_tools, player_pool, tool_registry
from nfl_mcp.database import NFLDatabase

NOW = datetime(2026, 10, 8, 18, tzinfo=UTC)
NEWS_MS = int(time.time() * 1000)


def _athlete(pid, name, position, team, rank=100, status="Active", news_ms=NEWS_MS):
    return pid, {"player_id": pid, "full_name": name, "position": position, "team": team,
                 "status": status, "search_rank": rank, "news_updated": news_ms,
                 "active": status == "Active"}


@pytest.fixture
def db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        database.upsert_athletes(dict([
            _athlete("3321", "Tyreek Hill", "WR", None, rank=145),
            _athlete("9997", "Zay Flowers", "WR", "BAL", rank=40),
            _athlete("13287", "Jeremiyah Love", "RB", "ARI", rank=15),
            _athlete("13301", "Antonio Williams", "WR", "WAS", rank=156),
            _athlete("7203", "Antonio Williams", "RB", None, rank=615),
            _athlete("164", "Joe Mixon", "RB", None, rank=164),
            # Retired years ago: teamless, high rank, no recent news.
            _athlete("74", "Tom Brady", "QB", None, rank=74, news_ms=1_600_000_000_000),
            _athlete("900", "Some Linebacker", "LB", None, rank=50),
        ]))
        monkeypatch.setattr(ownership_tools, "get_shared_db", lambda *a, **k: database)
        yield database


def _stub(monkeypatch, rosters, transactions=None, roster_resp=None):
    from nfl_mcp import sleeper_tools

    async def _state():
        return {"nfl_state": {"week": 5, "season": 2026}}

    async def _league(_):
        return {"league": {"name": "Test", "settings": {"waiver_type": 0, "waiver_clear_days": 2,
                                                        "waiver_day_of_week": 2}}}

    async def _rosters(_):
        return roster_resp or {"rosters": rosters, "snapshot_age_seconds": 3, "stale": False}

    async def _users(_):
        return {"users": [{"user_id": "u11", "display_name": "flogu",
                           "metadata": {"team_name": "Bregenz Cowboys"}},
                          {"user_id": "u7", "display_name": "gtonic"}]}

    async def _transactions(_league, week=None):
        return {"transactions": transactions or []}

    monkeypatch.setattr(sleeper_tools, "get_nfl_state", _state)
    monkeypatch.setattr(sleeper_tools, "get_league", _league)
    monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
    monkeypatch.setattr(sleeper_tools, "get_league_users", _users)
    monkeypatch.setattr(sleeper_tools, "get_transactions", _transactions)
    monkeypatch.setattr(ownership_tools, "_now", lambda: NOW)


ROSTERS = [
    {"roster_id": 11, "owner_id": "u11", "players": ["3321", "13301"], "starters": ["13301"]},
    {"roster_id": 7, "owner_id": "u7", "players": ["9997"], "starters": ["9997"],
     "reserve": ["9997"]},
]


def _by_query(resp):
    return {p["query"]: p for p in resp["players"]}


class TestNameResolution:
    def test_teamless_player_resolves(self, db):
        ranked, ambiguous = lineup_tools.name_candidates(db, "Tyreek Hill",
                                                         include_free_agents=True)
        assert ranked[0]["id"] == "3321" and not ambiguous

    def test_two_fantasy_players_one_teamless_is_ambiguous(self, db):
        ranked, ambiguous = lineup_tools.name_candidates(db, "Antonio Williams",
                                                         include_free_agents=True)
        assert ambiguous and [r["id"] for r in ranked] == ["13301", "7203"]
        # The default lookup keeps its old behaviour (teamed players only count).
        assert lineup_tools.name_candidates(db, "Antonio Williams")[1] is False


class TestOwnership:
    @pytest.mark.asyncio
    async def test_teamless_rostered_player_is_rostered(self, db, monkeypatch):
        _stub(monkeypatch, ROSTERS)
        resp = await ownership_tools.get_player_ownership(
            "L1", ["Tyreek Hill", "Zay Flowers", "Nobody Atall"])
        hill = _by_query(resp)["Tyreek Hill"]
        assert hill["status"] == "rostered" and hill["roster_id"] == 11
        assert hill["team"] == "FA" and hill["owner"] == "flogu"
        assert hill["team_name"] == "Bregenz Cowboys" and hill["slot"] == "bench"
        flowers = _by_query(resp)["Zay Flowers"]
        assert flowers["slot"] == "ir" and flowers["on_ir"] and flowers["team_name"] == "gtonic"
        assert resp["unresolved"] == [{"name": "Nobody Atall",
                                       "reason": "no player by that name in the athlete cache"}]
        assert "Nobody Atall" not in _by_query(resp)

    @pytest.mark.asyncio
    async def test_ambiguous_name_lists_candidates_and_their_rosters(self, db, monkeypatch):
        _stub(monkeypatch, ROSTERS)
        resp = await ownership_tools.get_player_ownership("L1", ["Antonio Williams"])
        aw = resp["players"][0]
        assert aw["ambiguous"] and aw["player_id"] == "13301" and aw["status"] == "rostered"
        assert {c["player_id"]: c["roster_id"] for c in aw["candidates"]} == {
            "13301": 11, "7203": None}

    @pytest.mark.asyncio
    async def test_free_agent_and_recent_drop(self, db, monkeypatch):
        dropped = int(datetime(2026, 10, 7, 10, 35, tzinfo=UTC).timestamp() * 1000)
        _stub(monkeypatch, ROSTERS, transactions=[
            {"status": "complete", "status_updated": dropped, "drops": {"13287": 3}}])
        resp = await ownership_tools.get_player_ownership("L1", ["Jeremiyah Love", "Joe Mixon"])
        love, mixon = _by_query(resp)["Jeremiyah Love"], _by_query(resp)["Joe Mixon"]
        assert love["status"] == "on_waivers"
        assert love["waiver_timing"]["on_waivers"] is True and "dropped" in \
            love["waiver_timing"]["reason"]
        # No NFL team: no game lock, so not "unknown" -- a free agent with a note.
        assert mixon["status"] == "free_agent" and mixon["team"] == "FA"
        assert mixon["waiver_timing"]["instant_add"] is True and "No NFL team" in mixon["note"]

    @pytest.mark.asyncio
    async def test_teamed_free_agent_without_kickoffs_is_unknown_timing(self, db, monkeypatch):
        _stub(monkeypatch, [{"roster_id": 1, "owner_id": "u7", "players": []}])
        resp = await ownership_tools.get_player_ownership("L1", ["Zay Flowers"])
        assert resp["players"][0]["status"] == "unrostered"
        assert resp["players"][0]["waiver_timing"]["on_waivers"] is None

    @pytest.mark.asyncio
    async def test_stale_rosters_are_an_error_not_free_agents(self, db, monkeypatch):
        _stub(monkeypatch, [], roster_resp={"success": False, "error": "boom", "rosters": []})
        resp = await ownership_tools.get_player_ownership("L1", ["Tyreek Hill"])
        assert resp["success"] is False and "players" not in resp

    @pytest.mark.asyncio
    async def test_registered_tool(self, db, monkeypatch):
        _stub(monkeypatch, ROSTERS)
        resp = await tool_registry.get_player_ownership("123", "Tyreek Hill")
        assert resp["players"][0]["roster_id"] == 11
        assert (await tool_registry.get_player_ownership("123", []))["success"] is False


class TestUnsignedFreeAgents:
    def test_relevant_teamless_players_only(self, db):
        rows = db.get_athletes_by_positions(["QB", "RB", "WR", "TE"])
        found = player_pool.unsigned_free_agents(rows, taken={"3321"})
        # Hill is rostered; Brady has no recent news; the second Antonio
        # Williams ranks past the cut-off; teamed players never appear.
        assert [u["name"] for u in found] == ["Joe Mixon"]
        assert player_pool.unsigned_free_agents(rows, taken=set(), positions=["WR"])[0][
            "name"] == "Tyreek Hill"
