"""Real practice reports: parsing, day attribution, storage and consumers.

The markup and blurbs below are trimmed copies of the live NFL.com report page
and ESPN injury notes fetched on 2026-09-23 (week 3, ATL@GB on Thursday).
"""
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nfl_mcp import practice_reports as pr
from nfl_mcp.database import NFLDatabase
from nfl_mcp.projections import _injury_mult, practice_adjusted_mult

NFL_COM_HTML = """
<h2 class="d3-o-section-title">THURSDAY, SEPTEMBER 24TH</h2>
<section class="nfl-o-injury-report__unit">
<div class="nfl-t-stats__title"><div class="d3-o-section-sub-title"><span>Falcons</span></div></div>
<table class="d3-o-table d3-o-table--detailed d3-o-reports--detailed"><thead><tr><th>Player</th>
<th>Position</th><th>Injuries</th><th>Practice Status</th><th>Game Status</th></tr></thead><tbody>
<tr><td><a href="/players/tua/"> Tua Tagovailoa </a></td><td>QB</td><td>Oblique</td>
<td>Full Participation in Practice</td><td></td></tr>
<tr><td><a> Michael Penix Jr. </a></td><td>QB</td><td>Knee</td>
<td>Limited Participation in Practice</td><td>Questionable</td></tr>
</tbody></table>
<div class="nfl-t-stats__title"><div class="d3-o-section-sub-title"><span>Packers</span></div></div>
<table class="d3-o-table d3-o-table--detailed d3-o-reports--detailed"><thead><tr><th>Player</th></tr></thead><tbody>
<tr><td><a> Anthony Campbell </a></td><td>LB</td><td>Ankle</td>
<td>Did Not Participate In Practice</td><td></td></tr>
<tr><td><a> Aaron Veteran </a></td><td>T</td><td>Not Injury Related - Rest</td>
<td>Did Not Participate In Practice</td><td></td></tr>
</tbody></table></section>
<h2 class="d3-o-section-title">SUNDAY, SEPTEMBER 27TH</h2>
<section><div class="d3-o-section-sub-title"><span>Cardinals</span></div>
<table class="d3-o-table d3-o-reports--detailed"><tbody>
<tr><td><a> Kyler Murray </a></td><td>QB</td><td>Foot</td>
<td>Did Not Participate In Practice</td><td></td></tr>
</tbody></table></section>
"""


def _et(y, m, d, h):
    """A UTC instant for an Eastern (EDT) wall-clock time."""
    return datetime(y, m, d, h + 4, 0, tzinfo=UTC)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield NFLDatabase(str(Path(tmp) / "t.db"))


class TestParsing:
    def test_nfl_com_rows(self):
        rows = pr.parse_nfl_com_report(NFL_COM_HTML, 2026)
        by_name = {r["player_name"]: r for r in rows}
        assert by_name["Tua Tagovailoa"]["team"] == "ATL"
        assert by_name["Tua Tagovailoa"]["practice_status"] == "FP"
        assert by_name["Michael Penix Jr."]["practice_status"] == "LP"
        assert by_name["Michael Penix Jr."]["game_status"] == "Questionable"
        assert by_name["Anthony Campbell"]["team"] == "GB"
        assert by_name["Anthony Campbell"]["practice_status"] == "DNP"
        # A rest day is not an injury DNP.
        assert by_name["Aaron Veteran"]["practice_status"] == "REST"
        assert by_name["Kyler Murray"]["team"] == "ARI"
        assert by_name["Kyler Murray"]["game_date"] == "2026-09-27"
        assert by_name["Tua Tagovailoa"]["game_date"] == "2026-09-24"

    @pytest.mark.parametrize("text,expected", [
        ("Taylor (ribs) was a full participant in Tuesday's practice, Tori McElhaney reports.",
         ("FP", "2026-09-22", False)),
        ("Jennings (hand) did not participate in Monday's estimated practice, Tom Silverstein reports.",
         ("DNP", "2026-09-21", True)),
        ("Cooper (shoulder) was listed as a limited participant on the Packers' estimated injury report Monday.",
         ("LP", "2026-09-21", True)),
        ("Richardson (groin) was a full participant at practice Thursday.",
         ("FP", "2026-09-17", False)),
    ])
    def test_espn_note_is_dated_by_the_named_day(self, text, expected):
        # Posted Tuesday 20:29 ET (00:29Z Wednesday) / Friday for the Thursday one.
        posted = "2026-09-23T00:29Z" if "Richardson" not in text else "2026-09-18T19:53Z"
        got = pr.parse_espn_practice_note(text, posted)
        assert (got["status"], got["date"], got["estimated"]) == expected

    @pytest.mark.parametrize("text", [
        "Darnold (glute) will practice Wednesday, according to Ian Rapoport.",
        "The Jets signed McClain from the practice squad to the active roster Tuesday.",
        "Johnson said he doesn't anticipate Williams practicing in Week 3.",
        "Hamilton tallied 13 tackles during the Ravens' loss on Sunday.",
    ])
    def test_non_reports_are_ignored(self, text):
        assert pr.parse_espn_practice_note(text, "2026-09-23T00:29Z") is None

    def test_espn_payload_respects_the_week_floor(self):
        payload = {"injuries": [{"displayName": "Atlanta Falcons", "injuries": [
            {"shortComment": "Muse (shoulder) was a full participant in Tuesday's practice.",
             "date": "2026-09-22T23:24Z", "athlete": {"displayName": "Nick Muse"}},
            {"shortComment": "Muse (shoulder) was limited at practice Thursday.",
             "date": "2026-09-17T23:24Z", "athlete": {"displayName": "Nick Muse"}},
        ]}]}
        rows = pr.espn_news_reports(payload, 2026, 3, since=date(2026, 9, 21))
        assert [(r["player_name"], r["team"], r["date"], r["status"]) for r in rows] == [
            ("Nick Muse", "ATL", "2026-09-22", "FP")
        ]


