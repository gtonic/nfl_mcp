"""A questionable player's practice week moves the blended projection, not
just our quarter of it — and is not charged twice."""
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import opportunity_tools, projections
from nfl_mcp import sleeper_projections as sp
from nfl_mcp.projections import PRACTICE_BLEND_MULT, practice_blend_mult
from tests.test_sleeper_first_blend import PLAYERS, _engine, _logs, _payload


class TestPracticeBlendMult:
    @pytest.mark.parametrize("practice, pattern, expected", [
        ("DNP", "DNP", PRACTICE_BLEND_MULT["DNP_SINGLE"]),         # Wednesday only
        ("DNP", "DNP-DNP", PRACTICE_BLEND_MULT["DNP"]),
        ("DNP", "DNP-DNP-DNP", PRACTICE_BLEND_MULT["DNP"]),
        ("DNP", "LP-DNP", PRACTICE_BLEND_MULT["DNP"]),             # worsening to a DNP
        ("LP", "FP-LP", PRACTICE_BLEND_MULT["LP"]),
        ("LP", "DNP-LP", PRACTICE_BLEND_MULT["LP"]),               # a limited week
        ("LP", "LP", PRACTICE_BLEND_MULT["LP"]),
        ("FP", "DNP-LP-FP", 1.0),
        ("REST", "REST", 1.0),
        ("Did Not Participate In Practice", None, PRACTICE_BLEND_MULT["DNP_SINGLE"]),
        (None, None, 1.0),
    ])
    def test_questionable(self, practice, pattern, expected):
        assert practice_blend_mult("Questionable", practice, pattern) == expected

    @pytest.mark.parametrize("status", [None, "Active", "Out", "Doubtful", "IR"])
    def test_only_a_questionable_tag(self, status):
        # Healthy DNP is rest; Out is zero and doubtful capped in the blend.
        assert practice_blend_mult(status, "DNP", "DNP-DNP-DNP") == 1.0

    def test_the_pattern_alone_is_enough(self):
        assert practice_blend_mult("Q", None, "DNP-DNP") == PRACTICE_BLEND_MULT["DNP"]


class TestCalibratedValues:
    """The 2023-25 report backtest (`evals/backtest/practice_backtest.py`):
    each bucket's blend lands on the share of a healthy week it scored."""

    def test_sleeper_share_is_the_realised_share_over_sleepers_own_shading(self):
        for key in ("DNP_SINGLE", "DNP", "LP"):
            assert PRACTICE_BLEND_MULT[key] == pytest.approx(min(
                1.0, projections.QUESTIONABLE_REALISED[key]
                / projections.SLEEPER_QUESTIONABLE_PRICED), abs=0.005)

    def test_buckets_are_ordered(self):
        r = projections.QUESTIONABLE_REALISED
        assert r["DNP"] < r["LP"] < r["DNP_SINGLE"] < r["FP"] <= 1.0
        assert PRACTICE_BLEND_MULT["DNP"] < PRACTICE_BLEND_MULT["LP"] \
            < PRACTICE_BLEND_MULT["DNP_SINGLE"] < 1.0

    @pytest.mark.parametrize("practice, pattern, key", [
        ("LP", None, "LP"), ("FP", None, "FP"), ("DNP", "DNP-DNP", "DNP"),
        ("DNP", "LP-DNP", "DNP"), ("DNP", "DNP", "DNP_SINGLE"), ("DNP", None, "DNP_SINGLE"),
        (None, "DNP-DNP", "DNP"), ("limited", None, "LP"),
    ])
    def test_model_share(self, practice, pattern, key):
        assert projections.practice_adjusted_mult("Questionable", practice, pattern) \
            == projections.QUESTIONABLE_REALISED[key]

    def test_model_share_needs_a_questionable_tag(self):
        assert projections.practice_adjusted_mult(None, "DNP", "DNP-DNP") == 1.0
        assert projections.practice_adjusted_mult("Out", "LP") == 0.0
        assert projections.practice_adjusted_mult("Questionable", None) == 0.9


def _hurt(pattern):
    latest = pattern.split("-")[-1]
    return {**PLAYERS[0], "injury": {"status": "Questionable", "practice_status": latest,
                                     "practice_pattern": pattern}}


class TestOnTheBlend:
    @pytest.mark.asyncio
    async def test_a_dnp_week_discounts_sleepers_share(self, monkeypatch):
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_payload())))
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_logs()))
        out = await _engine().project_many([_hurt("DNP-DNP-DNP"), PLAYERS[0]], scoring="ppr",
                                           season=2026, week=3)
        hurt, healthy = out["projections"]
        bd = hurt["breakdown"]
        # Ours keeps its practice multiplier, once; Sleeper's share gets the week.
        assert bd["injury_mult"] == projections.QUESTIONABLE_BY_PRACTICE["DNP"]
        assert bd["practice_blend_mult"] == PRACTICE_BLEND_MULT["DNP"]
        assert hurt["projected_points"] == round(
            0.25 * hurt["model_projection"] + 0.75 * 11.0 * PRACTICE_BLEND_MULT["DNP"], 1)
        # The blend moves by far more than the old quarter-weighted 9%.
        assert hurt["projected_points"] < 0.8 * healthy["projected_points"]
        vol = projections._VOLATILITY["WR"]
        assert hurt["floor"] == round(hurt["projected_points"] * (1 - vol), 1)
        assert hurt["ceiling"] == round(hurt["projected_points"] * (1 + vol), 1)
        assert hurt["floor"] < hurt["projected_points"] < hurt["ceiling"]

    @pytest.mark.asyncio
    async def test_model_only_charges_the_practice_once(self, monkeypatch):
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_logs()))
        hurt, healthy = (await _engine().project_many(
            [_hurt("DNP-DNP"), PLAYERS[0]], scoring="ppr", season=2026, week=3))["projections"]
        assert hurt["projection_source"] == "model_only"
        assert hurt["projected_points"] == pytest.approx(
            healthy["projected_points"] * projections.QUESTIONABLE_BY_PRACTICE["DNP"], abs=0.11)

    @pytest.mark.asyncio
    async def test_a_full_practice_leaves_sleeper_alone(self, monkeypatch):
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_payload())))
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_logs()))
        p = (await _engine().project_many([_hurt("LP-FP")], scoring="ppr", season=2026,
                                          week=3))["projections"][0]
        assert p["breakdown"]["practice_blend_mult"] == 1.0
        assert p["projected_points"] == round(0.25 * p["model_projection"] + 0.75 * 11.0, 1)
