"""A backup inherits volume when the starter ahead of him is out.

The projection priced a backup off his market rank, so an unavailable starter
did not move him at all. But the fix has to be narrow: a starter who has been
out *all season* vacates nothing, because the backup's own trailing volume
already describes him as the starter. Boosting there would invent points out of
an absence — which is exactly the mistake a manual override made in live use.
"""
import types

import pytest

from nfl_mcp import opportunity_tools
from nfl_mcp.opportunity import project_opportunity
from nfl_mcp.opportunity_tools import (
    VACATED_VOLUME_SHARE,
    trailing_volume,
    vacated_volume,
)
from nfl_mcp.projections import ProjectionEngine, _depth_map, _starters_ahead


def _games(weeks, targets=0.0, carries=0.0, attempts=0.0):
    return [{"week": w, "targets": targets, "carries": carries, "attempts": attempts,
             "receptions": targets * 0.65, "receiving_yards": targets * 8.0,
             "receiving_tds": 0.05 * targets, "rushing_yards": carries * 4.2,
             "rushing_tds": 0.03 * carries, "passing_yards": attempts * 7.0,
             "passing_tds": 0.05 * attempts, "interceptions": 0.03 * attempts}
            for w in weeks]


def _index(entries):
    return opportunity_tools.build_name_index(
        {str(i): e for i, e in enumerate(entries)}
    )


class TestTrailingVolume:
    def test_none_without_prior_games(self):
        """A player who has been out all season has nothing to vacate."""
        index = _index([{"player_id": "1", "name": "Never Played", "position": "TE",
                         "team": "LV", "games": []}])
        assert trailing_volume(index, "Never Played", week=3) is None

    def test_none_for_an_unknown_player(self):
        assert trailing_volume({}, "Nobody", week=3) is None

    def test_only_counts_games_before_the_week(self):
        index = _index([{"player_id": "1", "name": "Starter", "position": "TE",
                         "team": "LV", "games": _games([1, 2, 3], targets=10)}])
        # Week 3 must not see week 3.
        assert trailing_volume(index, "Starter", week=3)["targets"] == pytest.approx(10.0)
        assert trailing_volume(index, "Starter", week=2)["targets"] == pytest.approx(10.0)
        assert trailing_volume(index, "Starter", week=1) is None


class TestVacatedVolume:
    def test_empty_when_the_out_player_never_played(self):
        """The Bowers case: out all season, so nothing is freed up."""
        index = _index([{"player_id": "1", "name": "Brock Bowers", "position": "TE",
                         "team": "LV", "games": []}])
        assert vacated_volume(index, ["Brock Bowers"], week=3) == {}

    def test_shares_a_productive_starters_volume(self):
        index = _index([{"player_id": "1", "name": "Big Starter", "position": "TE",
                         "team": "LV", "games": _games([1, 2], targets=10)}])
        got = vacated_volume(index, ["Big Starter"], week=3)
        assert got["targets"] == pytest.approx(10.0 * VACATED_VOLUME_SHARE)

    def test_the_share_is_conservative(self):
        assert 0 < VACATED_VOLUME_SHARE <= 0.6

    def test_multiple_out_players_add_up(self):
        index = _index([
            {"player_id": "1", "name": "A", "position": "RB", "team": "KC",
             "games": _games([1, 2], carries=10)},
            {"player_id": "2", "name": "B", "position": "RB", "team": "KC",
             "games": _games([1, 2], carries=6)},
        ])
        got = vacated_volume(index, ["A", "B"], week=3)
        assert got["carries"] == pytest.approx(16.0 * VACATED_VOLUME_SHARE)


class TestProjectionUsesIt:
    def test_extra_volume_raises_the_projection(self):
        games = _games([1, 2], targets=4)
        base = project_opportunity(games, "TE", ppr=0.5)
        boosted = project_opportunity(games, "TE", ppr=0.5,
                                      extra_volume={"targets": 5.0})
        assert boosted > base

    def test_it_is_converted_at_the_players_own_efficiency(self):
        """Inherited targets are worth what *he* does with a target."""
        efficient = _games([1, 2], targets=4)
        for g in efficient:
            g["receiving_yards"] = 80.0      # 20 yards a target
        inefficient = _games([1, 2], targets=4)
        for g in inefficient:
            g["receiving_yards"] = 16.0      # 4 yards a target

        extra = {"targets": 5.0}
        gain_efficient = (project_opportunity(efficient, "WR", ppr=0.5, extra_volume=extra)
                          - project_opportunity(efficient, "WR", ppr=0.5))
        gain_inefficient = (project_opportunity(inefficient, "WR", ppr=0.5, extra_volume=extra)
                            - project_opportunity(inefficient, "WR", ppr=0.5))
        assert gain_efficient > gain_inefficient

    def test_no_extra_volume_changes_nothing(self):
        games = _games([1, 2], targets=4)
        assert project_opportunity(games, "TE", ppr=0.5, extra_volume={}) == \
            project_opportunity(games, "TE", ppr=0.5)