class TestReportDay:
    def test_before_cutoff_is_the_previous_day(self):
        assert pr.report_day(_et(2026, 9, 24, 10), date(2026, 9, 27)) == date(2026, 9, 23)

    def test_after_cutoff_is_today(self):
        assert pr.report_day(_et(2026, 9, 24, 17), date(2026, 9, 27)) == date(2026, 9, 24)

    def test_saturday_snapshot_is_clamped_onto_friday(self):
        assert pr.report_day(_et(2026, 9, 26, 12), date(2026, 9, 27)) == date(2026, 9, 25)

    def test_thursday_game_final_report_is_wednesday(self):
        assert pr.report_day(_et(2026, 9, 24, 9), date(2026, 9, 24)) == date(2026, 9, 23)

    def test_tuesday_snapshot_for_a_sunday_game_is_not_a_report(self):
        assert pr.report_day(_et(2026, 9, 22, 18), date(2026, 9, 27)) is None

    def test_unchanged_team_report_is_not_stored_as_a_new_day(self):
        rows = pr.parse_nfl_com_report(NFL_COM_HTML, 2026)
        now = _et(2026, 9, 23, 17)
        previous = {"ATL": {"tua tagovailoa": "FP", "michael penix": "LP"}}
        out = pr.nfl_com_reports(rows, 2026, 3, now=now, previous=previous)
        teams = {r["team"] for r in out}
        assert "ATL" not in teams
        assert {"GB", "ARI"} <= teams
        assert all(r["date"] == "2026-09-23" for r in out)
        assert all(r["source"] == "nfl.com" for r in out)


class TestSummarize:
    @pytest.mark.parametrize("statuses,pattern,trend", [
        (["DNP", "LP", "FP"], "DNP-LP-FP", "improving"),
        (["FP", "LP", "DNP"], "FP-LP-DNP", "worsening"),
        (["LP", "LP", "LP"], "LP-LP-LP", "steady"),
        (["LP", "DNP", "LP"], "LP-DNP-LP", "improving"),
        (["DNP"], "DNP", "single_report"),
    ])
    def test_pattern_and_trend(self, statuses, pattern, trend):
        rows = [{"date": f"2026-09-{23 + i}", "status": s, "source": "nfl.com"}
                for i, s in enumerate(statuses)]
        got = pr.summarize(rows)
        assert got["pattern"] == pattern
        assert got["trend"] == trend
        assert got["latest"] == statuses[-1]

    def test_nothing_reported_is_none(self):
        assert pr.summarize([]) is None
        assert pr.practice_fields(None)["practice_status"] is None


