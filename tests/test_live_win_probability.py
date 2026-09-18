"""Mid-week, part of the matchup is settled and must stop being a guess."""
from datetime import UTC, datetime, timedelta

import pytest

from nfl_mcp.game_clock import GAME_LENGTH, game_progress, parse_kickoff, settle
from nfl_mcp.win_probability import optimize_win_probability

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


class TestGameProgress:
    def test_upcoming_game_is_zero(self):
        assert game_progress((NOW + timedelta(hours=5)).isoformat(), NOW) == 0.0

    def test_finished_game_is_one(self):
        assert game_progress((NOW - GAME_LENGTH - timedelta(minutes=1)).isoformat(), NOW) == 1.0

    def test_in_progress_is_between(self):
        p = game_progress((NOW - timedelta(hours=1)).isoformat(), NOW)
        assert 0.0 < p < 1.0

    def test_unknown_kickoff_counts_as_upcoming(self):
        # Treating a player as unplayed keeps him in the optimizer, which is
        # the recoverable error. Assuming he is done would freeze a lineup the
        # manager can still change.
        for value in (None, "", "not-a-date"):
            assert game_progress(value, NOW) == 0.0

    @pytest.mark.parametrize("raw", ["2026-09-18T00:15Z", "2026-09-18T00:15+00:00"])
    def test_kickoff_formats_from_the_schedule_cache(self, raw):
        assert parse_kickoff(raw) == datetime(2026, 9, 18, 0, 15, tzinfo=UTC)


class TestSettle:
    def test_not_started_keeps_the_projection_and_all_uncertainty(self):
        assert settle(14.0, None, 0.0) == (14.0, 1.0)

    def test_final_uses_the_actual_score_with_no_uncertainty(self):
        # 20.3 banked beats any projection: it already happened.
        assert settle(14.0, 20.3, 1.0) == (20.3, 0.0)

    def test_final_with_a_zero_score_is_still_settled(self):
        assert settle(14.0, 0.0, 1.0) == (0.0, 0.0)

    def test_in_progress_blends_banked_and_remaining(self):
        mean, share = settle(10.0, 4.0, 0.5)
        assert mean == 9.0  # 4 banked + half of a 10-point projection
        assert share == 0.5


class TestLockedPlayersInTheOptimizer:
    def _opp(self, mean):
        return [{"name": "Opp", "position": "QB", "projected_points": mean, "sd": 5.0}]

    def test_locked_points_count_toward_the_total(self):
        locked = [{"name": "Played", "position": "RB", "slot": "RB",
                   "projected_points": 20.0, "sd": 0.0}]
        cands = [{"name": "Open", "position": "RB", "projected_points": 10.0, "sd": 4.0}]
        res = optimize_win_probability(
            cands, self._opp(15.0), slots={"RB": 2}, locked_players=locked
        )
        assert res["projected_points"] == 30.0
        assert [p["player"] for p in res["recommended_lineup"]] == ["Played", "Open"]

    def test_a_locked_slot_is_not_offered_to_anyone_else(self):
        locked = [{"name": "Played", "position": "RB", "slot": "RB",
                   "projected_points": 20.0, "sd": 0.0}]
        cands = [
            {"name": "A", "position": "RB", "projected_points": 12.0, "sd": 4.0},
            {"name": "B", "position": "RB", "projected_points": 11.0, "sd": 4.0},
        ]
        res = optimize_win_probability(
            cands, self._opp(15.0), slots={"RB": 2}, locked_players=locked
        )
        # Two RB slots, one already filled -> exactly one more may be added.
        assert len(res["recommended_lineup"]) == 2

    def test_a_deficit_lowers_the_probability(self):
        # The case this exists for: the opponent banked a huge Thursday night.
        cands = [{"name": "Open", "position": "RB", "projected_points": 20.0, "sd": 6.0}]
        even = optimize_win_probability(
            cands, self._opp(20.0), slots={"RB": 1},
            locked_players=[{"name": "P", "position": "RB", "slot": "RB",
                             "projected_points": 20.0, "sd": 0.0}],
        )
        behind = optimize_win_probability(
            cands,
            [*self._opp(20.0), {"name": "Banked", "position": "QB",
                                "projected_points": 38.0, "sd": 0.0}],
            slots={"RB": 1},
            locked_players=[{"name": "P", "position": "RB", "slot": "RB",
                             "projected_points": 20.0, "sd": 0.0}],
        )
        assert behind["win_probability"] < even["win_probability"]

    def test_settled_players_remove_variance_rather_than_shifting_the_mean(self):
        uncertain = optimize_win_probability(
            [{"name": "X", "position": "RB", "projected_points": 20.0, "sd": 8.0}],
            self._opp(15.0), slots={"RB": 1},
        )
        settled = optimize_win_probability(
            [], self._opp(15.0), slots={"RB": 1},
            locked_players=[{"name": "X", "position": "RB", "slot": "RB",
                             "projected_points": 20.0, "sd": 0.0}],
        )
        assert settled["projected_points"] == uncertain["projected_points"]
        # Same lead, but no way left to lose it by variance.
        assert settled["win_probability"] > uncertain["win_probability"]

    def test_without_locked_players_nothing_changes(self):
        cands = [{"name": "A", "position": "RB", "projected_points": 12.0, "sd": 4.0}]
        a = optimize_win_probability(cands, self._opp(10.0), slots={"RB": 1})
        b = optimize_win_probability(cands, self._opp(10.0), slots={"RB": 1}, locked_players=[])
        assert a["win_probability"] == b["win_probability"]
