"""Every source boundary must canonicalize before it compares or looks up."""
import pytest

from nfl_mcp import coaching_tools, matchup_tools, projections


class TestProjectionHomeAwayDetection:
    """`is_home` compares the caller's spelling against a canonical code."""

    def _lines(self):
        # Week 2: NYG @ LAR. LAR is the HOME team.
        return {"LAR": {
            "home_team": "LAR", "away_team": "NYG",
            "home_implied_total": 27.5, "away_implied_total": 20.4,
            "is_fallback": False,
        }}

    @pytest.mark.parametrize("spelling", ["LAR", "LA"])
    def test_alias_does_not_flip_home_and_away(self, monkeypatch, spelling):
        # nflverse writes LA, Sleeper writes WAS/OAK. `get_game_lines`
        # normalizes its lookup but returns the canonical spelling, so an
        # un-normalized comparison failed on HOME games and priced the player
        # off the OPPONENT's implied total. Verified live: LA gave 20.4 where
        # LAR gave 27.5 for the same player.
        engine = projections.get_projection_engine()
        lines = self._lines()
        monkeypatch.setattr(engine.vegas, "get_game_lines", lambda t, ln=None: lines["LAR"])

        result = engine._project_one(
            {"name": "Puka Nacua", "position": "WR", "team": spelling, "opponent": "NYG"},
            values_index={}, rankings={}, lines=lines,
        )
        assert result["implied_total"] == 27.5
        assert result["opponent_implied_total"] == 20.4


class TestCoachingLookups:
    @pytest.mark.parametrize("code", ["WSH", "WAS", "Washington Commanders"])
    def test_espn_id_resolves_for_every_spelling(self, code):
        # The map was keyed on WAS while the rest of the codebase emits WSH, so
        # get_coaching_staff("WSH") sent the literal string as an ESPN team id
        # and the API answered 400.
        assert coaching_tools._get_espn_team_id(code) == "28"

    def test_numeric_ids_still_pass_through(self):
        assert coaching_tools._get_espn_team_id("28") == "28"

    def test_unknown_input_is_returned_unchanged(self):
        assert coaching_tools._get_espn_team_id("ZZZ") == "ZZZ"

    @pytest.mark.parametrize("code", ["WSH", "WAS"])
    def test_scheme_lookup_resolves_for_every_spelling(self, code):
        from nfl_mcp.teams import normalize_team
        key = normalize_team(code) or code
        assert coaching_tools.TEAM_SCHEMES.get(key) is not None


class TestMatchupOpponentNormalization:
    @pytest.mark.parametrize("alias,canonical", [
        ("WAS", "WSH"), ("JAC", "JAX"), ("LA", "LAR"), ("OAK", "LV"), ("SD", "LAC"),
    ])
    def test_aliases_beyond_the_old_two_entry_map(self, alias, canonical):
        # The hand-rolled map here handled only WAS and JAC; everything else
        # fell through to a neutral matchup tier.
        from nfl_mcp.teams import normalize_team
        assert normalize_team(alias) == canonical

    def test_analyzer_still_exposes_the_helper(self):
        assert hasattr(matchup_tools, "normalize_team")
