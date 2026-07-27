"""Tests for handcuff mapping (nfl_mcp.handcuff_tools)."""
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp.handcuff_tools import (
    _availability,
    _clean_name,
    get_handcuff_map,
    handcuff_from_depth,
)


class TestCleanName:
    def test_strips_injury_tags(self):
        assert _clean_name("Christian KirkQ") == "Christian Kirk"
        assert _clean_name("Isaac GuerendoO") == "Isaac Guerendo"
        assert _clean_name("Deebo SamuelIR") == "Deebo Samuel"   # attached multi-letter tag
        assert _clean_name("De'Zhaun Stribling") == "De'Zhaun Stribling"
        assert _clean_name("-") == "-"


class TestHandcuffFromDepth:
    def test_first_backup_is_the_handcuff(self):
        dc = [{"position": "Christian McCaffrey", "players": ["Jordan James", "Kaelon Black"]}]
        assert handcuff_from_depth(dc, "Christian McCaffrey") == ("Jordan James", "depth")

    def test_cleans_tags_on_key_and_backup(self):
        dc = [{"position": "Isaac GuerendoO", "players": ["Backup GuyQ", "-"]}]
        assert handcuff_from_depth(dc, "Isaac Guerendo") == ("Backup Guy", "depth")

    def test_no_backup_listed(self):
        dc = [{"position": "Lead Back", "players": ["-", "-"]}]
        assert handcuff_from_depth(dc, "Lead Back") == (None, "no_backup_listed")

    def test_player_is_already_a_backup(self):
        dc = [{"position": "Star", "players": ["Your Guy", "Third"]}]
        assert handcuff_from_depth(dc, "Your Guy") == (None, "you_roster_a_backup")

    def test_not_on_chart(self):
        dc = [{"position": "Star", "players": ["Backup"]}]
        assert handcuff_from_depth(dc, "Traded Away") == (None, "not_on_depth_chart")


class TestAvailability:
    def test_free_agent(self):
        assert _availability("x", {}, my_roster_id=1) == "free_agent"
        assert _availability(None, {"x": 1}, my_roster_id=1) == "free_agent"

    def test_yours_vs_opponent(self):
        assert _availability("x", {"x": 1}, my_roster_id=1) == "yours"
        assert _availability("x", {"x": 2}, my_roster_id=1) == "rostered_by_opponent"


class _DB:
    def __init__(self, by_id, by_team):
        self._by_id, self._by_team = by_id, by_team

    def get_athletes_by_ids(self, ids):
        return {i: self._by_id[i] for i in ids if i in self._by_id}

    def get_athletes_by_team(self, team):
        return self._by_team.get(team, [])


class TestGetHandcuffMap:
    def _db(self):
        return _DB(
            by_id={
                "rb_star": {"id": "rb_star", "full_name": "Star Back", "position": "RB", "team_id": "SF"},
                "wr1": {"id": "wr1", "full_name": "Wide Guy", "position": "WR", "team_id": "SF"},
            },
            by_team={"SF": [
                {"id": "rb_star", "full_name": "Star Back", "position": "RB"},
                {"id": "hc1", "full_name": "Handcuff Back", "position": "RB"},
            ]},
        )

    def _rosters(self, hc_owner=None):
        rosters = [
            {"roster_id": 1, "players": ["rb_star", "wr1"]},
            {"roster_id": 2, "players": ["some_other"]},
        ]
        if hc_owner:
            next(r for r in rosters if r["roster_id"] == hc_owner)["players"].append("hc1")
        return {"rosters": rosters, "success": True}

    async def _run(self, hc_owner=None):
        # Real ESPN shape: row keyed by the starter, backups follow (with a tag).
        depth = {"depth_chart": [{"position": "Star Back", "players": ["Handcuff BackO", "-"]}]}
        with patch("nfl_mcp.sleeper_tools.get_rosters", new=AsyncMock(return_value=self._rosters(hc_owner))), \
             patch("nfl_mcp.nfl_tools.get_depth_chart", new=AsyncMock(return_value=depth)):
            return await get_handcuff_map("123", roster_id=1, db=self._db())

    @pytest.mark.asyncio
    async def test_free_agent_handcuff_is_priority(self):
        res = await self._run(hc_owner=None)  # hc1 unrostered
        assert res["success"] is True
        hc = next(h for h in res["handcuffs"] if h["starter"] == "Star Back")
        assert hc["handcuff"] == "Handcuff Back"        # injury tag stripped
        assert hc["handcuff_player_id"] == "hc1"
        assert hc["handcuff_status"] == "free_agent"
        assert res["priority_free_agents"][0]["handcuff"] == "Handcuff Back"

    @pytest.mark.asyncio
    async def test_opponent_owned_handcuff(self):
        res = await self._run(hc_owner=2)  # hc1 on opponent roster
        hc = next(h for h in res["handcuffs"] if h["starter"] == "Star Back")
        assert hc["handcuff_status"] == "rostered_by_opponent"
        assert res["priority_free_agents"] == []

    @pytest.mark.asyncio
    async def test_roster_not_found(self):
        with patch("nfl_mcp.sleeper_tools.get_rosters",
                   new=AsyncMock(return_value={"rosters": [{"roster_id": 9, "players": []}]})):
            res = await get_handcuff_map("123", roster_id=1, db=self._db())
        assert res["success"] is False
        assert "not found" in res["error"]

    @pytest.mark.asyncio
    async def test_db_required(self):
        res = await get_handcuff_map("123", roster_id=1, db=None)
        assert res["success"] is False
        assert "database" in res["error"]
