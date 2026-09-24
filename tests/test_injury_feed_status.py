"""Injury feed outage guard, strict name matching, the shared status vocabulary,
inherited-absence detail and the prefetch crawl's database (fix/injury-feed-status)."""
import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from nfl_mcp import draft_tools, injury_service, injury_status, projections, ros
from nfl_mcp.database import NFLDatabase
from nfl_mcp.injury_match import build_injury_index, find_report
from nfl_mcp.injury_service import (
    InjuryAggregator,
    InjuryReport,
    crawl_injury_reports,
    prunable_teams,
    status_severity,
    worst_status,
)


def _report(pid, name, team, status="Out", position=None):
    return {"player_id": pid, "player_name": name, "team_id": team,
            "injury_status": status, "position": position, "sources": ["ESPN"]}


# 1 ---------------------------------------------------------------------------
class TestEmptyFeedIsNotARecovery:
    def test_many_empty_teams_prune_nothing(self):
        teams = {"BUF", "MIA", "NYJ", "NE", "KC"}
        assert prunable_teams(teams, {"BUF", "MIA", "NYJ", "NE"}, {}) == set()

    def test_one_team_losing_its_last_injury_is_pruned(self):
        assert prunable_teams({"BUF", "MIA"}, {"BUF"}, {"BUF": 1, "MIA": 7}) == {"BUF", "MIA"}

    def test_one_team_losing_many_open_reports_is_held(self):
        assert prunable_teams({"BUF", "MIA"}, {"BUF"}, {"BUF": 9}) == {"MIA"}

    @pytest.mark.asyncio
    async def test_espn_serving_empty_lists_keeps_every_injury(self):
        """The regression: every list page 200 with no items."""
        db = NFLDatabase()
        teams = ["BUF", "MIA", "NYJ", "NE", "KC"]
        db.upsert_injuries([_report(str(i), f"Player {i}", t, "IR")
                            for i, t in enumerate(teams)])

        async def _get(url, **_):
            r = MagicMock(status_code=200, headers={})
            r.json = MagicMock(return_value={"items": [], "pageCount": 1})
            return r
        client = MagicMock()
        client.get = AsyncMock(side_effect=_get)
        agg = InjuryAggregator(http_client=client, db=db, persist=False)
        injuries = await agg.fetch_all_injuries(teams, use_cache=False)
        assert injuries == [] and agg.empty_teams == set(teams)
        complete = prunable_teams(agg.complete_teams, agg.empty_teams,
                                  injury_service.open_report_counts(db))
        db.upsert_injuries(injuries, prune_missing=True, complete_teams=complete)
        assert {r["injury_status"] for r in db.get_all_current_injuries()} == {"IR"}

    def test_a_single_recovery_still_clears(self):
        db = NFLDatabase()
        db.upsert_injuries([_report("1", "Hurt Guy", "BUF", "Questionable"),
                            _report("2", "Other", "MIA", "Out")])
        complete = prunable_teams({"BUF", "MIA"}, {"BUF"}, injury_service.open_report_counts(db))
        db.upsert_injuries([_report("2", "Other", "MIA", "Out")], prune_missing=True,
                           complete_teams=complete)
        got = {r["player_id"]: r["injury_status"] for r in db.get_all_current_injuries()}
        assert got == {"1": "Active", "2": "Out"}


# 2 ---------------------------------------------------------------------------
def _athlete(name, team="BUF", position="WR", espn_id=None):
    raw = {"full_name": name}
    if espn_id:
        raw["espn_id"] = int(espn_id)
    return {"full_name": name, "team_id": team, "position": position, "raw": json.dumps(raw)}


