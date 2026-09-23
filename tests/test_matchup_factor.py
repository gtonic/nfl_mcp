"""The continuous matchup factor: counts from week 2, priced in league scoring."""
import pytest

from nfl_mcp.matchup_tools import (
    MATCHUP_PRIOR_GAMES,
    PRIOR_SEASON_WEIGHT,
    attach_prior_season,
    compute_defense_rankings,
    matchup_ratio,
)
from nfl_mcp.projections import matchup_factor


def _weekly(allowed: dict[str, list[tuple[float, float]]], pos: str = "WR") -> dict:
    """{team: [(ppr, rec) per week]} -> compute_defense_rankings input."""
    out = {}
    for team, weeks in allowed.items():
        for wk, (pts, rec) in enumerate(weeks, 1):
            out[(team, pos, str(wk))] = [pts, rec]
    return out


def _entry(rankings: dict, team: str, pos: str = "WR") -> dict:
    return next(r for r in rankings[pos] if r["team"] == team)


class TestComputeDefenseRankings:
    def test_entries_carry_the_raw_averages(self):
        r = compute_defense_rankings(_weekly({"AAA": [(40, 10), (44, 12)],
                                              "BBB": [(20, 6), (24, 8)]}), 2026)
        a = _entry(r, "AAA")
        assert a["ppr_allowed_avg"] == 42 and a["rec_allowed_avg"] == 11
        assert a["league_ppr_avg"] == 32 and a["league_rec_avg"] == 9
        assert a["games_sampled"] == 2

    def test_two_games_still_withhold_the_tier(self):
        # The discrete tier stays neutral early; the ratio is what moves.
        r = compute_defense_rankings(_weekly({"AAA": [(40, 10), (44, 12)],
                                              "BBB": [(20, 6), (24, 8)]}), 2026)
        assert _entry(r, "AAA")["matchup_tier"] == "neutral"
        assert matchup_ratio(_entry(r, "AAA")) > 1.0 > matchup_ratio(_entry(r, "BBB"))

    def test_empty_input_is_none(self):
        assert compute_defense_rankings({}, 2026) is None


class TestMatchupRatio:
    def test_shrinks_a_two_game_sample_hard(self):
        r = compute_defense_rankings(_weekly({"AAA": [(48, 12), (48, 12)],
                                              "BBB": [(16, 4), (16, 4)]}), 2026)
        # AAA allows 1.5x the league average over two games; with no prior
        # season, the ratio is pulled toward 1.0 by MATCHUP_PRIOR_GAMES.
        expected = (2 * 1.5 + MATCHUP_PRIOR_GAMES * 1.0) / (2 + MATCHUP_PRIOR_GAMES)
        assert matchup_ratio(_entry(r, "AAA")) == pytest.approx(expected)

    def test_prior_season_sets_where_it_shrinks_to(self):
        now = compute_defense_rankings(_weekly({"AAA": [(32, 8)], "BBB": [(32, 8)]}), 2026)
        last = compute_defense_rankings(_weekly({"AAA": [(48, 12)], "BBB": [(16, 4)]}), 2025)
        attach_prior_season(now, last)
        # Average so far, but last season AAA gave up 1.5x: part of it carries.
        prior = 1.0 + PRIOR_SEASON_WEIGHT * 0.5
        expected = (1 * 1.0 + MATCHUP_PRIOR_GAMES * prior) / (1 + MATCHUP_PRIOR_GAMES)
        assert matchup_ratio(_entry(now, "AAA")) == pytest.approx(expected)
        assert matchup_ratio(_entry(now, "BBB")) < 1.0

    def test_reception_value_changes_the_ratio(self):
        # AAA allows the same PPR points as BBB but through more catches, so
        # in half PPR it gives up fewer points than BBB.
        r = compute_defense_rankings(_weekly({"AAA": [(40, 16)] * 4,
                                              "BBB": [(40, 8)] * 4}), 2026)
        assert matchup_ratio(_entry(r, "AAA"), ppr=1.0) == pytest.approx(1.0)
        assert matchup_ratio(_entry(r, "AAA"), ppr=0.5) < 1.0 < matchup_ratio(_entry(r, "BBB"), ppr=0.5)

    @pytest.mark.parametrize("entry", [
        None,
        {"team": "AAA", "rank": 16, "is_fallback": True, "ppr_allowed_avg": 1.0},
        # A legacy row read back from the database has no raw averages.
        {"team": "AAA", "rank": 3, "points_allowed_avg": 30.0, "games_sampled": 5},
    ])
    def test_unusable_entries_return_none(self, entry):
        assert matchup_ratio(entry) is None


class TestMatchupFactor:
    def test_position_strength(self):
        assert matchup_factor("RB", 1.2) == pytest.approx(1.15)
        assert matchup_factor("WR", 1.2) == pytest.approx(1.05)
        assert matchup_factor("QB", 0.8) == pytest.approx(0.9)

    def test_deviation_is_capped(self):
        assert matchup_factor("RB", 3.0) == matchup_factor("RB", 1.3)

    def test_unknown_position_is_neutral(self):
        assert matchup_factor("K", 1.3) == 1.0


class TestEngineWiring:
    def test_projection_applies_the_ratio_and_reports_it(self):
        from nfl_mcp import projections

        engine = projections.get_projection_engine()
        rankings = compute_defense_rankings(
            _weekly({"MIA": [(30, 3), (30, 3)], "NE": [(10, 1), (10, 1)]}, pos="RB"), 2026)
        player = {"name": "Some RB", "position": "RB", "team": "BUF", "opponent": "MIA"}

        out = engine._project_one(player, values_index={}, rankings=rankings, lines={})

        bd = out["breakdown"]
        ratio = matchup_ratio(_entry(rankings, "MIA", "RB"))
        assert bd["matchup_ratio"] == pytest.approx(ratio, abs=1e-3)
        assert bd["matchup_mult"] == pytest.approx(matchup_factor("RB", ratio), abs=1e-3)
        assert bd["matchup_mult"] > 1.0
        # The tier is still withheld on two games; the factor is not.
        assert out["matchup_tier"] == "neutral"

    def test_legacy_rankings_fall_back_to_the_tier(self):
        from nfl_mcp import projections

        engine = projections.get_projection_engine()
        rankings = {"RB": [{"team": "MIA", "rank": 30, "matchup_tier": "smash",
                            "points_allowed_avg": 30.0}]}
        player = {"name": "Some RB", "position": "RB", "team": "BUF", "opponent": "MIA"}

        out = engine._project_one(player, values_index={}, rankings=rankings, lines={})

        assert out["breakdown"]["matchup_ratio"] is None
        assert out["breakdown"]["matchup_mult"] == projections.matchup_multiplier("RB", "smash")
