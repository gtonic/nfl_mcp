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


@pytest.fixture(autouse=True)
def _offline_injury_feed():
    """Keep the suite offline now that the advisor consults the injury feed.

    Without this every recommend/simulate test hits the live ESPN aggregator,
    which took the file from 0.5s to 100s. Tests that care about injuries patch
    over this with their own index.
    """
    dt._depth_chart_cache.clear()  # process-global: would leak between tests
    with patch.object(dt, "_injury_index", return_value={}):
        yield
    dt._depth_chart_cache.clear()


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

    def test_compute_vbd_attaches_vbd_and_orders_by_value(self):
        out = dt.compute_vbd(POOL, num_teams=12, superflex=False)
        # every ranked player still gets a vbd (kept as a within-position read)
        assert all(p["vbd"] is not None for p in out["players"])
        # replacement value recorded per position
        assert set(out["replacement"].keys()) == {"RB", "WR", "QB", "TE"}
        # ...but the ordering is by market value, which is what compares across
        # positions. See draft_currency() and TestPositionalBiasRegressions.
        values = [p["value"] for p in out["players"]]
        assert values == sorted(values, reverse=True)

    def test_starter_requirements(self):
        reqs = dt._starter_requirements({"slots_qb": 1, "slots_rb": 2, "slots_wr": 3,
                                         "slots_te": 1, "slots_flex": 1, "slots_def": 1})
        assert reqs == {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "DEF": 1, "K": 0}

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
        # board sorted by market value desc (not VBD -- see draft_currency)
        values = [p["value"] for p in res["board"]]
        assert values == sorted(values, reverse=True)
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
        # highest-value player leads (no slot -> no need weighting)
        assert res["top_pick"]["value"] == max(s["value"] for s in res["suggestions"])
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