class TestStorage:
    def test_migration_is_v14_and_week_round_trips(self, db):
        assert NFLDatabase.CURRENT_SCHEMA_VERSION == 14
        rows = [
            {"player_name": "Michael Penix Jr.", "team": "ATL", "date": d, "status": s,
             "season": 2026, "week": 3, "source": "nfl.com"}
            for d, s in (("2026-09-21", "DNP"), ("2026-09-22", "LP"), ("2026-09-23", "FP"))
        ]
        assert db.upsert_practice_status(rows) == 3
        # Name suffix / punctuation and team alias are normalized.
        got = pr.lookup_practice(db, "Michael Penix", "ATL", season=2026, week=3)
        assert got["pattern"] == "DNP-LP-FP"
        assert got["source"] == "nfl.com"
        assert pr.lookup_practice(db, "Michael Penix", "ATL", season=2026, week=2) is None

    def test_news_note_does_not_overwrite_the_official_report(self, db):
        base = {"player_name": "Nick Muse", "team": "ATL", "date": "2026-09-22",
                "season": 2026, "week": 3}
        db.upsert_practice_status([{**base, "status": "LP", "source": "nfl.com"}])
        assert db.upsert_practice_status([{**base, "status": "FP", "source": "espn_news"}]) == 0
        rows = db.get_practice_reports("Nick Muse", "ATL", season=2026, week=3)
        assert [(r["status"], r["source"]) for r in rows] == [("LP", "nfl.com")]
        # The official report does replace an earlier note.
        db.upsert_practice_status([{**base, "date": "2026-09-23", "status": "FP", "source": "espn_news"}])
        db.upsert_practice_status([{**base, "date": "2026-09-23", "status": "DNP", "source": "nfl.com"}])
        rows = db.get_practice_reports("Nick Muse", "ATL", season=2026, week=3)
        assert [r["status"] for r in rows] == ["LP", "DNP"]

    def test_team_previous_day(self, db):
        db.upsert_practice_status([
            {"player_name": "A B", "team": "GB", "date": "2026-09-21", "status": "DNP",
             "season": 2026, "week": 3, "source": "nfl.com"},
            {"player_name": "A B", "team": "GB", "date": "2026-09-22", "status": "LP",
             "season": 2026, "week": 3, "source": "nfl.com"},
        ])
        assert db.get_team_practice_days(2026, 3, before="2026-09-23", source="nfl.com") == {
            "GB": {"a b": "LP"}
        }

    def test_the_old_invented_rows_are_dropped_by_the_migration(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            NFLDatabase(path).close()
            with sqlite3.connect(path) as conn:
                conn.execute("DELETE FROM schema_version WHERE version = 14")
                conn.execute("DROP TABLE player_practice_status")
                conn.execute("CREATE TABLE player_practice_status (player_id TEXT NOT NULL, "
                             "date TEXT NOT NULL, status TEXT NOT NULL, source TEXT, "
                             "updated_at TEXT NOT NULL, PRIMARY KEY(player_id, date))")
                conn.execute("INSERT INTO player_practice_status VALUES "
                             "('4426348','2026-09-20','DNP','espn_injuries','2026-09-20')")
                conn.commit()
            migrated = NFLDatabase(path)
            with sqlite3.connect(path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM player_practice_status").fetchone()[0] == 0
                cols = {r[1] for r in conn.execute("PRAGMA table_info(player_practice_status)")}
            assert {"name_key", "team", "source_rank", "game_status", "week"} <= cols
            migrated.close()


def _analyzer():
    analyzer = MagicMock()
    analyzer.fetch_defense_rankings = AsyncMock(return_value={})
    analyzer.get_matchup_difficulty = MagicMock(return_value={"rank": 16, "matchup_tier": "neutral"})
    return analyzer


class TestConsumers:
    def test_projection_prices_the_practice_line_only_on_a_questionable_tag(self):
        assert practice_adjusted_mult("Questionable", "DNP") < _injury_mult("Questionable")
        assert practice_adjusted_mult("Questionable", "FP") > _injury_mult("Questionable")
        assert practice_adjusted_mult("Questionable", "limited") == _injury_mult("Questionable")
        assert practice_adjusted_mult("Questionable", None) == _injury_mult("Questionable")
        assert practice_adjusted_mult(None, "DNP") == 1.0
        assert practice_adjusted_mult("Out", "FP") == 0.0

    async def test_start_sit_reads_the_stored_report(self, db):
        from nfl_mcp.lineup_optimizer_tools import LineupOptimizer

        db.upsert_practice_status([
            {"player_name": "Test Receiver", "team": "KC", "date": d, "status": s,
             "season": 2026, "week": 3, "source": "nfl.com"}
            for d, s in (("2026-09-23", "FP"), ("2026-09-24", "LP"), ("2026-09-25", "DNP"))
        ])
        optimizer = LineupOptimizer(db=db, defense_analyzer=_analyzer(), auto_project=False)
        analysis = await optimizer.analyze_player(
            player_name="Test Receiver", player_id="", position="WR", team="KC",
            opponent="LV", injury_data={"status": "Questionable"},
            projection_data={"projected_points": 12.0}, season=2026, week=3,
        )
        assert analysis.practice_status == "DNP"
        assert analysis.practice_pattern == "FP-LP-DNP"
        assert analysis.practice_trend == "worsening"
        assert analysis.practice_source == "nfl.com"
        assert any("FP-LP-DNP" in r for r in analysis.reasoning)

    async def test_start_sit_without_a_report_has_no_practice(self, db):
        from nfl_mcp.lineup_optimizer_tools import LineupOptimizer

        optimizer = LineupOptimizer(db=db, defense_analyzer=_analyzer(), auto_project=False)
        analysis = await optimizer.analyze_player(
            player_name="Nobody Reported", player_id="", position="WR", team="KC",
            opponent="LV", projection_data={"projected_points": 12.0}, season=2026, week=3,
        )
        assert analysis.practice_status is None
        assert analysis.practice_source is None

    async def test_injury_report_attaches_the_week_line(self, db):
        from nfl_mcp import tool_registry

        db.upsert_practice_status([
            {"player_name": "Tua Tagovailoa", "team": "ATL", "date": "2026-09-22",
             "status": "FP", "season": 2026, "week": 3, "source": "nfl.com"},
        ])
        injuries = [
            {"player_id": "1", "player_name": "Tua Tagovailoa", "team_id": "ATL",
             "injury_status": "Questionable"},
            {"player_id": "2", "player_name": "Somebody Else", "team_id": "ATL",
             "injury_status": "Out"},
        ]
        with patch.object(tool_registry, "get_db", return_value=db), \
             patch("nfl_mcp.injury_service.get_injury_reports", AsyncMock(return_value=injuries)), \
             patch.object(tool_registry, "_current_season_week", AsyncMock(return_value=(2026, 3))), \
             patch("nfl_mcp.practice_reports.refresh_practice_reports", AsyncMock(return_value=0)):
            out = await tool_registry.get_injury_report(team_ids=["ATL"])
        by_name = {i["player_name"]: i for i in out["injuries"]}
        assert by_name["Tua Tagovailoa"]["practice_status"] == "FP"
        assert by_name["Tua Tagovailoa"]["practice_source"] == "nfl.com"
        assert by_name["Somebody Else"]["practice_status"] is None
        assert by_name["Somebody Else"]["practice_source"] is None
        assert out["practice_week"]["players_with_report"] == 1

    async def test_fetch_combines_nfl_com_and_espn_notes(self):
        payload = {"injuries": [{"displayName": "Green Bay Packers", "injuries": [
            {"shortComment": "Jennings (hand) did not participate in Monday's estimated practice.",
             "date": "2026-09-22T01:00Z", "athlete": {"displayName": "Donovan Jennings"}},
        ]}]}

        def _resp(url, **_):
            r = MagicMock(status_code=200)
            r.text = NFL_COM_HTML
            r.json = MagicMock(return_value=payload)
            return r

        client = MagicMock()
        client.get = AsyncMock(side_effect=_resp)
        rows = await pr.fetch_practice_reports(2026, 3, client=client, now=_et(2026, 9, 23, 17))
        sources = {r["source"] for r in rows}
        assert sources == {"nfl.com", "espn_news"}
        jennings = next(r for r in rows if r["player_name"] == "Donovan Jennings")
        assert (jennings["date"], jennings["status"], jennings["estimated"]) == ("2026-09-21", "DNP", True)


class TestWeekFloor:
    @pytest.mark.parametrize("today,monday_ok,expected", [
        (date(2026, 9, 23), False, date(2026, 9, 22)),   # Wed -> Tue
        (date(2026, 9, 23), True, date(2026, 9, 21)),    # Wed -> Mon (estimated reports)
        (date(2026, 9, 27), False, date(2026, 9, 22)),   # Sun -> Tue
        (date(2026, 9, 28), False, date(2026, 9, 22)),   # Mon -> last Tue (MNF week)
        (date(2026, 9, 22), False, date(2026, 9, 22)),   # Tue -> today
    ])
    def test_practice_week_start(self, today, monday_ok, expected):
        assert pr.practice_week_start(today, include_monday=monday_ok) == expected
