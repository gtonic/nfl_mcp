"""Defenses and kickers are priced off the game total, not a rank bucket."""
import pytest

from nfl_mcp.projections import defense_base, kicker_base


class TestDefenseBase:
    def test_scales_inversely_with_the_opponent_total(self):
        # The whole point: a defense scores when the opponent does not. The
        # generic path scaled a flat 7.0 by the defense's *own* implied total,
        # which rewarded defenses on high-scoring teams — backwards.
        shutout_spot = defense_base(15.0)
        neutral = defense_base(21.0)
        shootout = defense_base(30.0)
        assert shutout_spot > neutral > shootout

    @pytest.mark.parametrize("opp_total,expected", [
        (16.0, 11.0), (19.0, 9.5), (22.0, 8.0), (25.0, 6.5), (28.0, 5.0), (31.0, 3.5),
    ])
    def test_bucket_boundaries(self, opp_total, expected):
        assert defense_base(opp_total) == expected

    def test_without_vegas_it_falls_back_to_the_old_constant(self):
        # No lines is not the same as a neutral matchup; keep the previous
        # behaviour rather than inventing a number.
        assert defense_base(None) == 7.0

    def test_spread_across_the_range_is_meaningful(self):
        # A 7.5-point spread between the best and worst spot is the difference
        # between a startable defense and a streaming mistake.
        assert defense_base(15.0) - defense_base(31.0) == 7.5


class TestKickerBase:
    def test_rises_with_scoring_but_flattens_at_the_top(self):
        # A team expected to score 30 trades field goals for touchdowns, which
        # pays the kicker one point instead of three.
        assert kicker_base(19.0) < kicker_base(26.0)
        assert kicker_base(30.0) < kicker_base(26.0)

    def test_low_scoring_team_is_penalised(self):
        assert kicker_base(15.0) < kicker_base(22.0)

    def test_without_vegas_it_falls_back_to_the_old_constant(self):
        assert kicker_base(None) == 8.0


class TestEngineWiring:
    @pytest.mark.asyncio
    async def test_defense_uses_opponent_total_and_reports_it(self, monkeypatch):
        from nfl_mcp import projections

        engine = projections.get_projection_engine()

        # SF hosts MIA: SF implied 29.1, MIA implied 16.0. The defense must be
        # priced off 16.0, not 29.1.
        lines = {"SF": {
            "home_team": "SF", "away_team": "MIA",
            "home_implied_total": 29.1, "away_implied_total": 16.0,
            "is_fallback": False,
        }}
        monkeypatch.setattr(engine.vegas, "get_game_lines", lambda team, ln=None, **_: lines["SF"])

        result = engine._project_one(
            {"name": "SF", "position": "DST", "team": "SF", "opponent": "MIA"},
            values_index={}, rankings={}, lines=lines,
        )

        assert result["breakdown"]["base_source"] == "opponent_total"
        assert result["opponent_implied_total"] == 16.0
        assert result["projected_points"] == 11.0
        # Neither multiplier should silently re-apply the team's own total.
        assert result["breakdown"]["environment_mult"] == 1.0
        assert result["breakdown"]["matchup_mult"] == 1.0

    @pytest.mark.asyncio
    async def test_kicker_uses_its_own_team_total(self, monkeypatch):
        from nfl_mcp import projections

        engine = projections.get_projection_engine()
        lines = {"CHI": {
            "home_team": "CHI", "away_team": "MIN",
            "home_implied_total": 26.4, "away_implied_total": 21.9,
            "is_fallback": False,
        }}
        monkeypatch.setattr(engine.vegas, "get_game_lines", lambda team, ln=None, **_: lines["CHI"])

        result = engine._project_one(
            {"name": "Cairo Santos", "position": "K", "team": "CHI", "opponent": "MIN"},
            values_index={}, rankings={}, lines=lines,
        )
        assert result["breakdown"]["base_source"] == "team_total"
        assert result["projected_points"] == 9.5

    @pytest.mark.asyncio
    async def test_fallback_lines_do_not_fabricate_a_signal(self, monkeypatch):
        from nfl_mcp import projections

        engine = projections.get_projection_engine()
        lines = {"SF": {
            "home_team": "SF", "away_team": "MIA",
            "home_implied_total": 24.0, "away_implied_total": 21.0,
            "is_fallback": True,
        }}
        monkeypatch.setattr(engine.vegas, "get_game_lines", lambda team, ln=None, **_: lines["SF"])

        result = engine._project_one(
            {"name": "SF", "position": "DST", "team": "SF", "opponent": "MIA"},
            values_index={}, rankings={}, lines=lines,
        )
        assert result["projected_points"] == 7.0


class TestSeasonScoringFallback:
    """No Vegas lines: K/DEF are priced off the season's offense read, as start/sit is."""

    @staticmethod
    def _fallback_projection(engine, monkeypatch, name, position, team, opponent, status=None):
        lines = {team: {"home_team": team, "away_team": opponent, "home_implied_total": None,
                        "away_implied_total": None, "is_fallback": True}}
        monkeypatch.setattr(engine.vegas, "get_game_lines", lambda t, ln=None, **_: lines[team])
        player = {"name": name, "position": position, "team": team, "opponent": opponent}
        if status:
            player["injury"] = {"status": status}
        return engine._project_one(player, values_index={}, rankings={}, lines=lines)

    @pytest.mark.asyncio
    async def test_defenses_are_no_longer_one_constant(self, monkeypatch):
        from nfl_mcp import projections
        from nfl_mcp.scoring import resolve_scoring

        engine = projections.get_projection_engine()
        soft = self._fallback_projection(engine, monkeypatch, "SF", "DEF", "SF", "CAR")
        hard = self._fallback_projection(engine, monkeypatch, "PIT", "DEF", "PIT", "DET")
        assert soft["projected_points"] == hard["projected_points"] == 7.0

        async def _unit(position, team, opponent, season, model):
            ppg = {"CAR": 15.0, "DET": 31.0}[opponent]
            return {"projected_points": projections.defense_base(ppg), "offense_rank": 1,
                    "matchup_tier": "neutral", "points_per_game": ppg}

        monkeypatch.setattr(projections, "_unit_matchup", _unit)
        await projections._apply_unit_fallback([soft, hard], 2026, resolve_scoring("ppr"))
        assert soft["projected_points"] == 11.0
        assert hard["projected_points"] == 3.5
        assert soft["breakdown"]["base_source"] == "offense_rank"
        assert soft["unit_matchup"]["points_per_game"] == 15.0

    @pytest.mark.asyncio
    async def test_vegas_bye_and_injury(self, monkeypatch):
        from nfl_mcp import projections
        from nfl_mcp.scoring import resolve_scoring

        engine = projections.get_projection_engine()
        live = {"position": "K", "team": "CHI", "opponent": "MIN", "vegas_active": True,
                "projected_points": 9.5, "breakdown": {}}
        bye = {"position": "K", "team": "CHI", "opponent": "", "on_bye": True,
               "projected_points": 0.0, "breakdown": {}}
        out = self._fallback_projection(engine, monkeypatch, "Some Kicker", "K", "BUF", "MIA",
                                        status="Out")

        async def _unit(*_a, **_k):
            return {"projected_points": 10.0}

        monkeypatch.setattr(projections, "_unit_matchup", _unit)
        await projections._apply_unit_fallback([live, bye, out], 2026, resolve_scoring("ppr"))
        assert live["projected_points"] == 9.5      # live lines win
        assert bye["projected_points"] == 0.0       # a bye stays zero
        assert out["projected_points"] == 0.0       # ruled out stays zero
