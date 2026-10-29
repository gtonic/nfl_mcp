"""The weekly blend's position x tier calibration (`nfl_mcp.calibration`) and
its fit (`evals.backtest.weekly_calibration`). Network is never touched."""
import pytest

from evals.backtest import weekly_calibration as wc
from nfl_mcp import calibration as cal
from nfl_mcp import projection_store, projections, ros
from nfl_mcp import sleeper_projections as sp
from nfl_mcp.scoring import ScoringModel
from tests.test_sleeper_first_blend import PLAYERS, _engine, _payload, sleeper_week  # noqa: F401

PPR = ScoringModel.preset(1.0)
TABLE = {"cells": {"WR": {"T1": {"mult": 0.9, "raw": 0.8, "n": 300}},
                   "DEF": {"T1": {"mult": 0.94, "raw": 0.87, "n": 168}},
                   "TE": {"T2": {"mult": 2.0, "raw": 2.0, "n": 999}}}}


class TestTiers:
    @pytest.mark.parametrize("pos, rank, tier", [
        ("QB", 12, "T1"), ("QB", 13, "T2"), ("QB", 24, "T2"), ("QB", 25, "T3"),
        ("RB", 24, "T1"), ("WR", 48, "T2"), ("WR", 49, "T3"), ("DST", 3, "T1"),
        ("QB", None, None), ("QB", 0, None), ("LB", 1, None),
    ])
    def test_tier_for(self, pos, rank, tier):
        assert cal.tier_for(pos, rank) == tier

    def test_multiplier_reads_the_cell_and_caps(self):
        assert cal.multiplier("WR", 3, TABLE) == (0.9, "T1")
        assert cal.multiplier("WR", 30, TABLE) == (1.0, "T2")      # no cell: neutral
        assert cal.multiplier("TE", 15, TABLE) == (cal.CAP_HIGH, "T2")
        assert cal.multiplier("WR", None, TABLE) == (1.0, None)

    def test_calibrate_leaves_neutral_cells_untouched(self):
        assert cal.calibrate(10.04, "WR", 30, TABLE) == (10.04, 1.0, "T2")
        assert cal.calibrate(10.0, "WR", 1, TABLE) == (9.0, 0.9, "T1")

    def test_shipped_table_is_within_the_caps(self):
        for tiers in (cal.CALIBRATION.get("cells") or {}).values():
            for cell in tiers.values():
                assert cal.CAP_LOW <= cell["mult"] <= cal.CAP_HIGH
                assert cell["n"] >= cal.MIN_N


class TestRanks:
    def test_ranked_by_points_in_the_league_scoring(self):
        payload = [
            *_payload(),
            {"player_id": "9", "team": "MIA", "opponent": "BUF",
             "player": {"first_name": "Big", "last_name": "Target", "position": "WR", "team": "MIA"},
             "stats": {"rec": 9.0, "rec_yd": 50.0, "pts_ppr": 14.0}},
        ]
        index = sp._index(payload)
        ranks = cal.position_ranks(index, PPR)
        assert ranks[("WR", "9")] == 1 and ranks[("WR", "1")] == 2
        assert ranks[("DEF", "BUF")] == 1
        # Zero-point lines are unranked: no tier, no correction.
        assert ("QB", "2") not in ranks
        assert cal.rank_of(index, PPR, name="Wide Receiver", team="BUF", position="WR") == 2
        assert cal.rank_of(index, PPR, team="BUF", position="DEF") == 1
        assert cal.rank_of({}, PPR, name="Wide Receiver", team="BUF") is None


class TestApplied:
    @pytest.mark.asyncio
    async def test_weekly_blend_is_calibrated_and_says_so(self, sleeper_week, monkeypatch):  # noqa: F811
        monkeypatch.setattr(cal, "CALIBRATION", TABLE)
        out = await _engine().project_many(PLAYERS, scoring="ppr", season=2026, week=3)
        wr = out["projections"][0]
        raw = sp.blend(wr["model_projection"], 11.0)
        assert wr["projected_points"] == round(raw * 0.9, 1)
        bd = wr["breakdown"]
        assert bd["calibration_mult"] == 0.9 and bd["calibration_tier"] == "T1"
        assert bd["sleeper_rank"] == 1
        vol = projections._VOLATILITY["WR"]
        assert wr["floor"] == round(wr["projected_points"] * (1 - vol), 1)
        signals = projection_store.signals_of(wr)
        assert signals["calibration_mult"] == 0.9 and signals["calibration_tier"] == "T1"

    @pytest.mark.asyncio
    async def test_neutral_table_changes_nothing(self, sleeper_week):  # noqa: F811
        out = await _engine().project_many(PLAYERS, scoring="ppr", season=2026, week=3)
        wr = out["projections"][0]
        assert wr["projected_points"] == sp.blend(wr["model_projection"], 11.0)
        assert "calibration_mult" not in projection_store.signals_of(wr)

    def test_ros_later_week_takes_the_multiplier(self):
        pts, source, _, theirs = ros._later_week(8.0, (12.0, "projected"), sleeper_mult=1.0,
                                                 injured=False, sleeper_return=None,
                                                 calibration_mult=0.9)
        assert source == "sleeper_blend" and theirs == 12.0
        assert pts == round((0.25 * 8.0 + 0.75 * 12.0) * 0.9, 2)

    def test_ros_reads_each_weeks_rank(self, monkeypatch):
        monkeypatch.setattr(cal, "CALIBRATION", TABLE)
        index = sp._index(_payload())
        p = {"player_id": "1", "name": "Wide Receiver", "team": "BUF", "position": "WR"}
        assert ros._calibration_mults({5: index, 6: {}}, PPR, p) == {5: 0.9}

    def test_streaming_defense_week(self, monkeypatch):
        from nfl_mcp.streaming_tools import _blend_unit_week
        monkeypatch.setattr(cal, "CALIBRATION", TABLE)
        week = {"week": 3, "projected_points": 8.0}
        _blend_unit_week("DEF", "BUF", week, {3: sp._index(_payload())}, PPR)
        assert week["projected_points"] == round(sp.blend(8.0, week["sleeper_projection"]) * 0.94, 1)
        assert week["calibration_mult"] == 0.94