class TestStrictNameMatching:
    @pytest.mark.parametrize("sleeper,espn", [
        ("Chris Smith", "Christian Smith"),
        ("Jay Tufele", "Jaylon Tufele"),
        ("Steve Jones", "Stephen Jones"),
        ("Stephen Jones", "Steven Jones"),
    ])
    def test_different_first_names_do_not_match(self, sleeper, espn):
        index = build_injury_index([_report("9", espn, "BUF")], team_names=lambda t: set())
        assert find_report(_athlete(sleeper), index, "BUF") is None

    def test_known_nicknames_still_match(self):
        index = build_injury_index([_report("9", "Robert Beal Jr.", "BUF")],
                                   team_names=lambda t: set())
        assert find_report(_athlete("Bobby Beal"), index, "BUF")["player_id"] == "9"

    def test_brother_with_the_exact_name_blocks_the_alias(self):
        """ESPN's "Chris Smith" is the teammate of that name, not Christopher."""
        index = build_injury_index([_report("9", "Chris Smith", "BUF")],
                                   team_names=lambda t: {"chris smith", "christopher smith"})
        assert find_report(_athlete("Christopher Smith"), index, "BUF") is None

    def test_espn_id_join_wins_and_refuses_a_stranger(self):
        index = build_injury_index([_report("100", "Chris Smith", "BUF"),
                                    _report("200", "Christopher Smyth", "BUF")],
                                   team_names=lambda t: set())
        me = _athlete("Christopher Smyth", espn_id="200")
        assert find_report(me, index, "BUF")["player_id"] == "200"
        # The alias candidate carries another athlete's id: refused.
        other = _athlete("Christopher Smith", espn_id="555")
        assert find_report(other, index, "BUF") is None

    def test_position_guard_fires_on_filled_positions(self):
        index = build_injury_index([_report("9", "Robert Beal", "BUF", position="LB")],
                                   team_names=lambda t: set())
        assert find_report(_athlete("Bobby Beal", position="WR"), index, "BUF") is None


class TestIngestFillsPositionAndGameStatus:
    def test_position_from_espn_id_then_name(self):
        db = MagicMock()
        db.get_athletes_by_espn_ids = MagicMock(return_value={"1": {"position": "QB"}})
        db.get_athletes_by_team = MagicMock(return_value=[
            {"full_name": "Name Only", "position": "TE"},
            {"full_name": "Twin Name", "position": "WR"}, {"full_name": "Twin Name", "position": "RB"},
        ])
        reports = [InjuryReport("1", "Anyone", "BUF"), InjuryReport("2", "Name Only", "BUF"),
                   InjuryReport("3", "Twin Name", "BUF")]
        InjuryAggregator(db=db)._fill_positions(reports)
        assert [r.position for r in reports] == ["QB", "TE", None]

    def test_espn_id_lookup_on_the_real_table(self):
        db = NFLDatabase()
        db.upsert_athletes({"77": {"player_id": "77", "full_name": "A B", "team": "BUF",
                                   "position": "RB", "espn_id": 4242}})
        assert db.get_athletes_by_espn_ids(["4242"])["4242"]["position"] == "RB"

    @pytest.mark.parametrize("details,expected", [
        ({"fantasyStatus": {"description": "PUP-R"}}, "PUP-R"),
        ({"fantasyStatus": {"description": "QUESTIONABLE"}}, "Questionable"),
        ({"fantasyStatus": {"description": "IR"}}, "IR"),
        ({}, None),
    ])
    def test_game_status_from_fantasy_status(self, details, expected):
        assert injury_service._game_status(details) == expected


# 3 ---------------------------------------------------------------------------
RAW_SPELLINGS = [
    # Sleeper short codes
    ("Sus", "Suspended", "out"), ("NA", "NA", "out"), ("DNR", "DNR", "out"),
    ("COV", "Reserve", "out"), ("IR", "IR", "out"), ("PUP", "PUP", "out"),
    ("NFI", "NFI", "out"), ("O", "Out", "out"), ("D", "Doubtful", "doubtful"),
    ("Q", "Questionable", "questionable"),
    # ESPN long forms
    ("Injured Reserve", "IR", "out"), ("PUP-R", "PUP", "out"), ("Reserve/PUP", "PUP", "out"),
    ("NFI-R", "NFI", "out"), ("Suspension", "Suspended", "out"),
    ("Day-To-Day", "Questionable", "questionable"), ("Out", "Out", "out"),
    ("Inactive", "Inactive", "out"), ("Reserve/COVID-19", "Reserve", "out"),
    ("Physically Unable To Perform", "PUP", "out"), ("injured", "Out", "out"),
    ("limited", "Questionable", "questionable"), ("Doubtful", "Doubtful", "doubtful"),
    ("Probable", "Probable", "healthy"), ("Active", "Active", "healthy"),
    ("Did Not Participate In Practice", "DNP", "questionable"), ("FP", "FP", "healthy"),
    ("Unknown", "Unknown", "uncertain"),
]


