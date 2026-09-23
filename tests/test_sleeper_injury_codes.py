"""Sleeper's short injury codes have to be recognised.

`_injury_mult` and `INJURY_STATUS_SCORES` knew out/ir/pup/suspended. The player
feed actually sends `Sus`, `NA`, `DNR` and `COV` — 110 players in the live cache
carried one of those four, and every one of them projected at full points and
was never auto-benched. A suspended player was a recommended start.
"""
import pytest

from nfl_mcp.injury_service import status_severity, worst_status
from nfl_mcp.lineup_optimizer_tools import INJURY_STATUS_SCORES
from nfl_mcp.projections import _injury_mult

pytestmark = pytest.mark.usefixtures("offline_sources")  # no ambient network reads

# Verified against the live athlete cache on 2026-09-21:
#   NA  96 players (almost all unrostered, active=False)
#   Sus 10 players (injury_body_part literally "Suspension")
#   DNR  2 players (one an ACL case)
#   COV  2 players (COVID list)
SLEEPER_SHORT_CODES = ["Sus", "NA", "DNR", "COV"]


class TestUnavailableCodes:
    @pytest.mark.parametrize("code", SLEEPER_SHORT_CODES)
    def test_multiplier_is_zero(self, code):
        assert _injury_mult(code) == 0.0

    @pytest.mark.parametrize("code", SLEEPER_SHORT_CODES)
    def test_case_insensitive(self, code):
        assert _injury_mult(code.lower()) == 0.0
        assert _injury_mult(code.upper()) == 0.0

    @pytest.mark.parametrize("code", SLEEPER_SHORT_CODES)
    def test_health_score_is_zero(self, code):
        assert INJURY_STATUS_SCORES[code.lower()] == 0

    @pytest.mark.parametrize("code", SLEEPER_SHORT_CODES)
    def test_severity_is_severe(self, code):
        assert status_severity(code) == 5

    @pytest.mark.parametrize("code", SLEEPER_SHORT_CODES)
    def test_beats_a_milder_reading_from_the_other_feed(self, code):
        assert worst_status("Questionable", code) == code


class TestKnownStatusesUnchanged:
    @pytest.mark.parametrize("status,expected", [
        ("Out", 0.0), ("IR", 0.0), ("PUP", 0.0), ("NFI", 0.0),
        ("injured reserve", 0.0), ("suspended", 0.0),
        ("Doubtful", 0.35), ("Questionable", 0.9),
        ("Active", 1.0), ("Probable", 1.0), (None, 1.0), ("", 1.0),
    ])
    def test_multiplier(self, status, expected):
        assert _injury_mult(status) == expected

    def test_whitespace_is_tolerated(self):
        assert _injury_mult("  Out  ") == 0.0


class TestUnknownStatusFailsSafe:
    def test_an_unknown_code_is_not_treated_as_healthy(self):
        """Sleeper only fills injury_status when something is wrong.

        Defaulting an unrecognised value to 1.0 is exactly what let `Sus` and
        `NA` project at full points, so a new upstream code now degrades to the
        questionable haircut rather than to "fine".
        """
        assert _injury_mult("SomeFutureCode") == 0.9

    def test_it_is_logged_so_the_gap_is_visible(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="nfl_mcp.projections"):
            _injury_mult("SomeFutureCode")
        assert any("unrecognised injury status" in r.message for r in caplog.records)

    def test_an_empty_status_is_not_logged_as_a_problem(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="nfl_mcp.projections"):
            _injury_mult(None)
            _injury_mult("")
        assert not caplog.records


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_a_suspended_player_projects_zero(self):
        from nfl_mcp.projections import project_players

        res = await project_players(
            [{"name": "Banned Guy", "position": "WR", "team": "MIN",
              "opponent": "CHI", "injury": {"status": "Sus"}}],
            scoring="0.5",
        )
        assert res["projections"][0]["projected_points"] == 0.0

    @pytest.mark.asyncio
    async def test_a_suspended_player_is_a_must_sit(self):
        from unittest.mock import AsyncMock

        from nfl_mcp import lineup_optimizer_tools as lo
        from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer

        analyzer = DefenseRankingsAnalyzer(db=None)
        analyzer.fetch_defense_rankings = AsyncMock(return_value={})
        opt = lo.LineupOptimizer(db=None, defense_analyzer=analyzer, auto_project=False)

        analysis = await opt.analyze_player(
            "Banned Guy", "", "WR", "MIN", "CHI",
            injury_data={"status": "Sus"},
            projection_data={"projected_points": 15.0},
        )
        assert analysis.decision == "must_sit"
