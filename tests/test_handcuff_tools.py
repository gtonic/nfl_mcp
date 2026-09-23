"""Tests for handcuff mapping (nfl_mcp.handcuff_tools)."""
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp.handcuff_tools import (
    _availability,
    _clean_name,
    _match_athlete,
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
    """Rows are {"position": <position label>, "players": [starter, backup...]}.

    These previously used a player name as `position`, a shape nothing
    produces — which is precisely why the lookup could be dead code and still
    show green.
    """

    def test_first_backup_is_the_handcuff(self):
        dc = [{"position": "RB", "players": ["Christian McCaffrey", "Jordan James"]}]
        assert handcuff_from_depth(dc, "Christian McCaffrey") == ("Jordan James", "depth")

    def test_cleans_tags_on_starter_and_backup(self):
        dc = [{"position": "RB", "players": ["Isaac GuerendoO", "Backup GuyQ", "-"]}]
        assert handcuff_from_depth(dc, "Isaac Guerendo") == ("Backup Guy", "depth")

    def test_no_backup_listed(self):
        dc = [{"position": "RB", "players": ["Lead Back", "-", "-"]}]
        assert handcuff_from_depth(dc, "Lead Back") == (None, "no_backup_listed")

    def test_player_is_already_a_backup(self):
        dc = [{"position": "RB", "players": ["Star", "Your Guy"]}]
        assert handcuff_from_depth(dc, "Your Guy") == (None, "you_roster_a_backup")

    def test_not_on_chart(self):
        dc = [{"position": "RB", "players": ["Star", "Backup"]}]
        assert handcuff_from_depth(dc, "Traded Away") == (None, "not_on_depth_chart")


class TestAvailability:
    def test_free_agent(self):
        assert _availability("x", {}, my_roster_id=1) == "free_agent"

    def test_unmatched_player_is_unknown_not_free_agent(self):
        assert _availability(None, {"x": 1}, my_roster_id=1) == "unknown"

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
        # Real get_depth_chart shape: position label, starter first in `players`
        # (with an injury tag on the backup). The previous mock keyed the row by
        # the starter's name — a shape nothing produces, which is how the
        # lookup stayed dead code with a green suite.
        depth = {"depth_chart": [
            {"position": "RB", "players": ["Star Back", "Handcuff BackO", "-"]}
        ]}
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


class TestMatchAthlete:
    TEAM = [
        {"id": "1", "full_name": "Kenneth Walker", "position": "RB"},
        {"id": "2", "full_name": "D.J. Giddens", "position": "RB"},
        {"id": "3", "full_name": "Zach Charbonnet", "position": "RB"},
        {"id": "4", "full_name": "Cameron Skattebo", "position": "RB"},
    ]

    def test_suffix_and_punctuation(self):
        assert _match_athlete("Kenneth Walker III", self.TEAM)["id"] == "1"
        assert _match_athlete("DJ Giddens", self.TEAM)["id"] == "2"

    def test_first_initial_and_last_name(self):
        assert _match_athlete("Cam Skattebo", self.TEAM)["id"] == "4"

    def test_no_match(self):
        assert _match_athlete("Nobody Here", self.TEAM) is None


class TestUnmatchedHandcuff:
    async def _run(self, db, players, depth_row):
        depth = {"depth_chart": [{"position": "RB", "players": depth_row}]}
        rosters = {"rosters": [{"roster_id": 1, "players": players}], "success": True}
        with patch("nfl_mcp.sleeper_tools.get_rosters", new=AsyncMock(return_value=rosters)), \
             patch("nfl_mcp.nfl_tools.get_depth_chart", new=AsyncMock(return_value=depth)):
            return await get_handcuff_map("123", roster_id=1, db=db)

    @pytest.mark.asyncio
    async def test_unmatched_name_is_unknown(self):
        db = _DB(
            by_id={"rb_star": {"id": "rb_star", "full_name": "Star Back", "position": "RB",
                               "team_id": "SF"}},
            by_team={"SF": [{"id": "rb_star", "full_name": "Star Back", "position": "RB"}]},
        )
        res = await self._run(db, ["rb_star"], ["Star Back", "Mystery Man"])
        hc = res["handcuffs"][0]
        assert hc["handcuff"] == "Mystery Man"
        assert hc["handcuff_status"] == "unknown"
        assert res["priority_free_agents"] == []

    @pytest.mark.asyncio
    async def test_rostered_backup_reports_the_starter_he_backs_up(self):
        db = _DB(
            by_id={"rb2": {"id": "rb2", "full_name": "Second Back", "position": "RB",
                           "team_id": "SF"}},
            by_team={"SF": []},
        )
        res = await self._run(db, ["rb2"], ["Star Back", "Second Back", "Third Back"])
        hc = res["handcuffs"][0]
        assert hc["handcuff"] is None          # not "Third Back"
        assert hc["match"] == "you_roster_a_backup"
        assert hc["backs_up"] == "Star Back"