class TestPositionalBiasRegressions:
    """Guards against the two roster-construction bugs found in a live mock draft.

    A 12-team half-PPR mock produced a roster with seven tight ends and pushed a
    third quarterback ahead of startable skill players. Both came from ranking
    cross-position on a scale that isn't cross-position comparable.
    """

    @staticmethod
    def _curved_pool():
        """Pool that reproduces the real value curves per position.

        TE collapses fast (the 14th is nearly worthless), RB/WR decline gently.
        That asymmetry is what makes TE replacement level far lower than RB/WR
        and hands every TE a flat VBD head start.
        """
        pool = []
        for i in range(40):  # RB/WR: gentle decline, still valuable at baseline
            pool.append({"player_id": f"r{i}", "name": f"RB {i:02d}", "position": "RB",
                         "team": "SF", "value": 6000 - i * 115, "tier": i // 4})
            pool.append({"player_id": f"w{i}", "name": f"WR {i:02d}", "position": "WR",
                         "team": "ARI", "value": 5800 - i * 110, "tier": i // 4})
        for i in range(18):  # TE: cliff after the elite handful
            pool.append({"player_id": f"t{i}", "name": f"TE {i:02d}", "position": "TE",
                         "team": "KC", "value": max(150, 5000 - i * 300), "tier": i // 3})
        for i in range(15):
            pool.append({"player_id": f"q{i}", "name": f"QB {i:02d}", "position": "QB",
                         "team": "BUF", "value": 5200 - i * 250, "tier": i // 3})
        return pool

    def test_te_replacement_offset_exists(self):
        """The distortion this guards against must actually be present."""
        vbd = dt.compute_vbd(self._curved_pool(), num_teams=12, superflex=False)
        rep = vbd["replacement"]
        # TE baseline (n*1.2 = 14) lands deep in the collapsed tail.
        assert rep["TE"] < rep["WR"] < rep["RB"]

    def test_ranking_is_not_biased_toward_te(self):
        """Equal-need positions must rank by market value, not by VBD offset.

        Regression: ranking on VBD put a cheaper TE ahead of a pricier RB/WR,
        every round, which is what produced a seven-tight-end roster.
        """
        vbd = dt.compute_vbd(self._curved_pool(), num_teams=12, superflex=False)
        by_name = {p["name"]: p for p in vbd["players"]}
        te, rb = by_name["TE 07"], by_name["RB 20"]
        # Same tier of player: the TE is worth less on the market ...
        assert te["value"] < rb["value"]
        # ... but VBD would rank it ahead. That's the trap.
        assert te["vbd"] > rb["vbd"]

        reqs = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2}
        counts = {"RB": 2, "WR": 2, "TE": 1, "QB": 1}  # starters full, flex open
        ranked = dt._need_weighted_ranking(vbd["players"], counts, reqs)
        order = [p["name"] for p, _ in ranked]
        assert order.index("RB 20") < order.index("TE 07")

        # No TE pile-up at the top once the base TE slot is filled. On this pool
        # VBD ranking put 4 TEs in the top 20, value ranking puts 1.
        top20 = [p["position"] for p, _ in ranked[:20]]
        assert top20.count("TE") <= 2, f"TE over-represented: {top20}"

    def test_third_qb_is_penalised_in_single_qb_league(self):
        """With 1 QB slot and no superflex, a third QB is dead weight."""
        reqs = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2}
        first = dt._need_multiplier("QB", {"QB": 0}, reqs, flex_filled=0)
        backup = dt._need_multiplier("QB", {"QB": 1}, reqs, flex_filled=0)
        third = dt._need_multiplier("QB", {"QB": 2}, reqs, flex_filled=0)
        assert first == (2.0, "need_starter")
        assert backup[0] == 1.0
        # A third QB cannot enter the lineup at all -- harsher than "overfilled",
        # which is reserved for RB/WR surplus that still has flex/bye value.
        assert third == (0.15, "dead_weight")
        assert third[0] < dt._need_multiplier("RB", {"RB": 4}, reqs, 2)[0]

    def test_superflex_still_allows_two_starting_qbs(self):
        """The QB cap keys off the slot count, so superflex isn't punished."""
        reqs = {"QB": 2, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}  # slots_qb+super_flex
        assert dt._need_multiplier("QB", {"QB": 1}, reqs, 0) == (2.0, "need_starter")
        assert dt._need_multiplier("QB", {"QB": 2}, reqs, 0)[0] == 1.0
        assert dt._need_multiplier("QB", {"QB": 3}, reqs, 0) == (0.15, "dead_weight")


class TestInjuryAwareness:
    """The injury feed is advisory for Questionable and priced only above it.

    Calibrated against the live preseason feed: of 195 board players, 42 carried
    an injury record and 40 of those were "Questionable" -- including Mahomes
    ("on track to start Week 1") and McCaffrey (a planned camp rest), both with
    severity 2 / confidence 65 and a mislabelled "Knee - ACL". Only 2 players
    carried a status that actually costs games.
    """

    def test_questionable_is_flagged_but_not_priced(self):
        assert dt._injury_multiplier("Questionable") == 1.0
        assert dt._injury_multiplier(None) == 1.0
        assert dt._injury_multiplier("Active") == 1.0

    def test_missing_time_is_discounted(self):
        assert dt._injury_multiplier("Out") < 0.5
        assert dt._injury_multiplier("IR") < dt._injury_multiplier("Out")
        assert dt._injury_multiplier("Doubtful") < 1.0
        assert dt._injury_multiplier("Suspended") < 1.0
        # Case and whitespace from the feed must not defeat the lookup.
        assert dt._injury_multiplier("  out ") == dt._injury_multiplier("OUT")

    def test_name_matching_survives_punctuation_and_suffixes(self):
        assert dt._norm_name("Ja'Marr Chase") == dt._norm_name("JaMarr Chase")
        assert dt._norm_name("Marvin Harrison Jr.") == dt._norm_name("Marvin Harrison")
        assert dt._norm_name("De'Von Achane") == dt._norm_name("DeVon Achane")
        assert dt._norm_name("A.J. Brown") == dt._norm_name("AJ Brown")

    def test_team_mismatch_rejects_a_same_name_player(self):
        index = {dt._norm_name("Mike Williams"): {
            "player_name": "Mike Williams", "team_id": "NYJ",
            "injury_status": "Out", "injury_type": "Back"}}
        assert dt._injury_for({"name": "Mike Williams", "team": "NYJ"}, index) is not None
        assert dt._injury_for({"name": "Mike Williams", "team": "LAC"}, index) is None
        # A missing team on either side must not block the match.
        assert dt._injury_for({"name": "Mike Williams", "team": None}, index) is not None

    def test_index_keeps_the_most_severe_duplicate(self):
        # Same name twice: the status that costs games has to win.
        recs = [{"player_name": "Dup Guy", "injury_status": "Questionable"},
                {"player_name": "Dup Guy", "injury_status": "Out"}]
        index = {}
        for r in recs:
            k = dt._norm_name(r["player_name"])
            prev = index.get(k)
            if prev is None or dt._injury_multiplier(r["injury_status"]) < dt._injury_multiplier(prev["injury_status"]):
                index[k] = r
        assert index[dt._norm_name("Dup Guy")]["injury_status"] == "Out"

    async def test_recommend_survives_a_dead_injury_feed(self):
        """A broken feed must never take the live draft assistant down."""
        db = _temp_db()
        svc = _service_with_pool(db)
        draft = TestRecommendPick.DRAFT
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)), \
             patch.object(dt, "get_draft", return_value=draft), \
             patch.object(dt, "get_draft_picks", return_value={"success": True, "picks": []}), \
             patch("nfl_mcp.injury_service.get_injury_reports", side_effect=RuntimeError("feed down")):
            res = await dt.recommend_draft_pick("d1", my_slot=3, num_suggestions=3, db=db)
        assert res["success"] is True
        assert len(res["suggestions"]) == 3
        assert all(s["injury"] is None for s in res["suggestions"])
        pv._service = None


class TestUnrankableStarterSlots:
    """DEF/K are real starter slots that can never appear in `suggestions`.

    Regression: a completed 12-team mock ended with the assistant offering a
    fourth QB in the last round while the DEF slot sat empty. Eight of twelve
    defenses were already gone by pick 160, so a silent gap is expensive.
    """

    REQS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "DEF": 1, "K": 0}

    def test_def_slot_is_read_from_settings(self):
        reqs = dt._starter_requirements({"slots_qb": 1, "slots_def": 1, "slots_k": 1})
        assert reqs["DEF"] == 1 and reqs["K"] == 1

    def test_gap_reported_while_slot_unfilled(self):
        gaps = dt._unrankable_gaps({"WR": 5, "RB": 4}, self.REQS, picks_left=4)
        assert [g["position"] for g in gaps] == ["DEF"]
        assert gaps[0]["needed"] == 1
        assert gaps[0]["urgent"] is False  # 4 picks for 1 slot -- still relaxed

    def test_gap_turns_urgent_on_the_last_pick(self):
        gaps = dt._unrankable_gaps({"WR": 5}, self.REQS, picks_left=1)
        assert gaps[0]["urgent"] is True

    def test_no_gap_once_filled(self):
        assert dt._unrankable_gaps({"DEF": 1}, self.REQS, picks_left=1) == []

    def test_zero_slot_positions_are_never_demanded(self):
        # This league has no kicker; K must not show up as a gap.
        gaps = dt._unrankable_gaps({}, self.REQS, picks_left=1)
        assert all(g["position"] != "K" for g in gaps)

    def test_my_picks_remaining_follows_snake(self):
        # Slot 9 of 12 over 15 rounds -> picks 9,16,33,...,177.
        assert dt._my_picks_remaining(0, my_slot=9, num_teams=12, rounds=15) == 15
        assert dt._my_picks_remaining(160, my_slot=9, num_teams=12, rounds=15) == 1
        assert dt._my_picks_remaining(177, my_slot=9, num_teams=12, rounds=15) == 0


class TestDraftTilts:
    """Playoff schedule and handcuff signals: tiebreakers, never drivers."""

    def test_playoff_tilt_is_bounded(self):
        idx = {"WR": {"SF": 26.8, "CLE": 86.0}, "RB": {"MIN": 68.0}}
        soft, _ = dt._playoff_tilt({"position": "WR", "team": "CLE"}, idx)
        hard, _ = dt._playoff_tilt({"position": "WR", "team": "SF"}, idx)
        assert soft > 1.0 > hard
        # Must stay small enough that it cannot jump a tier.
        assert 1.0 < soft < 1.05 and 0.95 < hard < 1.0

    def test_playoff_tilt_neutral_when_unknown(self):
        assert dt._playoff_tilt({"position": "WR", "team": "XXX"}, {}) == (1.0, None)
        assert dt._playoff_tilt({"position": None, "team": None}, {"WR": {}}) == (1.0, None)

    def test_tilt_cannot_outrank_a_real_value_gap(self):
        """A soft schedule must not lift a clearly worse player over a better one."""
        idx = {"WR": {"CLE": 88.0, "SF": 13.0}}
        good = {"position": "WR", "team": "SF", "value": 3000}
        weak = {"position": "WR", "team": "CLE", "value": 2500}
        g = good["value"] * dt._playoff_tilt(good, idx)[0]
        w = weak["value"] * dt._playoff_tilt(weak, idx)[0]
        assert g > w

    async def test_handcuff_index_finds_the_backup(self):
        chart = {"depth_chart": [
            {"position": "RB", "players": ["Javonte Williams", "Jaydon Blue", "Phil Mafah"]},
            {"position": "WR", "players": ["CeeDee Lamb"]},
        ]}
        with patch("nfl_mcp.nfl_tools.get_depth_chart", return_value=chart):
            idx = await dt._handcuff_index(
                [{"name": "Javonte Williams", "position": "RB", "team": "DAL"}])
        assert idx[dt._norm_name("Jaydon Blue")] == "Javonte Williams"
        assert idx[dt._norm_name("Phil Mafah")] == "Javonte Williams"
        # The starter himself is not his own handcuff.
        assert dt._norm_name("Javonte Williams") not in idx

    async def test_handcuff_index_ignores_non_rb_and_survives_failure(self):
        assert await dt._handcuff_index([{"name": "CeeDee Lamb", "position": "WR", "team": "DAL"}]) == {}
        with patch("nfl_mcp.nfl_tools.get_depth_chart", side_effect=RuntimeError("espn down")):
            assert await dt._handcuff_index(
                [{"name": "Javonte Williams", "position": "RB", "team": "DAL"}]) == {}

    async def test_handcuff_index_handles_starter_missing_from_chart(self):
        chart = {"depth_chart": [{"position": "RB", "players": ["Someone Else"]}]}
        with patch("nfl_mcp.nfl_tools.get_depth_chart", return_value=chart):
            assert await dt._handcuff_index(
                [{"name": "Javonte Williams", "position": "RB", "team": "DAL"}]) == {}


class TestDepthChartCache:
    """Depth charts are static during a draft; refetching them per pick is waste."""

    CHART = {"depth_chart": [{"position": "RB", "players": ["Starter Guy", "Backup Guy"]}]}

    async def test_second_lookup_does_not_refetch(self):
        roster = [{"name": "Starter Guy", "position": "RB", "team": "DAL"}]
        with patch("nfl_mcp.nfl_tools.get_depth_chart", return_value=self.CHART) as mock:
            first = await dt._handcuff_index(roster)
            second = await dt._handcuff_index(roster)
        assert mock.call_count == 1, "depth chart refetched on the second call"
        assert first == second
        assert second[dt._norm_name("Backup Guy")] == "Starter Guy"

    async def test_a_new_team_still_gets_fetched(self):
        with patch("nfl_mcp.nfl_tools.get_depth_chart", return_value=self.CHART) as mock:
            await dt._handcuff_index([{"name": "Starter Guy", "position": "RB", "team": "DAL"}])
            await dt._handcuff_index([{"name": "Starter Guy", "position": "RB", "team": "DAL"},
                                      {"name": "Starter Guy", "position": "RB", "team": "MIA"}])
        assert mock.call_count == 2  # DAL cached, MIA fresh

    async def test_expired_entry_is_refetched(self):
        roster = [{"name": "Starter Guy", "position": "RB", "team": "DAL"}]
        with patch("nfl_mcp.nfl_tools.get_depth_chart", return_value=self.CHART) as mock:
            await dt._handcuff_index(roster)
            # Age the entry past its TTL.
            ts, chart = dt._depth_chart_cache["DAL"]
            dt._depth_chart_cache["DAL"] = (ts - dt._DEPTH_CHART_TTL - 1, chart)
            await dt._handcuff_index(roster)
        assert mock.call_count == 2
