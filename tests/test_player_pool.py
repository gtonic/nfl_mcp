"""Free-agent pools hold players who play: one kicker per team, no ghost rows.

GB listed Trey Smack (depth 1) and Lenny Krieg (no depth spot, international
pathway) as kickers; get_waiver_targets offered both at the same 9.5.
"""
import json
import time

from nfl_mcp.player_pool import is_ghost, playing_options, starters_by_team
from nfl_mcp.streaming_tools import _unit_availability

_NOW_MS = time.time() * 1000
_RECENT = _NOW_MS - 3 * 86400 * 1000
_YEARS_AGO = _NOW_MS - 4 * 365 * 86400 * 1000


def _row(pid, name, position, team, depth=None, news=_RECENT, **raw):
    payload = {"depth_chart_order": depth, "news_updated": news, "team": team,
               "position": position, "active": True, **raw}
    return {"id": pid, "full_name": name, "position": position, "team_id": team,
            "status": "Active", "raw": json.dumps(payload)}


SMACK = _row("13545", "Trey Smack", "K", "GB", depth=1)
KRIEG = _row("12548", "Lenny Krieg", "K", "GB", depth=None)
FITZ = _row("1", "Ryan Fitzgerald", "K", "CAR", depth=1)
KESSMAN = _row("2", "Alex Kessman", "K", "CAR", depth=None, news=_YEARS_AGO)
NO_DEPTH_TEAM_K = _row("3", "Only Kicker", "K", "NYG", depth=None)


class TestKickers:
    def test_only_the_depth_chart_starter_is_an_option(self):
        kept = playing_options([SMACK, KRIEG, FITZ, KESSMAN])
        assert [r["full_name"] for r in kept] == ["Trey Smack", "Ryan Fitzgerald"]

    def test_backup_is_no_option_even_when_the_starter_is_rostered(self):
        # The pool excludes rostered players; the reference still names him.
        kept = playing_options([KRIEG], reference=[SMACK, KRIEG])
        assert kept == []

    def test_team_without_any_depth_data_keeps_its_kicker(self):
        assert playing_options([NO_DEPTH_TEAM_K]) == [NO_DEPTH_TEAM_K]

    def test_starters_by_team(self):
        assert starters_by_team([SMACK, KRIEG, FITZ], "K") == {"GB": {"13545"}, "CAR": {"1"}}


class TestGhostRows:
    def test_retired_player_with_a_team(self):
        ben = _row("9", "Ben Roethlisberger", "QB", "PIT", depth=None, news=_YEARS_AGO)
        assert is_ghost(ben)
        assert playing_options([ben]) == []

    def test_not_active_in_sleeper(self):
        assert is_ghost(_row("9", "X", "WR", "KC", depth=2, active=False))

    def test_practice_squad_body_with_recent_news_stays(self):
        # No depth spot but current news: a real, if unlikely, option.
        assert not is_ghost(_row("9", "Jaydon Blue", "RB", "PHI", depth=None))

    def test_depth_chart_player_is_never_a_ghost(self):
        assert not is_ghost(_row("9", "Vet", "TE", "KC", depth=1, news=_YEARS_AGO))

    def test_defense_and_non_sleeper_rows_are_left_alone(self):
        assert not is_ghost({"id": "GB", "position": "DEF", "team_id": "GB",
                             "raw": json.dumps({"active": False})})
        assert not is_ghost({"id": "1", "position": "WR", "team_id": "KC",
                             "raw": json.dumps({"full_name": "Stub"})})


class TestStreamingAvailability:
    def test_a_free_backup_kicker_is_not_a_streamer(self):
        class _DB:
            def get_athletes_by_team(self, team):
                return [SMACK, KRIEG]

        avail = _unit_availability("K", "GB", rostered={"13545"}, db=_DB())
        assert [p["name"] for p in avail["players"]] == ["Trey Smack"]
        assert avail["has_free_agent"] is False
