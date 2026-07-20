"""Tests for the draft assistant (draft_tools.py).

Values and Sleeper draft calls are patched so tests are deterministic/offline.
"""

import tempfile
from unittest.mock import patch

import pytest

from nfl_mcp import draft_tools as dt
from nfl_mcp import player_values as pv
from nfl_mcp.database import NFLDatabase


# A small but position-diverse value pool.
POOL = [
    {"player_id": "1", "name": "RB One", "position": "RB", "team": "ATL", "value": 10000, "overall_rank": 1, "position_rank": 1, "tier": 1, "trend_30day": 5},
    {"player_id": "2", "name": "RB Two", "position": "RB", "team": "DAL", "value": 8000, "overall_rank": 4, "position_rank": 2, "tier": 1, "trend_30day": 0},
    {"player_id": "3", "name": "WR One", "position": "WR", "team": "CIN", "value": 9500, "overall_rank": 2, "position_rank": 1, "tier": 1, "trend_30day": 2},
    {"player_id": "4", "name": "WR Two", "position": "WR", "team": "LAR", "value": 9000, "overall_rank": 3, "position_rank": 2, "tier": 2, "trend_30day": 1},
    {"player_id": "5", "name": "QB One", "position": "QB", "team": "BUF", "value": 5000, "overall_rank": 10, "position_rank": 1, "tier": 3, "trend_30day": 0},
    {"player_id": "6", "name": "TE One", "position": "TE", "team": "KC", "value": 4000, "overall_rank": 12, "position_rank": 1, "tier": 3, "trend_30day": 0},
]


def _temp_db():
    return NFLDatabase(tempfile.mktemp(suffix=".db"))


def _service_with_pool(db):
    pv._service = None
    svc = pv.get_values_service(db)
    return svc


class TestPureHelpers:
    def test_replacement_baselines_superflex(self):
        base = dt.replacement_baselines(12, superflex=False)
        base_sf = dt.replacement_baselines(12, superflex=True)
        assert base["QB"] == 12
        assert base_sf["QB"] == 24  # superflex doubles QB baseline
        assert base["WR"] > base["QB"]

    def test_compute_vbd_orders_by_value_over_replacement(self):
        out = dt.compute_vbd(POOL, num_teams=12, superflex=False)
        # every ranked player gets a vbd
        assert all(p["vbd"] is not None for p in out["players"])
        # replacement value recorded per position
        assert set(out["replacement"].keys()) == {"RB", "WR", "QB", "TE"}
        # highest vbd first
        vbds = [p["vbd"] for p in out["players"]]
        assert vbds == sorted(vbds, reverse=True)

    def test_starter_requirements(self):
        reqs = dt._starter_requirements({"slots_qb": 1, "slots_rb": 2, "slots_wr": 3, "slots_te": 1, "slots_flex": 1})
        assert reqs == {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1}

    def test_starter_requirements_counts_all_flex_variants(self):
        # Real Sleeper league: flex + wrrb_flex + rec_flex = 3 flex slots; super_flex -> QB.
        reqs = dt._starter_requirements({
            "slots_qb": 1, "slots_rb": 2, "slots_wr": 2, "slots_te": 1,
            "slots_flex": 1, "slots_wrrb_flex": 1, "slots_rec_flex": 1,
            "slots_super_flex": 1,
        })
        assert reqs["FLEX"] == 3
        assert reqs["QB"] == 2  # base QB + superflex

    def test_need_multiplier(self):
        reqs = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}
        # need a starter -> boosted
        mult, label = dt._need_multiplier("RB", {"RB": 0}, reqs, flex_filled=0)
        assert mult > 1 and label == "need_starter"
        # overfilled -> discounted
        mult, label = dt._need_multiplier("RB", {"RB": 4}, reqs, flex_filled=1)
        assert mult < 1 and label == "overfilled"


class TestDraftBoard:
    async def test_get_draft_board(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)):
            res = await dt.get_draft_board(scoring="ppr", num_teams=12, limit=10, db=db)
        assert res["success"] is True
        assert res["total"] == len(POOL)
        # board sorted by vbd desc
        vbds = [p["vbd"] for p in res["board"]]
        assert vbds == sorted(vbds, reverse=True)
        assert "RB" in res["tiers_by_position"]
        pv._service = None

    async def test_get_draft_board_position_filter(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)):
            res = await dt.get_draft_board(scoring="ppr", position="WR", db=db)
        assert all(p["position"] == "WR" for p in res["board"])
        pv._service = None