def _rows(pos, tier, rank, pairs, week=1):
    return [{"week": week, "pos": pos, "tier": tier, "rank": rank, "projected": p, "actual": a}
            for p, a in pairs]


class TestFit:
    def test_shrunk_toward_one_and_capped(self):
        rows = _rows("DEF", "T1", 1, [(10.0, 8.0)] * 100)
        cell = wc.fit(rows, k=100)["DEF"]["T1"]
        assert cell["raw"] == 0.8 and cell["n"] == 100
        assert cell["mult"] == 0.9        # 1 + (0.8 - 1) * 100 / 200
        capped = wc.fit(_rows("TE", "T2", 15, [(5.0, 10.0)] * 1000), k=10)["TE"]["T2"]
        assert capped["mult"] == cal.CAP_HIGH

    def test_cells_need_rows_and_a_held_out_gain(self):
        fitted = {"DEF": {"T1": {"mult": 0.9, "raw": 0.8, "n": 100}},
                  "WR": {"T1": {"mult": 0.9, "raw": 0.8, "n": 100},
                         "T2": {"mult": 0.9, "raw": 0.8, "n": 10}}}
        holdout = (_rows("DEF", "T1", 1, [(10.0, 8.0)] * 5, week=7)      # lower: helps
                   + _rows("WR", "T1", 1, [(10.0, 12.0)] * 5, week=7)    # higher: hurts
                   + _rows("WR", "T2", 30, [(10.0, 8.0)] * 5, week=7))   # too few fit rows
        assert wc.keep_cells(fitted, holdout, min_n=60) == {"DEF": fitted["DEF"]}

    def test_write_table_round_trips(self, tmp_path):
        target = tmp_path / "calibration.py"
        target.write_text(open(cal.__file__, encoding="utf-8").read(), encoding="utf-8")
        cells = {"DEF": {"T1": {"mult": 0.938, "raw": 0.865, "n": 168}}}
        comps = {"def_td": {"mult": 0.724, "raw": 0.467, "n": 214}}
        wc.write_table(cells, 2026, [1, 2, 3], 4107, path=str(target), comps=comps)
        ns: dict = {}
        exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), ns)
        assert ns["CALIBRATION"]["cells"] == cells
        assert ns["CALIBRATION"]["def_components"] == comps
        assert ns["CALIBRATION"]["weeks"] == [1, 2, 3] and ns["CALIBRATION"]["season"] == 2026


DEF_LINE = {"sack": 3.0, "int": 1.0, "def_td": 0.2, "pts_allow": 17.0, "pts_ppr": 9.0}
COMPS = {"def_components": {"def_td": {"mult": 0.5}, "sack": {"mult": 9.0}}}


def _def_row(week, stats, actual_stats, projected, actual):
    return {"week": week, "pos": "DEF", "name": "BUF", "tier": "T1", "rank": 1,
            "model": PPR, "stats": stats, "actual_stats": actual_stats,
            "raw_projected": projected, "projected": projected, "actual": actual}


class TestDefenseEvents:
    def test_only_a_defense_line_is_scaled_and_capped(self):
        line = cal.defense_line(DEF_LINE, COMPS)
        assert line["def_td"] == 0.1 and line["sack"] == 3.0 * cal.DEF_CAP_HIGH
        assert line["int"] == 1.0 and line["pts_allow"] == 17.0
        wr = {"rec": 5.0, "fum_rec": 1.0}
        assert cal.defense_line(wr, {"def_components": {"fum_rec": {"mult": 0.5}}}) is wr

    def test_price_stats_prices_the_calibrated_line(self, monkeypatch):
        raw = sp.price_stats(DEF_LINE, PPR, calibrated=False)
        monkeypatch.setattr(cal, "CALIBRATION", {"cells": {}, **COMPS})
        # def_td is worth 6 in Sleeper's defaults: 0.1 fewer, 0.6 points;
        # sacks 1.5x, +1.5.
        assert sp.price_stats(DEF_LINE, PPR) == pytest.approx(raw - 0.6 + 1.5, abs=0.11)
        assert sp.price_stats(DEF_LINE, PPR, calibrated=False) == raw

    def test_fit_counts_each_team_week_once(self):
        rows = [_def_row(1, {"def_td": 0.2}, {"def_td": 0.0}, 8.0, 5.0),
                {**_def_row(1, {"def_td": 0.2}, {"def_td": 0.0}, 8.0, 5.0), "league": "other"},
                _def_row(2, {"def_td": 0.2}, {"def_td": 0.2}, 8.0, 5.0)]
        cell = wc.fit_components(rows, k=0)["def_td"]
        assert cell == {"mult": 0.5, "raw": 0.5, "n": 2}

    def test_components_need_a_held_out_gain(self):
        line = {"def_td": 0.5, "pts_allow": 20.0}
        lower = {"def_td": {"mult": 0.5, "raw": 0.5, "n": 100}}
        projected = sp.price_stats(line, PPR, calibrated=False)
        helps = [_def_row(7, line, {}, projected, projected - 1.5)]
        hurts = [_def_row(7, line, {}, projected, projected + 1.5)]
        assert wc.keep_components(lower, helps) == lower
        assert wc.keep_components(lower, hurts) == {}
