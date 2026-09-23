"""Injury reports and rosters live in different id spaces.

`player_injuries` and `injury_history` are keyed by ESPN athlete ids (practice
reports by name and team); rosters carry Sleeper ids. Looking one up with the other
found nothing for the player asked about — the briefing reported no injury
moves on a roster where Jayden Daniels and Brock Bowers had just been
downgraded — and, for 12 accidental collisions, a stranger's injury. These
tests pin the join through (name, team), the Sleeper fallback, and the clearing
of reports a complete crawl no longer lists.
"""
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from nfl_mcp.database import NFLDatabase
from nfl_mcp.injury_match import build_injury_index, report_ids_for
from nfl_mcp.injury_service import STATUS_SEVERITY
from nfl_mcp.sleeper_enrichment import _enrich_usage_and_opponent


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield NFLDatabase(str(Path(tmp) / "t.db"))


def _report(player_id, name, team, status, **extra):
    return {"player_id": player_id, "player_name": name, "team_id": team,
            "position": "QB", "injury_status": status, "sources": ["ESPN"], **extra}


def _athlete(pid, name, team, sleeper_status=None, position="QB"):
    return {"id": pid, "player_id": pid, "full_name": name, "team_id": team,
            "position": position, "raw": json.dumps({"injury_status": sleeper_status})}


class TestFindPlayerInjury:
    def test_matches_by_name_and_team(self, db):
        db.upsert_injuries([_report("4426348", "Jayden Daniels", "WSH", "Doubtful")])
        got = db.find_player_injury("Jayden Daniels", "WSH", max_age_hours=24)
        assert got["injury_status"] == "Doubtful"
        assert got["player_id"] == "4426348"

    def test_a_colliding_id_does_not_leak_a_strangers_injury(self, db):
        # ESPN 8439 is Aaron Rodgers; Sleeper 8439 is Demetris Robertson.
        db.upsert_injuries([_report("8439", "Aaron Rodgers", "PIT", "Out")])
        assert db.find_player_injury("Demetris Robertson", "PIT", max_age_hours=24) is None
        assert db.get_player_injury_from_cache("8439", max_age_hours=24)["player_name"] == "Aaron Rodgers"

    def test_team_aliases_and_name_suffixes_are_normalized(self, db):
        db.upsert_injuries([_report("1", "Brock Bowers Jr.", "LV", "Out")])
        assert db.find_player_injury("Brock Bowers", "OAK", max_age_hours=24)["injury_status"] == "Out"

    def test_same_name_on_another_team_is_not_a_match(self, db):
        db.upsert_injuries([_report("1", "Josh Allen", "JAX", "Out")])
        assert db.find_player_injury("Josh Allen", "BUF", max_age_hours=24) is None


class TestEnrichment:
    def _db(self, report=None, practice=None):
        mock = Mock()
        mock.find_player_injury = Mock(return_value=report)
        mock.get_latest_practice_status = Mock(return_value=practice)
        mock.get_usage_last_n_weeks = Mock(return_value=None)
        return mock

    def test_sleeper_status_is_used_when_the_report_is_missing(self):
        out = _enrich_usage_and_opponent(
            self._db(), _athlete("11604", "Brock Bowers", "LV", "Out", "TE"), 2026, 3
        )
        assert out["injury_status"] == "Out"
        assert out["injury_sources"] == ["Sleeper"]
        # No report was published: the designation is not a practice line.
        assert out["practice_status"] is None

    def test_worse_of_the_two_sources_wins(self):
        report = {"player_id": "4426348", "injury_status": "Questionable",
                  "updated_at": datetime.now(UTC).isoformat(), "sources": ["ESPN"]}
        out = _enrich_usage_and_opponent(
            self._db(report), _athlete("11566", "Jayden Daniels", "WSH", "Out"), 2026, 3
        )
        assert out["injury_status"] == "Out"
        assert out["injury_sources"] == ["ESPN", "Sleeper"]

    def test_report_active_does_not_hide_a_sleeper_designation(self):
        report = {"player_id": "1", "injury_status": "Active",
                  "updated_at": datetime.now(UTC).isoformat()}
        out = _enrich_usage_and_opponent(
            self._db(report), _athlete("2", "X Y", "KC", "Doubtful"), 2026, 3
        )
        assert out["injury_status"] == "Doubtful"

    def test_practice_is_looked_up_by_name_and_team_for_the_week(self):
        report = {"player_id": "4426348", "injury_status": "Doubtful",
                  "updated_at": datetime.now(UTC).isoformat()}
        mock = self._db(report)
        mock.get_practice_reports = Mock(return_value=[])
        _enrich_usage_and_opponent(mock, _athlete("11566", "Jayden Daniels", "WSH"), 2026, 3)
        mock.get_practice_reports.assert_called_once_with("Jayden Daniels", "WSH", season=2026, week=3)
        mock.get_latest_practice_status.assert_not_called()

    @pytest.mark.parametrize("status", ["IR", "Sus", "NA", "DNR", "COV", "Out", "Questionable"])
    def test_a_designation_is_never_turned_into_a_practice_line(self, status):
        out = _enrich_usage_and_opponent(self._db(), _athlete("1", "X Y", "KC", status), 2026, 3)
        assert out["practice_status"] is None
        assert out["practice_source"] == "unreported"

    def test_an_unreadable_designation_is_not_full_practice(self):
        report = {"player_id": "1", "injury_status": "Day-To-Day",
                  "updated_at": datetime.now(UTC).isoformat()}
        out = _enrich_usage_and_opponent(self._db(report), _athlete("2", "X Y", "KC"), 2026, 3)
        assert out["practice_status"] is None

    def test_healthy_player_without_a_report_is_unreported_not_full(self):
        out = _enrich_usage_and_opponent(self._db(), _athlete("1", "X Y", "KC"), 2026, 3)
        assert out["practice_status"] is None
        assert out["practice_status_source"] == "unreported"