class TestRecommendPick:
    DRAFT = {
        "success": True,
        "draft": {
            "draft_id": "d1", "type": "snake", "status": "drafting",
            "settings": {"teams": 12, "rounds": 15, "slots_qb": 1, "slots_rb": 2,
                         "slots_wr": 2, "slots_te": 1, "slots_flex": 1, "slots_bn": 7},
            "metadata": {"scoring_type": "ppr"},
        },
    }

    def _picks(self, picks):
        return {"success": True, "picks": picks}

    async def test_recommend_weights_by_roster_need(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        # My slot=3 already has 2 RBs -> RB starters filled, should prefer WR/QB/TE.
        picks = [
            {"player_id": "1", "draft_slot": 3, "round": 1, "metadata": {"position": "RB", "first_name": "RB", "last_name": "One"}},
            {"player_id": "2", "draft_slot": 3, "round": 2, "metadata": {"position": "RB", "first_name": "RB", "last_name": "Two"}},
        ]
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)), \
             patch.object(dt, "get_draft", return_value=self.DRAFT), \
             patch.object(dt, "get_draft_picks", return_value=self._picks(picks)):
            res = await dt.recommend_draft_pick("d1", my_slot=3, num_suggestions=3, db=db)
        assert res["success"] is True
        assert res["picks_made"] == 2
        # drafted RBs must not be suggested
        suggested_ids = {s["player_id"] for s in res["suggestions"]}
        assert "1" not in suggested_ids and "2" not in suggested_ids
        # top suggestion should NOT be an (overfilled) RB
        assert res["top_pick"]["position"] != "RB"
        assert res["my_roster"]["position_counts"]["RB"] == 2
        pv._service = None

    async def test_recommend_best_available_without_slot(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)), \
             patch.object(dt, "get_draft", return_value=self.DRAFT), \
             patch.object(dt, "get_draft_picks", return_value=self._picks([])):
            res = await dt.recommend_draft_pick("d1", my_slot=None, num_suggestions=3, db=db)
        assert res["success"] is True
        assert res["my_roster"] is None
        # highest-VBD player leads
        assert res["top_pick"]["vbd"] == max(s["vbd"] for s in res["suggestions"])
        pv._service = None

    async def test_recommend_requires_draft_id(self):
        res = await dt.recommend_draft_pick("", db=_temp_db())
        assert res["success"] is False


# A larger pool so a full mock draft can fill starters + bench for all teams
# without the player pool starving under position caps.
def _big_pool(n_per_pos=60):
    pool = []
    rank = 1
    for pos, base in (("RB", 10000), ("WR", 9800), ("QB", 6000), ("TE", 5000)):
        for i in range(n_per_pos):
            pool.append({
                "player_id": f"{pos}{i}", "name": f"{pos} Player {i}", "position": pos,
                "team": "ATL", "value": base - i * 100, "overall_rank": rank,
                "position_rank": i + 1, "tier": (i // 6) + 1, "trend_30day": 0,
            })
            rank += 1
    return pool


class TestSimulateDraft:
    async def _run(self, db, **kw):
        svc = _service_with_pool(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=_big_pool()):
            return await dt.simulate_draft(db=db, **kw)

    async def test_single_sim_fills_starters(self):
        db = _temp_db()
        res = await self._run(db, my_slot=3, num_teams=12, rounds=15, seed=42)
        assert res["success"] is True
        sample = res["sample"]
        assert len(sample["my_team"]) == 15
        # roster must satisfy starter requirements (QB1/RB2/WR2/TE1)
        assert sample["starters_filled"] is True
        counts = sample["my_position_counts"]
        assert counts.get("QB", 0) >= 1 and counts.get("TE", 0) >= 1
        # no position wildly over-stacked (caps enforced)
        assert counts.get("WR", 0) <= 7 and counts.get("RB", 0) <= 7
        assert 1 <= sample["my_value_rank"] <= 12
        pv._service = None

    async def test_deterministic_with_seed(self):
        db = _temp_db()
        a = await self._run(db, my_slot=5, num_teams=10, seed=7)
        b = await self._run(db, my_slot=5, num_teams=10, seed=7)
        assert [r["player_id"] for r in a["sample"]["my_team"]] == \
               [r["player_id"] for r in b["sample"]["my_team"]]
        pv._service = None

    async def test_multi_sim_aggregate(self):
        db = _temp_db()
        res = await self._run(db, my_slot=1, num_teams=12, num_sims=10, seed=1)
        assert res["num_sims"] == 10
        agg = res["aggregate"]
        assert "avg_position_counts" in agg
        assert "avg_value_rank" in agg
        assert sum(agg["grade_distribution"].values()) == 10
        pv._service = None

    async def test_invalid_slot(self):
        db = _temp_db()
        res = await self._run(db, my_slot=20, num_teams=12)
        assert res["success"] is False
        pv._service = None
