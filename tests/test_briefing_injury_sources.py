"""The briefing must consult both injury feeds, not just Sleeper's.

`_injury_status` reads Sleeper's player list, which around kickoff routinely
lags the ESPN report sitting in `player_injuries`. Taking the milder of the two
is what put a doubtful tight end into a recommended lineup on 2026-09-20.
"""
import json

import pytest

from nfl_mcp.briefing_tools import build_injury_index, resolve_injury
from nfl_mcp.injury_service import status_severity, worst_status

pytestmark = pytest.mark.usefixtures("offline_sources")  # no ambient network reads


def _athlete(name, injury_status=None, team="LV"):
    raw = {"injury_status": injury_status} if injury_status else {}
    return {"full_name": name, "team_id": team, "position": "TE",
            "raw": json.dumps(raw)}


def _report(name, status, team="LV", itype="Knee"):
    return {"player_name": name, "team_id": team, "injury_status": status,
            "injury_type": itype}


class TestWorstStatus:
    def test_picks_the_more_severe(self):
        assert worst_status("Questionable", "Doubtful") == "Doubtful"
        assert worst_status("Doubtful", "Questionable") == "Doubtful"
        assert worst_status("Out", "Questionable") == "Out"
        assert worst_status("IR", "Out") == "IR"

    def test_ignores_empty_sources(self):
        assert worst_status(None, "Doubtful") == "Doubtful"
        assert worst_status("Doubtful", None) == "Doubtful"
        assert worst_status(None, None) is None
        assert worst_status() is None

    def test_unknown_status_is_treated_as_questionable(self):
        # Not silently best-cased: an unrecognised designation must still beat
        # a minor one, or a feed change would quietly downgrade everyone. It
        # ranks with Questionable (projections price it at 0.9), not above.
        assert status_severity("Some New Designation") == 2
        assert worst_status("Probable", "Some New Designation") == "Some New Designation"
        assert worst_status("Unknown", "Questionable") == "Questionable"
        assert worst_status("Questionable", "Unknown") == "Questionable"
        assert status_severity("Reserve") == 5


class TestBuildInjuryIndex:
    def test_keys_on_normalized_name_and_canonical_team(self):
        index = build_injury_index([_report("Brock Bowers Jr.", "Out", team="LV")])
        assert ("brock bowers", "LV") in index

    def test_normalizes_team_variants(self):
        index = build_injury_index([_report("Jayden Daniels", "Out", team="WAS")])
        assert ("jayden daniels", "WSH") in index

    def test_skips_rows_without_a_name(self):
        assert build_injury_index([_report(None, "Out")]) == {}


class TestResolveInjury:
    def test_report_overrides_a_milder_sleeper_status(self):
        """The exact 2026-09-20 Bowers case."""
        index = build_injury_index([_report("Brock Bowers", "Doubtful")])
        got = resolve_injury(_athlete("Brock Bowers", "Questionable"), index, "LV")

        assert got["status"] == "Doubtful"
        assert got["source"] == "both"
        assert got["sleeper_status"] == "Questionable"
        assert got["report_status"] == "Doubtful"

    def test_sleeper_wins_when_it_is_the_more_severe(self):
        index = build_injury_index([_report("Brock Bowers", "Questionable")])
        got = resolve_injury(_athlete("Brock Bowers", "Out"), index, "LV")
        assert got["status"] == "Out"

    def test_report_alone_is_enough(self):
        """Sleeper says nothing; the ESPN report must still reach the lineup."""
        index = build_injury_index([_report("Brock Bowers", "Out")])
        got = resolve_injury(_athlete("Brock Bowers", None), index, "LV")
        assert got["status"] == "Out"
        assert got["source"] == "report"

    def test_report_active_does_not_override_a_real_sleeper_designation(self):
        """`Active` in the report means "no injury", not "cleared to play"."""
        index = build_injury_index([_report("Ladd McConkey", "Active", team="LAC")])
        got = resolve_injury(
            _athlete("Ladd McConkey", "Questionable", team="LAC"), index, "LAC"
        )
        assert got["status"] == "Questionable"
        assert got["source"] == "sleeper"

    def test_healthy_player_yields_nothing(self):
        index = build_injury_index([_report("Ladd McConkey", "Active", team="LAC")])
        assert resolve_injury(_athlete("Ladd McConkey", None, team="LAC"), index, "LAC") is None

    def test_a_player_missing_from_the_report_still_uses_sleeper(self):
        got = resolve_injury(_athlete("Nobody Known", "Out"), {}, "LV")
        assert got["status"] == "Out"
        assert got["source"] == "sleeper"

    def test_wrong_team_does_not_match(self):
        """Name collisions across teams must not import someone else's injury."""
        index = build_injury_index([_report("Kenneth Walker", "Out", team="SEA")])
        got = resolve_injury(_athlete("Kenneth Walker", None, team="KC"), index, "KC")
        assert got is None


class TestBuildPlayerUsesIt:
    def test_the_more_severe_status_reaches_the_projection_input(self):
        from nfl_mcp.briefing_tools import _build_player

        athletes = {"11604": {**_athlete("Brock Bowers", "Questionable"), "position": "TE"}}
        index = build_injury_index([_report("Brock Bowers", "Out")])
        player = _build_player("11604", athletes, {"LV": "LAC"}, {}, {}, index)

        assert player["injury"] == {"status": "Out"}
        assert player["injury_detail"]["sleeper_status"] == "Questionable"

    def test_without_an_index_it_falls_back_to_sleeper(self):
        from nfl_mcp.briefing_tools import _build_player

        athletes = {"11604": {**_athlete("Brock Bowers", "Questionable"), "position": "TE"}}
        player = _build_player("11604", athletes, {"LV": "LAC"}, {}, {})
        assert player["injury"] == {"status": "Questionable"}

    @pytest.mark.asyncio
    async def test_out_status_zeroes_the_projection(self):
        """End to end: the worse status has to change the number, not just a field."""
        from nfl_mcp.projections import project_players

        res = await project_players(
            [{"name": "X", "position": "TE", "team": "LV", "opponent": "LAC",
              "injury": {"status": "Out"}}],
            scoring="0.5",
        )
        assert res["projections"][0]["projected_points"] == 0.0
