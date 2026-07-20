"""Tests for the weekly projection engine (offline, no network)."""

import pytest

from nfl_mcp import projections as pj


class FakeValues:
    def __init__(self, by_id=None, by_name=None):
        self._by_id = by_id or {}
        self._by_name = by_name or {}

    async def get_values(self, *a, **k):
        return {"source": "fantasycalc", "list": list(self._by_id.values())}

    def lookup(self, idx, player_id=None, name=None, position=None):
        return self._by_id.get(str(player_id)) or self._by_name.get(name)


class FakeDefense:
    def __init__(self, tier="neutral"):
        self.tier = tier

    async def fetch_defense_rankings(self):
        return {}

    def get_matchup_difficulty(self, position, opponent, rankings):
        return {"matchup_tier": self.tier, "rank": 16}


class FakeVegas:
    def __init__(self, lines=None):
        self._lines = lines or {}

    async def fetch_current_lines(self):
        return self._lines

    def get_game_lines(self, team, lines):
        return (lines or self._lines).get(team, {"home_team": team, "is_fallback": True})


def _engine(values=None, defense=None, vegas=None):
    e = pj.ProjectionEngine.__new__(pj.ProjectionEngine)  # skip __init__ (no singletons)
    e.values = values or FakeValues()
    e.defense = defense or FakeDefense()
    e.vegas = vegas or FakeVegas()
    return e


class TestPureHelpers:
    def test_base_ppg_tiers(self):
        assert pj.base_ppg("RB", 1) > pj.base_ppg("RB", 20) > pj.base_ppg("RB", 40)
        assert pj.base_ppg("WR", 1) == 17.0
        assert pj.base_ppg("QB", 2) == 22.0
        # unknown rank -> lowest tier
        assert pj.base_ppg("TE", None) == pj.base_ppg("TE", 999)

    def test_environment_mult(self):
        assert pj._environment_mult(29, False) > 1.0
        assert pj._environment_mult(17, False) < 1.0
        assert pj._environment_mult(29, True) == 1.0   # fallback -> neutral
        assert pj._environment_mult(None, False) == 1.0

    def test_usage_and_injury_mult(self):
        assert pj._usage_mult(90, "up") > pj._usage_mult(30, "down")
        assert pj._injury_mult("out") == 0.0
        assert pj._injury_mult("questionable") == 0.9
        assert pj._injury_mult(None) == 1.0

    def test_matchup_multiplier_is_position_specific(self):
        # Data-driven (evals/backtest): RB matchup matters most, WR essentially not.
        assert pj.matchup_multiplier("RB", "smash") > pj.matchup_multiplier("WR", "smash")
        assert pj.matchup_multiplier("WR", "smash") == 1.0   # WR strength 0 -> no swing
        assert pj.matchup_multiplier("WR", "elite") == 1.0
        # RB swings both ways around 1.0
        assert pj.matchup_multiplier("RB", "smash") > 1.0 > pj.matchup_multiplier("RB", "elite")
        # unknown / neutral tier never moves anything
        assert pj.matchup_multiplier("RB", "unknown") == 1.0
        assert pj.matchup_multiplier("TE", "neutral") == 1.0


class TestProjectMany:
    async def test_smash_and_environment_boost(self):
        vals = FakeValues(by_name={"WR One": {"value": 9000, "position_rank": 3, "position": "WR"}})
        vegas = FakeVegas({"CIN": {"home_team": "CIN", "home_implied_total": 29, "is_fallback": False}})
        eng = _engine(vals, FakeDefense("smash"), vegas)
        res = await eng.project_many([{"name": "WR One", "position": "WR", "team": "CIN", "opponent": "CLE",
                                       "usage": {"snap_percentage": 90, "usage_trend": "up"}}])
        p = res["projections"][0]
        # base 17 (rank<=5) * smash 1.10 * env 1.08 * usage(1.05*1.05) > base
        assert p["projected_points"] > 17.0
        assert p["floor"] < p["projected_points"] < p["ceiling"]
        assert p["confidence"] >= 80  # value + real vegas + usage
        assert p["matchup_tier"] == "smash"

    async def test_injury_out_zeroes_projection(self):
        vals = FakeValues(by_name={"Hurt Guy": {"value": 5000, "position_rank": 10, "position": "RB"}})
        eng = _engine(vals)
        res = await eng.project_many([{"name": "Hurt Guy", "position": "RB", "team": "KC", "opponent": "LV",
                                       "injury": {"status": "out"}}])
        p = res["projections"][0]
        assert p["projected_points"] == 0.0
        assert p["floor"] == 0.0 and p["ceiling"] == 0.0

    async def test_unknown_player_uses_baseline(self):
        eng = _engine()  # empty values -> lookup misses
        res = await eng.project_many([{"name": "Nobody", "position": "WR", "team": "NYJ", "opponent": "BUF"}])
        p = res["projections"][0]
        assert p["value_source"] == "baseline"
        assert p["projected_points"] > 0
        assert p["confidence"] == 50  # no real signals

    async def test_tool_project_players_empty(self):
        res = await pj.project_players([], db=None)
        assert res["success"] is False
