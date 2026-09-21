"""Two games of points allowed is noise, and must not arrive as a tier.

Early in 2026 the rankings put Houston at #3 "elite" against RBs (12.4 points
allowed per game) and #31 "smash" against WRs (43.0) *at the same time*, off two
games, with no shrinkage and no blend with the prior season. That fed the
projection's RB multiplier at full weight.
"""
import pytest

from nfl_mcp.matchup_tools import (
    MIN_GAMES_FOR_TIERS,
    SHRINKAGE_GAMES,
    DefenseRankingsAnalyzer,
)


def _csv(rows):
    head = ("season,week,season_type,position,recent_team,opponent_team,"
            "fantasy_points_ppr\n")
    return head + "".join(
        f"2026,{wk},REG,RB,OFF,{team},{pts}\n" for team, wk, pts in rows
    )


class _Resp:
    def __init__(self, text):
        self.text, self.status_code = text, 200

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, text):
        self._resp = _Resp(text)

    async def get(self, url, **kw):
        return self._resp


async def _rank(rows):
    analyzer = DefenseRankingsAnalyzer(db=None)
    out = await analyzer._fetch_nflverse_rankings(_Client(_csv(rows)), 2026)
    return {r["team"]: r for r in out["RB"]}


class TestShrinkage:
    @pytest.mark.asyncio
    async def test_reported_average_is_pulled_toward_the_league_mean(self):
        """An extreme two-game defense must not be reported at face value."""
        rows = [("TOUGH", 1, 5), ("TOUGH", 2, 5),
                ("SOFT", 1, 45), ("SOFT", 2, 45)]
        got = await _rank(rows)

        assert got["TOUGH"]["points_allowed_observed"] == 5.0
        assert got["SOFT"]["points_allowed_observed"] == 45.0
        # League mean 25; with 2 games against 6 of prior weight the reported
        # figure sits a quarter of the way out from the mean.
        assert got["TOUGH"]["points_allowed_avg"] == pytest.approx(20.0, abs=0.2)
        assert got["SOFT"]["points_allowed_avg"] == pytest.approx(30.0, abs=0.2)

    @pytest.mark.asyncio
    async def test_more_games_shrink_less(self):
        few = await _rank([("A", 1, 5), ("A", 2, 5), ("B", 1, 45), ("B", 2, 45)])
        many = await _rank(
            [("A", w, 5) for w in range(1, 13)] + [("B", w, 45) for w in range(1, 13)]
        )
        assert many["A"]["points_allowed_avg"] < few["A"]["points_allowed_avg"]

    @pytest.mark.asyncio
    async def test_ordering_is_preserved(self):
        """Shrinkage compresses, it does not reshuffle — which is why the tier
        gate below is the part that actually fixes the noise."""
        got = await _rank([("A", 1, 5), ("A", 2, 5), ("B", 1, 25), ("B", 2, 25),
                           ("C", 1, 45), ("C", 2, 45)])
        assert got["A"]["rank"] < got["B"]["rank"] < got["C"]["rank"]


class TestProvisionalTiers:
    @pytest.mark.asyncio
    async def test_a_small_sample_yields_no_tier_at_all(self):
        got = await _rank([("A", 1, 5), ("A", 2, 5), ("B", 1, 45), ("B", 2, 45)])
        assert all(r["is_provisional"] for r in got.values())
        assert {r["matchup_tier"] for r in got.values()} == {"neutral"}

    @pytest.mark.asyncio
    async def test_enough_games_restores_real_tiers(self):
        weeks = range(1, MIN_GAMES_FOR_TIERS + 1)
        rows = [("A", w, 5) for w in weeks] + [("B", w, 45) for w in weeks]
        got = await _rank(rows)
        assert not any(r["is_provisional"] for r in got.values())
        assert {r["matchup_tier"] for r in got.values()} != {"neutral"}

    @pytest.mark.asyncio
    async def test_games_sampled_is_reported(self):
        got = await _rank([("A", 1, 5), ("A", 2, 5), ("B", 1, 45), ("B", 2, 45)])
        assert all(r["games_sampled"] == 2 for r in got.values())


class TestNeutralIsHarmless:
    def test_a_neutral_tier_does_not_move_a_projection(self):
        """The point of withholding the tier: no effect beats a wrong effect."""
        from nfl_mcp.projections import matchup_multiplier

        for position in ("QB", "RB", "WR", "TE"):
            assert matchup_multiplier(position, "neutral") == 1.0
        # And the tier it would otherwise have produced does move it.
        assert matchup_multiplier("RB", "elite") != 1.0


class TestConstants:
    def test_shrinkage_weight_is_meaningful(self):
        assert SHRINKAGE_GAMES >= 4.0

    def test_tier_threshold_covers_the_early_season(self):
        assert MIN_GAMES_FOR_TIERS >= 4