class TestInjuryHistoryJoin:
    def test_roster_ids_translate_to_report_ids(self):
        index = build_injury_index([
            _report("4426348", "Jayden Daniels", "WSH", "Doubtful"),
            _report("4432665", "Brock Bowers", "LV", "Out"),
        ])
        rows = [
            {"full_name": "Jayden Daniels", "team_id": "WSH"},
            {"full_name": "Brock Bowers", "team_id": "LV"},
            {"full_name": "Healthy Guy", "team_id": "KC"},
        ]
        assert report_ids_for(rows, index) == ["4426348", "4432665"]

    def test_changes_are_found_through_the_translated_ids(self, db):
        db.upsert_injuries([_report("4426348", "Jayden Daniels", "WSH", "Active")])
        db.upsert_injuries([_report("4426348", "Jayden Daniels", "WSH", "Doubtful")])
        since = (datetime.now(UTC) - timedelta(days=7)).isoformat()

        assert db.get_injury_status_changes(since=since, player_ids=["11566"]) == []
        ids = report_ids_for([{"full_name": "Jayden Daniels", "team_id": "WSH"}],
                             build_injury_index(db.get_all_current_injuries()))
        changes = db.get_injury_status_changes(since=since, player_ids=ids)
        assert [c["injury_status"] for c in changes] == ["Doubtful", "Active"]


class TestPruneMissing:
    def _statuses(self, db):
        return {r["player_name"]: r["injury_status"] for r in db.get_all_current_injuries()}

    def test_a_player_dropped_from_his_teams_report_is_cleared(self, db):
        db.upsert_injuries([_report("1", "Cedric Tillman", "CLE", "Questionable"),
                            _report("2", "Other Brown", "CLE", "Out")])
        db.upsert_injuries([_report("2", "Other Brown", "CLE", "Out")], prune_missing=True)
        assert self._statuses(db) == {"Cedric Tillman": "Active", "Other Brown": "Out"}

    def test_the_clearing_is_recorded_as_a_recovery(self, db):
        db.upsert_injuries([_report("1", "Cedric Tillman", "CLE", "Questionable"),
                            _report("2", "Other Brown", "CLE", "Out")])
        db.upsert_injuries([_report("2", "Other Brown", "CLE", "Out")], prune_missing=True)
        since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        better = db.get_injury_status_changes(
            since=since, player_ids=["1"], direction="better",
            severity_map={k: int(v) for k, v in STATUS_SEVERITY.items()},
        )
        assert [(c["previous_status"], c["injury_status"]) for c in better] == [("Questionable", "Active")]

    def test_teams_missing_from_the_crawl_keep_their_reports(self, db):
        # A team whose fetch failed contributes no rows; that is not a recovery.
        db.upsert_injuries([_report("1", "A", "CLE", "Out"), _report("2", "B", "KC", "Out")])
        db.upsert_injuries([_report("2", "B", "KC", "Out")], prune_missing=True)
        assert self._statuses(db) == {"A": "Out", "B": "Out"}

    def test_without_the_flag_nothing_is_cleared(self, db):
        db.upsert_injuries([_report("1", "A", "CLE", "Out"), _report("2", "B", "CLE", "Out")])
        db.upsert_injuries([_report("2", "B", "CLE", "Out")])
        assert self._statuses(db) == {"A": "Out", "B": "Out"}

    def test_already_active_rows_are_not_rewritten(self, db):
        db.upsert_injuries([_report("1", "A", "CLE", "Active"), _report("2", "B", "CLE", "Out")])
        db.upsert_injuries([_report("2", "B", "CLE", "Out")], prune_missing=True)
        since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        assert len(db.get_injury_status_changes(since=since, player_ids=["1"])) == 1


class TestWorstStatusTiebreak:
    @pytest.mark.parametrize("order", [("Doubtful", "Out"), ("Out", "Doubtful")])
    def test_out_beats_doubtful_whatever_the_argument_order(self, order):
        from nfl_mcp.injury_service import worst_status
        assert worst_status(*order) == "Out"

    def test_severity_still_dominates_the_tiebreak(self):
        from nfl_mcp.injury_service import worst_status
        assert worst_status("Out", "IR") == "IR"
        assert worst_status("Questionable", "Doubtful") == "Doubtful"


class TestStatusMoves:
    def test_only_real_moves_survive(self):
        from nfl_mcp.briefing_tools import _status_moves
        rows = [
            {"player_name": "Daniels", "previous_status": "Active", "injury_status": "Doubtful"},
            {"player_name": "Egbuka", "previous_status": "Active", "injury_status": "Active"},
            {"player_name": "Gibbs", "previous_status": None, "injury_status": "Active"},
            {"player_name": "Mason", "previous_status": None, "injury_status": "Questionable"},
            {"player_name": "Burrow", "previous_status": "Questionable", "injury_status": "Active"},
        ]
        assert [r["player_name"] for r in _status_moves(rows)] == ["Daniels", "Mason", "Burrow"]
