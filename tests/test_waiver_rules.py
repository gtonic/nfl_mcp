"""Waiver configuration reported, not guessed at.

Two inferences were tried in live use and both were wrong: "never dropped
therefore instant" and "`daily_waivers=1` therefore instant". These tests pin
the module to reporting the settings and refusing the verdict.
"""
from nfl_mcp.waiver_rules import waiver_rules

VLBG = {"settings": {"waiver_type": 0, "daily_waivers": 1, "daily_waivers_days": 1372,
                     "waiver_clear_days": 1, "waiver_day_of_week": 2, "waiver_budget": 100}}
ROPEWAY = {"settings": {"waiver_type": 0, "daily_waivers": 0, "daily_waivers_days": 5461,
                        "waiver_clear_days": 2, "waiver_day_of_week": 2, "waiver_budget": 100}}
FAAB_LEAGUE = {"settings": {"waiver_type": 2, "waiver_budget": 100,
                            "waiver_clear_days": 1, "waiver_day_of_week": 2}}


class TestWaiverType:
    def test_priority_when_budget_is_inert(self):
        """`waiver_budget: 100` with `waiver_type: 0` is a leftover, not a budget."""
        assert waiver_rules(VLBG)["waiver_type"] == "priority"
        assert waiver_rules(ROPEWAY)["waiver_type"] == "priority"
        assert waiver_rules(VLBG)["budget"] is None

    def test_faab_needs_both_the_type_and_a_budget(self):
        rules = waiver_rules(FAAB_LEAGUE)
        assert rules["waiver_type"] == "faab"
        assert rules["budget"] == 100

    def test_faab_type_without_budget_is_not_faab(self):
        league = {"settings": {"waiver_type": 2, "waiver_budget": 0}}
        assert waiver_rules(league)["waiver_type"] == "priority"


class TestProcessing:
    def test_weekly_vs_daily(self):
        assert waiver_rules(ROPEWAY)["processing"] == "weekly"
        assert waiver_rules(VLBG)["processing"] == "daily"

    def test_weekday_name_matches_the_app(self):
        """Value 2 shows as Wednesday in the league app."""
        assert waiver_rules(ROPEWAY)["waiver_day"] == "Wednesday"

    def test_out_of_range_weekday_is_none_not_a_crash(self):
        assert waiver_rules({"settings": {"waiver_day_of_week": 99}})["waiver_day"] is None
        assert waiver_rules({"settings": {"waiver_day_of_week": None}})["waiver_day"] is None


class TestRefusesTheVerdict:
    def test_no_add_mode_field_is_returned(self):
        """The field whose two previous implementations were both wrong."""
        for league in (VLBG, ROPEWAY, FAAB_LEAGUE):
            assert "add_mode" not in waiver_rules(league)

    def test_weekly_note_states_the_pool_is_locked(self):
        note = waiver_rules(ROPEWAY)["note"]
        assert "locked" in note
        assert "does not make a player an instant add" in note

    def test_daily_note_does_not_promise_instant(self):
        note = waiver_rules(VLBG)["note"]
        assert "may still be a claim" in note
        assert "1372" in note   # the undecoded bitmask is named, not hidden

    def test_how_to_confirm_names_the_app_and_the_api_gap(self):
        for league in (VLBG, ROPEWAY):
            text = waiver_rules(league)["how_to_confirm"]
            assert "league app" in text
            assert "not exposed by the API" in text


class TestRobustness:
    def test_missing_settings_do_not_raise(self):
        for league in (None, {}, {"settings": None}, {"settings": {}}):
            rules = waiver_rules(league)
            assert rules["waiver_type"] == "priority"
            assert "how_to_confirm" in rules
