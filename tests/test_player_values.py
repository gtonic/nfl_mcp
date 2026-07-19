"""Tests for the consensus player-value layer (player_values.py).

Network is never touched: the FantasyCalc fetch is patched with canned data so
these tests are deterministic and CI-safe.
"""

import tempfile
import os
from unittest.mock import patch

import pytest

from nfl_mcp import player_values as pv
from nfl_mcp.database import NFLDatabase


# Raw FantasyCalc-shaped entries (subset of the real schema)
RAW_FC = [
    {"player": {"name": "Bijan Robinson", "sleeperId": "9509", "position": "RB", "maybeTeam": "ATL"},
     "value": 10000, "overallRank": 1, "positionRank": 1, "redraftValue": 10000, "maybeTier": 1, "trend30Day": 50},
    {"player": {"name": "Ja'Marr Chase", "sleeperId": "6794", "position": "WR", "maybeTeam": "CIN"},
     "value": 9500, "overallRank": 2, "positionRank": 1, "redraftValue": 9500, "maybeTier": 1, "trend30Day": 10},
    {"player": {"name": "Patrick Mahomes", "sleeperId": "4046", "position": "QB", "maybeTeam": "KC"},
     "value": 4000, "overallRank": 30, "positionRank": 3, "redraftValue": 4000, "maybeTier": 4, "trend30Day": -5},
    # Entry without a sleeperId must be dropped (cannot join to roster data)
    {"player": {"name": "No Sleeper Id", "sleeperId": None, "position": "WR"}, "value": 100, "overallRank": 200},
]

NORMALIZED = [n for n in (pv._normalize_fantasycalc_entry(e) for e in RAW_FC) if n]


def _temp_db():
    return NFLDatabase(tempfile.mktemp(suffix=".db"))


class TestPureHelpers:
    def test_scoring_to_ppr(self):
        assert pv.scoring_to_ppr("ppr") == 1.0
        assert pv.scoring_to_ppr("half-ppr") == 0.5
        assert pv.scoring_to_ppr("standard") == 0.0
        assert pv.scoring_to_ppr(None) == 1.0
        assert pv.scoring_to_ppr("0.5") == 0.5
        assert pv.scoring_to_ppr("garbage") == 1.0

    def test_build_format_key_distinct(self):
        a = pv.build_format_key(1.0, 1, 12, False)
        b = pv.build_format_key(1.0, 2, 12, False)  # superflex
        c = pv.build_format_key(0.5, 1, 12, False)  # half
        d = pv.build_format_key(1.0, 1, 12, True)   # dynasty
        assert len({a, b, c, d}) == 4

    def test_normalize_name(self):
        assert pv.normalize_name("Ja'Marr Chase") == "jamarr chase"
        assert pv.normalize_name("Michael Pittman Jr.") == "michael pittman"
        assert pv.normalize_name("") == ""

    def test_normalize_entry_drops_without_sleeper_id(self):
        assert pv._normalize_fantasycalc_entry(RAW_FC[3]) is None
        norm = pv._normalize_fantasycalc_entry(RAW_FC[0])
        assert norm["player_id"] == "9509"
        assert norm["position"] == "RB"
        assert norm["source"] == "fantasycalc"


class TestService:
    async def test_fetch_indexes_and_persists(self):
        db = _temp_db()
        svc = pv.PlayerValuesService(db=db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(NORMALIZED)):
            data = await svc.get_values(ppr=1.0, num_qbs=1, num_teams=12, is_dynasty=False)
        assert data["source"] == "fantasycalc"
        assert data["stale"] is False
        assert data["count"] == 3  # entry w/o sleeperId dropped
        # index lookups
        assert svc.lookup(data, player_id="9509")["name"] == "Bijan Robinson"
        assert svc.lookup(data, name="Ja'Marr Chase")["player_id"] == "6794"
        # persisted to DB
        key = pv.build_format_key(1.0, 1, 12, False)
        assert db.get_player_values_last_updated(key) is not None

    async def test_memory_cache_prevents_second_fetch(self):
        db = _temp_db()
        svc = pv.PlayerValuesService(db=db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(NORMALIZED)) as m:
            await svc.get_values(1.0, 1, 12, False)
            await svc.get_values(1.0, 1, 12, False)
        assert m.call_count == 1  # second call served from memory

    async def test_db_stale_fallback_when_api_fails(self):
        db = _temp_db()
        # Seed DB via a first successful service, then a fresh service with a failing API.
        svc1 = pv.PlayerValuesService(db=db)
        with patch.object(svc1, "_fetch_from_fantasycalc", return_value=list(NORMALIZED)):
            await svc1.get_values(1.0, 1, 12, False)

        svc2 = pv.PlayerValuesService(db=db)

        async def boom(*a, **k):
            raise RuntimeError("api down")

        with patch.object(svc2, "_fetch_from_fantasycalc", side_effect=boom):
            data = await svc2.get_values(1.0, 1, 12, False)
        assert data["source"] == "db_cache"
        assert data["stale"] is True
        assert data["count"] == 3

    async def test_position_filter_lookup(self):
        db = _temp_db()
        svc = pv.PlayerValuesService(db=db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(NORMALIZED)):
            data = await svc.get_values(1.0, 1, 12, False)
        # name lookup with wrong position must miss
        assert svc.lookup(data, name="Bijan Robinson", position="WR") is None
        assert svc.lookup(data, name="Bijan Robinson", position="RB") is not None


class TestTools:
    async def test_get_player_values_tool(self):
        db = _temp_db()
        pv._service = None  # reset singleton
        svc = pv.get_values_service(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(NORMALIZED)):
            res = await pv.get_player_values(scoring="ppr", position="RB", limit=5, db=db)
        assert res["success"] is True
        assert res["total"] == 1
        assert res["values"][0]["name"] == "Bijan Robinson"
        pv._service = None

    async def test_get_player_value_tool_by_id(self):
        db = _temp_db()
        pv._service = None
        svc = pv.get_values_service(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(NORMALIZED)):
            res = await pv.get_player_value(player_id="4046", db=db)
        assert res["success"] is True
        assert res["found"] is True
        assert res["value"]["name"] == "Patrick Mahomes"
        pv._service = None

    async def test_get_player_value_requires_arg(self):
        res = await pv.get_player_value(db=_temp_db())
        assert res["success"] is False
