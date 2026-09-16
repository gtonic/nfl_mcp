"""One canonical team mapping, applied at every source boundary."""
import pytest

from nfl_mcp.teams import CANONICAL_TEAMS, normalize_team


class TestNormalizeTeam:
    @pytest.mark.parametrize("value,expected", [
        # Sleeper's athlete rows disagree with the odds feed and ESPN. Left
        # unnormalized these do not raise — they simply fail to join, which is
        # how a healthy quarterback once looked like he was on a bye.
        ("WAS", "WSH"),
        ("OAK", "LV"),
        ("JAC", "JAX"),
        ("LA", "LAR"),
        # Relocations still present in historical data.
        ("SD", "LAC"),
        ("STL", "LAR"),
        # Already canonical, in any casing.
        ("WSH", "WSH"),
        ("wsh", "WSH"),
        (" kc ", "KC"),
        # Full names, as the odds feed sends them.
        ("Las Vegas Raiders", "LV"),
        ("Washington Commanders", "WSH"),
    ])
    def test_variants_map_to_canonical(self, value, expected):
        assert normalize_team(value) == expected

    @pytest.mark.parametrize("value", [None, "", "   ", "NOT_A_TEAM", "ZZZ"])
    def test_unknown_returns_none(self, value):
        # Free agents carry "" in the athlete rows; a caller must be able to
        # tell that apart from a real team rather than getting a guess.
        assert normalize_team(value) is None

    def test_every_canonical_code_is_stable(self):
        for code in CANONICAL_TEAMS:
            assert normalize_team(code) == code

    def test_no_alias_shadows_a_canonical_code(self):
        from nfl_mcp.teams import TEAM_ALIASES
        assert not (set(TEAM_ALIASES) & CANONICAL_TEAMS)

    def test_all_aliases_resolve_to_canonical_codes(self):
        from nfl_mcp.teams import TEAM_ALIASES
        assert set(TEAM_ALIASES.values()) <= CANONICAL_TEAMS


class TestCallersUseIt:
    def test_vegas_analyzer_normalizes_through_the_shared_map(self):
        from nfl_mcp.vegas_tools import get_vegas_analyzer
        a = get_vegas_analyzer()
        assert a._normalize_team("WAS") == "WSH"
        assert a._normalize_team("OAK") == "LV"
        assert a._normalize_team("Los Angeles Rams") == "LAR"

    def test_unknown_value_stays_visible_rather_than_vanishing(self):
        from nfl_mcp.vegas_tools import get_vegas_analyzer
        a = get_vegas_analyzer()
        assert a._normalize_team("XYZ") == "XYZ"


class TestPartialNames:
    """The odds feed and ESPN both send city-only spellings."""

    @pytest.mark.parametrize("value,expected", [
        ("Kansas City", "KC"),
        ("Green Bay", "GB"),
        ("Buccaneers", "TB"),
        ("49ers", "SF"),
    ])
    def test_unambiguous_partial_resolves(self, value, expected):
        assert normalize_team(value) == expected

    def test_ambiguous_partial_is_refused(self):
        # "New York" is two teams. The previous implementation returned
        # whichever came first in dict order, which is a coin flip dressed up
        # as an answer.
        assert normalize_team("New York") is None
