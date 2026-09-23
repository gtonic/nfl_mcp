"""Start/sit must rank by expected points, not by how much we know.

`confidence` is a data-quality score: 25% matchup, 25% snap share, 20% health,
15% projection, 15% trend — and the projection share was quantised into four
buckets. Sorting by it inverted the ranking: an 8-point receiver in a smash
matchup beat a 17-point receiver in a tough one, and `must_start` required a
favourable matchup, so a star facing a top defense could never be one.

The projection engine had already concluded from its own backtest that matchup
is worth *nothing* for WRs (`_MATCHUP_POS_STRENGTH["WR"] == 0.0`). The ranking
layer weighted the same signal at 25% and contradicted the model beneath it.
"""
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp.lineup_optimizer_tools import MEANINGFUL_SWAP_GAIN, _good_game_thresholds

pytestmark = pytest.mark.usefixtures("offline_sources")  # no ambient network reads


def _analyzer(tier="neutral"):
    from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
    analyzer = DefenseRankingsAnalyzer(db=None)
    analyzer.fetch_defense_rankings = AsyncMock(return_value={})
    analyzer.get_matchup_difficulty = lambda pos, opp, rk=None: {
        "rank": 28, "matchup_tier": tier}
    return analyzer


async def _analyze(name, pos, points, tier="neutral", injury=None, scoring="ppr"):
    optimizer = lo.LineupOptimizer(db=None, defense_analyzer=_analyzer(tier),
                                   auto_project=False)
    return await optimizer.analyze_player(
        name, "", pos, "KC", "OPP",
        injury_data={"status": injury} if injury else None,
        projection_data={"projected_points": points},
        scoring=scoring,
    )


class TestDecisionFollowsPoints:
    @pytest.mark.asyncio
    async def test_a_star_in_a_tough_matchup_can_be_must_start(self):
        """The old rule required smash/favorable, so this was impossible."""
        a = await _analyze("Star", "WR", 17.0, tier="tough")
        assert a.decision == "must_start"

    @pytest.mark.asyncio
    async def test_a_weak_player_in_a_smash_matchup_is_not_a_must_start(self):
        a = await _analyze("Scrub", "WR", 8.0, tier="smash")
        assert a.decision not in ("must_start", "start")

    @pytest.mark.asyncio
    async def test_an_unavailable_player_is_always_must_sit(self):
        a = await _analyze("Hurt", "WR", 20.0, tier="smash", injury="out")
        assert a.decision == "must_sit"

    @pytest.mark.asyncio
    async def test_no_projection_yields_flex_rather_than_a_confident_read(self):
        a = await _analyze("Unknown", "WR", 0.0)
        assert a.decision == "flex"

    @pytest.mark.asyncio
    async def test_thresholds_follow_the_league_scoring(self):
        """14 points is a good WR week in half PPR, merely fine in full PPR."""
        full = await _analyze("X", "WR", 14.0, scoring="ppr")
        half = await _analyze("X", "WR", 14.0, scoring="half_ppr")
        assert full.decision == "start"
        assert half.decision == "must_start"


class TestRankingOrder:
    @pytest.mark.asyncio
    async def test_compare_players_picks_the_higher_projection(self):
        """The reported inversion, end to end."""
        analyses = [await _analyze("WR1", "WR", 17.0, tier="tough"),
                    await _analyze("WR4", "WR", 8.0, tier="smash")]
        analyses.sort(key=lambda x: (x.projected_points, x.confidence), reverse=True)
        assert analyses[0].player_name == "WR1"
        # And the loser is the one the old sort would have crowned.
        assert analyses[1].confidence > analyses[0].confidence

    @pytest.mark.asyncio
    async def test_confidence_only_breaks_a_tie(self):
        low = await _analyze("LowConf", "WR", 12.0, tier="tough")
        high = await _analyze("HighConf", "WR", 12.0, tier="smash")
        ranked = sorted([low, high], key=lambda x: (x.projected_points, x.confidence),
                        reverse=True)
        assert ranked[0].player_name == "HighConf"


class TestCompareTool:
    @pytest.mark.asyncio
    async def test_verdict_is_scaled_to_the_models_error(self, monkeypatch):
        monkeypatch.setattr(lo, "get_lineup_optimizer",
                            lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                       defense_analyzer=_analyzer()))
        players = [{"name": "A", "position": "WR", "team": "KC", "opponent": "OPP",
                    "projection": {"projected_points": 17.0}},
                   {"name": "B", "position": "WR", "team": "KC", "opponent": "OPP",
                    "projection": {"projected_points": 16.5}}]
        out = await lo.compare_players_for_slot(players, slot="WR2")

        assert out["winner"]["player"] == "A"
        assert out["points_gap"] == 0.5
        # Half a point is not an edge at MAE ~5.8.
        assert "Coin flip" in out["verdict"]
        assert out["winner"]["projected_points"] == 17.0

    @pytest.mark.asyncio
    async def test_a_real_gap_is_called_clear(self, monkeypatch):
        monkeypatch.setattr(lo, "get_lineup_optimizer",
                            lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                       defense_analyzer=_analyzer()))
        players = [{"name": "A", "position": "WR", "team": "KC", "opponent": "OPP",
                    "projection": {"projected_points": 18.0}},
                   {"name": "B", "position": "WR", "team": "KC", "opponent": "OPP",
                    "projection": {"projected_points": 6.0}}]
        out = await lo.compare_players_for_slot(players, slot="WR2")
        assert "Clear choice" in out["verdict"]
        assert out["points_gap"] == 12.0


