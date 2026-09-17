"""Roster-strength questions must ignore players who cannot play."""
import pytest

from nfl_mcp.sleeper_tools import active_enriched


def _roster(**kw):
    base = {
        "players_enriched": [
            {"player_id": "rb1", "position": "RB", "full_name": "Stashed Star"},
            {"player_id": "rb2", "position": "RB", "full_name": "Healthy Back"},
            {"player_id": "wr1", "position": "WR", "full_name": "Wideout"},
        ],
    }
    base.update(kw)
    return base


class TestActiveEnriched:
    def test_reserve_players_are_removed(self):
        roster = _roster(reserve=["rb1"])
        assert [p["player_id"] for p in active_enriched(roster)] == ["rb2", "wr1"]

    def test_taxi_players_are_removed(self):
        roster = _roster(taxi=["wr1"])
        assert [p["player_id"] for p in active_enriched(roster)] == ["rb1", "rb2"]

    def test_both_lists_apply(self):
        roster = _roster(reserve=["rb1"], taxi=["wr1"])
        assert [p["player_id"] for p in active_enriched(roster)] == ["rb2"]

    @pytest.mark.parametrize("kw", [{}, {"reserve": None}, {"reserve": [], "taxi": []}])
    def test_roster_without_stashes_is_unchanged(self, kw):
        assert len(active_enriched(_roster(**kw))) == 3

    def test_missing_enriched_list_is_empty_not_an_error(self):
        assert active_enriched({"reserve": ["x"]}) == []

    def test_ids_are_compared_as_strings(self):
        # Sleeper sends ids as strings; a caller holding ints must still match.
        roster = {
            "players_enriched": [{"player_id": 123, "position": "RB"}],
            "reserve": ["123"],
        }
        assert active_enriched(roster) == []


class TestFaabReplacementValue:
    """The scenario the tool exists for: your starter is hurt."""

    def test_an_ir_starter_no_longer_counts_as_your_replacement(self):
        # slots=2 for RB, so replacement_value is the 2nd-best *available* RB.
        # Counting an IR'd RB1 pushed the real replacement down a rank, shrank
        # `upgrade` to 0 and emitted "you're already strong at RB".
        roster = {
            "players_enriched": [
                {"player_id": "ir_star", "position": "RB", "full_name": "IR Star"},
                {"player_id": "ok1", "position": "RB", "full_name": "Fine Back"},
                {"player_id": "ok2", "position": "RB", "full_name": "Scrub"},
            ],
            "reserve": ["ir_star"],
        }
        remaining = [p["full_name"] for p in active_enriched(roster)]
        assert "IR Star" not in remaining
        assert remaining == ["Fine Back", "Scrub"]