class TestOneVocabulary:
    @pytest.mark.parametrize("raw,canonical,kind", RAW_SPELLINGS)
    def test_every_consumer_agrees(self, raw, canonical, kind):
        assert injury_status.normalize(raw) == canonical
        assert projections.availability(raw) == kind
        info = injury_status.STATUS_TABLE[canonical]
        assert projections._injury_mult(raw) == info.multiplier
        assert status_severity(raw) == info.severity
        assert InjuryAggregator.get_severity(raw) == info.severity
        # The SQL severity map knows the spelling as the feeds send it.
        assert injury_service.STATUS_SEVERITY.get(raw) == info.severity

    def test_long_forms_are_severe(self):
        for raw in ("Injured Reserve", "PUP-R", "Reserve/PUP", "NFI-R"):
            assert status_severity(raw) == 5

    def test_raw_o_projects_at_zero(self):
        assert projections._injury_mult("O") == 0.0

    def test_draft_prices_every_absence(self):
        for raw in ("Sus", "Suspension", "NFI", "NA", "Inactive", "Injured Reserve"):
            assert draft_tools._injury_multiplier(raw) < 1.0, raw
        assert draft_tools._injury_multiplier("Injured Reserve") == draft_tools._injury_multiplier("IR")
        assert draft_tools._injury_multiplier("Questionable") == 1.0

    def test_probable_is_healthy_everywhere(self):
        from nfl_mcp.nfl_tools import _is_current_injury
        from nfl_mcp.tool_registry import _is_healthy_report
        assert not _is_current_injury("Probable", None)
        assert _is_healthy_report({"injury_status": "Probable"})
        assert projections.availability("Probable") == "healthy"

    def test_worst_status_normalizes_before_ranking(self):
        assert worst_status("Doubtful", "O") == "O"
        assert worst_status("Questionable", "Injured Reserve") == "Injured Reserve"

    def test_unrecognised_is_questionable(self):
        assert projections.availability("Brand New Code") == "unrecognised"
        assert status_severity("Brand New Code") == 2


# 4 ---------------------------------------------------------------------------
class TestInheritedAbsenceUsesTheReport:
    def test_season_ending_note_outlasts_one_week(self):
        entry = {"status": "Out", "description": "Tore his ACL, out for the season."}
        assert ros._starter_absence(entry) == ros.SEASON_ENDING_WEEKS
        assert ros._starter_absence("Out") == 1

    def test_reserve_placement_date_counts(self):
        today = date(2026, 10, 1)
        placed = {"status": "IR", "placed_on": "2026-09-10"}
        assert ros._starter_absence(placed, today) == ros.IR_MIN_WEEKS - 3

    def test_pup_list_under_an_out_status(self):
        assert ros._starter_absence({"status": "Out", "game_status": "PUP-R"}) == ros.IR_MIN_WEEKS

    def test_projection_carries_the_detail(self):
        def status_of(name, team):
            return "Out"
        status_of.detail = lambda name, team: {"description": "season-ending"}
        assert projections._absence_detail(status_of, "WR One", "BUF") == {
            "status": "Out", "description": "season-ending"}
        # A plain lookup (tests, callers without reports) still works.
        assert projections._absence_detail(lambda n, t: "Out", "X", "BUF") == {"status": "Out"}


class TestEspnRefsAreFollowedSafely:
    @pytest.mark.asyncio
    async def test_off_espn_detail_ref_is_not_fetched_and_team_not_pruned(self):
        async def _get(url, **_):
            r = MagicMock(status_code=200, headers={})
            r.json = MagicMock(return_value={"items": [{"$ref": "http://evil.example/inj/1"}],
                                             "pageCount": 1})
            return r
        client = MagicMock()
        client.get = AsyncMock(side_effect=_get)
        agg = InjuryAggregator(http_client=client)
        assert await agg.fetch_espn_injuries(["BUF"]) == []
        assert all("evil.example" not in c.args[0] for c in client.get.await_args_list)
        assert "BUF" not in agg.complete_teams


# 7 ---------------------------------------------------------------------------
class TestPrefetchCrawlReadsTheSharedDb:
    @pytest.mark.asyncio
    async def test_stored_name_and_position_without_writing(self, monkeypatch):
        db = MagicMock()
        db.get_player_injury_from_cache = MagicMock(return_value={"player_name": "Known Name"})
        db.get_athletes_by_espn_ids = MagicMock(return_value={"5": {"position": "RB"}})
        db.get_all_current_injuries = MagicMock(return_value=[])

        async def _espn(self, teams=None):
            self.complete_teams.add("BUF")
            return [InjuryReport("5", injury_service.UNKNOWN_NAME, "BUF", injury_status="Out")]
        monkeypatch.setattr(InjuryAggregator, "fetch_espn_injuries", _espn)
        monkeypatch.setattr("nfl_mcp.database.get_shared_db", lambda: db)
        rows, complete = await crawl_injury_reports(["BUF"])
        assert rows[0]["player_name"] == "Known Name"
        assert rows[0]["position"] == "RB"
        assert complete == {"BUF"}
        db.upsert_injuries.assert_not_called()