class TestRosterRecommendations:
    @pytest.mark.asyncio
    async def test_sorted_by_points(self, monkeypatch):
        monkeypatch.setattr(lo, "get_lineup_optimizer",
                            lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                       defense_analyzer=_analyzer()))
        players = [{"name": "Low", "position": "WR", "team": "KC", "opponent": "OPP",
                    "projection": {"projected_points": 4.0}},
                   {"name": "High", "position": "WR", "team": "KC", "opponent": "OPP",
                    "projection": {"projected_points": 19.0}}]
        out = await lo.get_roster_recommendations(players)
        assert [r["player_name"] for r in out["recommendations"]] == ["High", "Low"]


class TestLineupGrade:
    @pytest.mark.asyncio
    async def test_an_optimal_lineup_grades_a(self, monkeypatch):
        """Nothing on the bench beats a starter -> you started your best."""
        monkeypatch.setattr(lo, "get_lineup_optimizer",
                            lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                       defense_analyzer=_analyzer()))
        lineup = {
            "WR": [{"name": "Good", "team": "KC", "opponent": "OPP", "position": "WR",
                    "projection": {"projected_points": 18.0}}],
            "BENCH": [{"name": "Worse", "team": "KC", "opponent": "OPP", "position": "WR",
                       "projection": {"projected_points": 5.0}}],
        }
        out = await lo.analyze_full_lineup(lineup)
        assert out["lineup_grade"] == "A"
        assert out["lineup_efficiency_pct"] == 100.0
        assert out["suggested_changes"] == []

    @pytest.mark.asyncio
    async def test_leaving_points_on_the_bench_costs_the_grade(self, monkeypatch):
        monkeypatch.setattr(lo, "get_lineup_optimizer",
                            lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                       defense_analyzer=_analyzer()))
        lineup = {
            "WR": [{"name": "Weak", "team": "KC", "opponent": "OPP", "position": "WR",
                    "projection": {"projected_points": 3.0}}],
            "BENCH": [{"name": "Strong", "team": "KC", "opponent": "OPP", "position": "WR",
                       "projection": {"projected_points": 19.0}}],
        }
        out = await lo.analyze_full_lineup(lineup)
        assert out["lineup_grade"] == "F"
        assert out["lineup_efficiency_pct"] < 50
        assert out["suggested_changes"]
        change = out["suggested_changes"][0]
        assert change["bench_in"] == "Strong"
        assert change["gain"] == 16.0


class TestSwapThreshold:
    def test_a_swap_has_to_beat_the_models_error(self):
        assert MEANINGFUL_SWAP_GAIN >= 2.0

    def test_good_game_thresholds_are_used_for_weak_spots(self):
        adequate, good = _good_game_thresholds("WR", 1.0)
        assert adequate == 10 and good == 16


class TestSlotEligibility:
    def test_flex_takes_only_flex_eligible_positions(self):
        from nfl_mcp.lineup_optimizer_tools import slot_accepts

        for position in ("RB", "WR", "TE"):
            assert slot_accepts("FLEX", position)
        # The bug: a weak FLEX matched any bench player, so a kicker or a
        # defense was offered for a flex spot.
        for position in ("QB", "K", "DST", "DEF"):
            assert not slot_accepts("FLEX", position)

    def test_superflex_also_takes_a_quarterback(self):
        from nfl_mcp.lineup_optimizer_tools import slot_accepts

        assert slot_accepts("SUPERFLEX", "QB")
        assert not slot_accepts("SUPERFLEX", "K")

    def test_defense_slot_accepts_either_spelling(self):
        from nfl_mcp.lineup_optimizer_tools import slot_accepts

        assert slot_accepts("DST", "DEF") and slot_accepts("DEF", "DST")

    def test_an_unknown_slot_requires_an_exact_match(self):
        """Falls back to strict, not to "anything goes"."""
        from nfl_mcp.lineup_optimizer_tools import slot_accepts

        assert slot_accepts("QB", "QB")
        assert not slot_accepts("QB", "RB")
        assert not slot_accepts("SOMETHING_NEW", "RB")

    @pytest.mark.asyncio
    async def test_a_kicker_is_never_suggested_for_a_flex(self, monkeypatch):
        monkeypatch.setattr(lo, "get_lineup_optimizer",
                            lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                       defense_analyzer=_analyzer()))

        async def _state():
            return (2026, 4)
        monkeypatch.setattr("nfl_mcp.nfl_tools.get_current_season_and_week", _state)

        lineup = {
            "FLEX": [{"name": "WeakFlex", "team": "KC", "opponent": "OPP",
                      "position": "WR", "projection": {"projected_points": 2.0}}],
            "BENCH": [{"name": "BigKicker", "team": "KC", "opponent": "OPP",
                       "position": "K", "projection": {"projected_points": 14.0}}],
        }
        out = await lo.analyze_full_lineup(lineup)
        assert all(c["bench_in"] != "BigKicker" for c in out["suggested_changes"])

    @pytest.mark.asyncio
    async def test_a_bench_player_without_a_position_is_skipped_not_guessed(self, monkeypatch):
        monkeypatch.setattr(lo, "get_lineup_optimizer",
                            lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                       defense_analyzer=_analyzer()))

        async def _state():
            return (2026, 4)
        monkeypatch.setattr("nfl_mcp.nfl_tools.get_current_season_and_week", _state)

        lineup = {
            "WR": [{"name": "Starter", "team": "KC", "opponent": "OPP", "position": "WR",
                    "projection": {"projected_points": 3.0}}],
            "BENCH": [{"name": "Mystery", "team": "KC", "opponent": "OPP",
                       "projection": {"projected_points": 20.0}}],
        }
        out = await lo.analyze_full_lineup(lineup)
        assert all(b["player_name"] != "Mystery" for b in out["bench"])
