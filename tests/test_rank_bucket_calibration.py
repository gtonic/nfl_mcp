"""Rank buckets and receiver priors, recalibrated on 2023-24 (see evals/backtest)."""
import pytest

from evals.backtest import bucket_calibration as bc
from nfl_mcp import opportunity, projections
from nfl_mcp.projections import _RANK_BUCKETS, base_ppg, rank_bucket


class TestRankBuckets:
    def test_tiers_are_monotone_and_end_open(self):
        for pos, tiers in _RANK_BUCKETS.items():
            values = [v for _, v in tiers]
            assert values == sorted(values, reverse=True), pos
            assert tiers[-1][0] is None
            lasts = [last for last, _ in tiers[:-1]]
            assert lasts == sorted(lasts)

    def test_receiver_tiers_are_per_game_played(self):
        # The levels PR #230 measured (and scaled ROS by): WR37-48 ~10.0,
        # WR25-36 ~12, WR6-12 ~17.1, TE13-20 ~8.8, TE7-12 ~10.1.
        assert rank_bucket("WR", 40) == pytest.approx(10.0)
        assert rank_bucket("WR", 30) == pytest.approx(12.0)
        assert rank_bucket("WR", 10) == pytest.approx(17.0)
        assert rank_bucket("TE", 15) == pytest.approx(8.5)
        assert rank_bucket("TE", 10) == pytest.approx(10.5)

    def test_qb_unchanged(self):
        assert [rank_bucket("QB", r) for r in (1, 5, 10, 15, 25)] == [22, 20, 18, 16, 14]

    def test_unranked_and_invalid_ranks_are_the_last_tier(self):
        for pos, tiers in _RANK_BUCKETS.items():
            assert rank_bucket(pos, None) == rank_bucket(pos, 0) == rank_bucket(pos, 999) \
                == tiers[-1][1]
        assert rank_bucket("K", 1) == 8.0 and rank_bucket("DEF", 1) == 7.0

    def test_half_ppr_rebase_uses_the_measured_reception_share(self):
        # A WR's full-PPR points are ~35% receptions, a TE's ~41%.
        assert base_ppg("WR", 40, 0.5) == pytest.approx(10.0 * (1 - 0.5 * 0.35), abs=0.01)
        assert base_ppg("TE", 15, 0.5) == pytest.approx(8.5 * (1 - 0.5 * 0.41), abs=0.01)
        assert projections._RECEPTION_SHARE["RB"] < projections._RECEPTION_SHARE["WR"] \
            < projections._RECEPTION_SHARE["TE"]


class TestReceiverPriors:
    def test_te_target_is_not_priced_below_a_wr_target(self):
        # A TE's higher catch rate makes up for the shorter yards per catch.
        assert opportunity._PRIORS["TE"]["ppt"] >= opportunity._PRIORS["WR"]["ppt"]
        assert opportunity._PRIORS["TE"]["ppt"] == pytest.approx(1.72)

    def test_catch_rates(self):
        cr = opportunity._CATCH_RATE
        assert cr["WR"] < cr["TE"] < cr["RB"]

    def test_low_sample_te_projects_near_league_rate(self):
        # Two average TE games (5 targets, 3.6 catches, 36 yards, a TD every
        # ~4 games): the projection sits near 5 × 1.72, not 5 × 1.35.
        games = [{"week": w, "targets": 5, "receptions": 3.6, "receiving_yards": 36,
                  "receiving_tds": 0.25, "carries": 0} for w in (1, 2)]
        proj = opportunity.project_opportunity(games, "TE")
        assert proj == pytest.approx(5 * 1.72, abs=0.3)


class TestCalibrationHarness:
    def _recs(self):
        out = []
        for w in range(1, 9):
            out.append({"player_id": "a", "position": "WR", "week": w, "ppr": 12.0,
                        "receptions": 4.0})
            out.append({"player_id": "b", "position": "WR", "week": w, "ppr": 6.0,
                        "receptions": 2.0})
        return out

    def test_tier_index(self):
        assert bc.tier_index("WR", 1) == 0
        assert bc.tier_index("WR", 40) == 4
        assert bc.tier_index("WR", 500) == len(_RANK_BUCKETS["WR"]) - 1
        assert bc.tier_index("WR", None) is None
        assert bc.tier_label("WR", 0) == "1-5"

    def test_season_and_ros_tier_means(self):
        ranks = {"a": 3}
        season = bc.tier_means(bc.season_rows(self._recs(), ranks), "WR")
        assert season[0] == {"n": 1, "full": 12.0, "half": 10.0}
        assert season["unranked"]["full"] == 6.0
        ros = bc.tier_means(bc.ros_rows(self._recs(), ranks, as_of=3), "WR")
        assert ros[0]["n"] == 1 and ros[0]["full"] == 12.0