class TestDepthDetection:
    VALUES = {"list": [
        {"name": "Star TE", "position": "TE", "team": "LV", "position_rank": 2},
        {"name": "Backup TE", "position": "TE", "team": "LV", "position_rank": 20},
        {"name": "Other Team TE", "position": "TE", "team": "KC", "position_rank": 5},
    ]}

    def test_depth_map_groups_and_orders(self):
        depth = _depth_map(self.VALUES)
        assert [e["name"] for e in depth[("LV", "TE")]] == ["Star TE", "Backup TE"]

    def test_finds_an_out_starter_ahead(self):
        depth = _depth_map(self.VALUES)
        status = lambda n, t: "Out" if n == "Star TE" else None  # noqa: E731
        assert _starters_ahead(depth, "LV", "TE", 20, status) == ["Star TE"]

    def test_ignores_a_healthy_starter(self):
        depth = _depth_map(self.VALUES)
        status = lambda n, t: None  # noqa: E731
        assert _starters_ahead(depth, "LV", "TE", 20, status) == []

    def test_ignores_players_behind(self):
        """The star must not inherit from his own backup."""
        depth = _depth_map(self.VALUES)
        status = lambda n, t: "Out" if n == "Backup TE" else None  # noqa: E731
        assert _starters_ahead(depth, "LV", "TE", 2, status) == []

    def test_ignores_another_team(self):
        depth = _depth_map(self.VALUES)
        status = lambda n, t: "Out"  # noqa: E731
        assert _starters_ahead(depth, "KC", "TE", 20, status) == ["Other Team TE"]
        assert "Star TE" not in _starters_ahead(depth, "KC", "TE", 20, status)

    def test_no_rank_means_no_claim(self):
        depth = _depth_map(self.VALUES)
        status = lambda n, t: "Out"  # noqa: E731
        assert _starters_ahead(depth, "LV", "TE", None, status) == []


class TestEngineIntegration:
    def _engine(self, out_name, starter_games):
        engine = ProjectionEngine.__new__(ProjectionEngine)
        engine.db = None
        engine.values = types.SimpleNamespace(
            lookup=lambda *a, **k: {"position_rank": 20})
        engine.defense = types.SimpleNamespace(
            get_matchup_difficulty=lambda *a, **k: {"matchup_tier": "neutral"})
        engine.vegas = types.SimpleNamespace(
            get_game_lines=lambda *a, **k: {"is_fallback": True})
        values = {"list": [
            {"name": out_name, "position": "TE", "team": "LV", "position_rank": 2},
            {"name": "Backup TE", "position": "TE", "team": "LV", "position_rank": 20},
        ]}
        index = _index([
            {"player_id": "1", "name": out_name, "position": "TE", "team": "LV",
             "games": starter_games},
            {"player_id": "2", "name": "Backup TE", "position": "TE", "team": "LV",
             "games": _games([1, 2], targets=3)},
        ])
        return engine, values, index

    def _project(self, starter_games):
        engine, values, index = self._engine("Star TE", starter_games)
        player = {"name": "Backup TE", "position": "TE", "team": "LV", "opponent": "NO"}
        status = lambda n, t: "Out" if n == "Star TE" else None  # noqa: E731
        return engine._project_one(player, values, {}, {}, index, 3, 0.5,
                                   _depth_map(values), status)

    def test_a_productive_out_starter_lifts_the_backup(self):
        with_volume = self._project(_games([1, 2], targets=9))
        assert with_volume["breakdown"]["starters_out_ahead"] == ["Star TE"]
        assert with_volume["breakdown"]["vacated_volume"]["targets"] > 0

        without = self._project([])
        assert without["breakdown"]["vacated_volume"] == {}
        assert with_volume["projected_points"] > without["projected_points"]

    def test_a_starter_out_all_season_changes_nothing(self):
        """Detected and reported, but no boost — there is nothing to inherit."""
        got = self._project([])
        assert got["breakdown"]["starters_out_ahead"] == ["Star TE"]
        assert got["breakdown"]["vacated_volume"] == {}
