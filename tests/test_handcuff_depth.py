"""Handcuff lookup against the shape `get_depth_chart` actually returns."""
import pytest

from nfl_mcp.handcuff_tools import backs_up, handcuff_from_depth

# Verbatim from a live get_depth_chart("BUF") response.
BUF = [
    {"position": "QB", "players": ["Josh Allen", "Kyle Allen"]},
    {"position": "RB", "players": ["James Cook III", "Ray Davis", "Ty Johnson"]},
    {"position": "WR", "players": ["DJ Moore", "Joshua Palmer"]},
]


class TestCurrentShape:
    def test_starter_gets_the_next_man_up(self):
        # `position` is a POSITION LABEL, never a player name. Matching the
        # starter against it could not succeed, so every lookup used to fall
        # through to "you_roster_a_backup" and report no handcuff at all.
        assert handcuff_from_depth(BUF, "James Cook III") == ("Ray Davis", "depth")

    def test_a_backup_has_no_handcuff(self):
        # The RB2 *is* the contingent value. The RB3 behind him inherits
        # nothing while the starter plays, so he is nobody's handcuff.
        assert handcuff_from_depth(BUF, "Ray Davis") == (None, "you_roster_a_backup")
        assert backs_up(BUF, "Ray Davis") == "James Cook III"

    def test_a_starter_backs_up_nobody(self):
        assert backs_up(BUF, "James Cook III") is None
        assert backs_up(BUF, "Saquon Barkley") is None

    def test_last_man_on_the_chart_is_the_contingent_value(self):
        assert handcuff_from_depth(BUF, "Ty Johnson") == (None, "you_roster_a_backup")

    def test_lone_starter_reports_no_backup(self):
        chart = [{"position": "TE", "players": ["Dalton Kincaid"]}]
        assert handcuff_from_depth(chart, "Dalton Kincaid") == (None, "no_backup_listed")

    def test_player_on_another_team_is_not_placed(self):
        assert handcuff_from_depth(BUF, "Saquon Barkley") == (None, "not_on_depth_chart")

    def test_injury_tags_are_stripped_on_both_sides(self):
        chart = [{"position": "RB", "players": ["Christian McCaffreyQ", "Isaac GuerendoO"]}]
        assert handcuff_from_depth(chart, "Christian McCaffrey") == ("Isaac Guerendo", "depth")

    def test_placeholder_entries_are_skipped(self):
        chart = [{"position": "RB", "players": ["James Cook III", "-", "Ray Davis"]}]
        assert handcuff_from_depth(chart, "James Cook III") == ("Ray Davis", "depth")


class TestEmptyInput:
    @pytest.mark.parametrize("chart", [None, [], [{}]])
    def test_no_chart_is_handled(self, chart):
        assert handcuff_from_depth(chart, "Anyone") == (None, "not_on_depth_chart")
