"""A scheme belongs to the play-caller, not the franchise.

`get_scheme_classification` used to return a hand-maintained per-team table with
no date on it, so after a coordinator change it asserted the previous regime's
scheme as current fact. It now resolves the scheme from the live staff and says
plainly when it could not.
"""
import pytest

from nfl_mcp import coaching_tools
from nfl_mcp.coaching_tools import (
    COACH_DEFENSIVE_SCHEMES,
    COACH_OFFENSIVE_SCHEMES,
    CURATED_AS_OF,
    TEAM_SCHEMES,
    get_scheme_classification,
)


def _staff(monkeypatch, head=None, oc=None, dc=None, success=True):
    async def _fake(team_id, season=None):
        return {
            "success": success,
            "head_coach": {"name": head} if head else None,
            "offensive_coordinator": {"name": oc} if oc else None,
            "defensive_coordinator": {"name": dc} if dc else None,
        }
    monkeypatch.setattr(coaching_tools, "get_coaching_staff", _fake)


class TestResolvedFromTheCurrentStaff:
    @pytest.mark.asyncio
    async def test_coordinator_wins_over_the_team_table(self, monkeypatch):
        # SF's table entry is "Shanahan Wide Zone"; a Coryell OC must override it.
        assert TEAM_SCHEMES["SF"]["offense"] == "Shanahan Wide Zone"
        _staff(monkeypatch, head="Kyle Shanahan", oc="Todd Monken", dc="Jim Schwartz")

        result = await get_scheme_classification("SF")

        assert result["offensive_scheme"] == "Coryell/Vertical"
        assert result["offense"]["source"] == "coach"
        assert result["offense"]["attributed_to"] == "Todd Monken"
        assert result["defensive_scheme"] == "4-3 Wide-9"
        assert result["is_fallback"] is False
        assert result["warnings"] == []

    @pytest.mark.asyncio
    async def test_falls_back_to_the_head_coach_when_no_coordinator(self, monkeypatch):
        _staff(monkeypatch, head="Sean McVay")
        result = await get_scheme_classification("LAR")

        assert result["offensive_scheme"] == "McVay Offense"
        assert result["offense"]["role"] == "Head Coach"
        assert result["offense"]["attributed_to"] == "Sean McVay"

    @pytest.mark.asyncio
    async def test_a_staff_change_changes_the_answer(self, monkeypatch):
        """The whole point: the table does not have to be edited every January."""
        _staff(monkeypatch, head="Kyle Shanahan")
        before = await get_scheme_classification("SF")

        _staff(monkeypatch, head="Kyle Shanahan", oc="Kliff Kingsbury")
        after = await get_scheme_classification("SF")

        assert before["offensive_scheme"] == "Shanahan Wide Zone"
        assert after["offensive_scheme"] == "Air Raid/Spread"


class TestHonestyAboutTheFallback:
    @pytest.mark.asyncio
    async def test_an_unknown_coach_is_named_not_papered_over(self, monkeypatch):
        _staff(monkeypatch, head="Some New Coach", oc="Nobody We Know")
        result = await get_scheme_classification("KC")

        assert result["offense"]["source"] == "team_table"
        assert result["offense"]["unmatched_coach"] == "Nobody We Know"
        assert result["is_fallback"] is True
        assert any("Nobody We Know" in w for w in result["warnings"])
        assert result["as_of"] == CURATED_AS_OF

    @pytest.mark.asyncio
    async def test_a_dead_staff_lookup_still_answers_but_says_so(self, monkeypatch):
        async def _boom(team_id, season=None):
            raise RuntimeError("ESPN down")
        monkeypatch.setattr(coaching_tools, "get_coaching_staff", _boom)

        result = await get_scheme_classification("KC")

        assert result["found"] is True
        assert result["offensive_scheme"] == TEAM_SCHEMES["KC"]["offense"]
        assert result["is_fallback"] is True
        assert len(result["warnings"]) == 2   # both sides are guesses

    @pytest.mark.asyncio
    async def test_one_side_resolved_one_side_guessed(self, monkeypatch):
        _staff(monkeypatch, head="Andy Reid", dc="Steve Spagnuolo")
        result = await get_scheme_classification("KC")

        assert result["offense"]["source"] == "coach"
        assert result["defense"]["source"] == "coach"
        assert result["warnings"] == []

        _staff(monkeypatch, head="Andy Reid", dc="Unknown Person")
        result = await get_scheme_classification("KC")
        assert result["offense"]["source"] == "coach"
        assert result["defense"]["source"] == "team_table"
        assert len(result["warnings"]) == 1
        assert "defensive" in result["warnings"][0]

    @pytest.mark.asyncio
    async def test_normalizes_team_codes(self, monkeypatch):
        _staff(monkeypatch)
        for code in ("WAS", "WSH"):
            result = await get_scheme_classification(code, use_live_staff=False)
            assert result["found"] is True


class TestCuratedTablesAreDated:
    def test_every_response_path_carries_as_of(self):
        assert CURATED_AS_OF

    def test_coach_tables_are_not_empty_and_have_no_blank_entries(self):
        for table in (COACH_OFFENSIVE_SCHEMES, COACH_DEFENSIVE_SCHEMES):
            assert table
            assert all(name.strip() and scheme.strip() for name, scheme in table.items())

    @pytest.mark.asyncio
    async def test_coaching_tree_says_absence_means_absence(self):
        from nfl_mcp.coaching_tools import get_coaching_tree

        result = await get_coaching_tree("Nobody At All")
        assert result["found"] is False
        assert result["as_of"] == CURATED_AS_OF
        # Not "this coach has no lineage" — "this list does not have him".
        assert "not that the coach has no lineage" in result["message"]

    @pytest.mark.asyncio
    async def test_coaching_tree_hit_is_labelled_as_history(self):
        from nfl_mcp.coaching_tools import get_coaching_tree

        result = await get_coaching_tree("Andy Reid")
        assert result["found"] is True
        assert result["as_of"] == CURATED_AS_OF
        assert "not a statement about current employment" in result["note"]
