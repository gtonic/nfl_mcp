"""Injury and practice data correctness: validation of real practice rows,
complete-crawl pruning, name handling, default healthy filtering, severity
defaults, nickname matching, ESPN note parsing, short-week practice lookup,
stored Sleeper statuses for inactives and shared severity labels."""
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nfl_mcp import gameday_inactives as gi
from nfl_mcp import practice_reports as pr
from nfl_mcp import tool_registry
from nfl_mcp.database import NFLDatabase
from nfl_mcp.injury_match import build_injury_index, find_report
from nfl_mcp.injury_service import InjuryAggregator, InjuryReport, worst_status
from nfl_mcp.nfl_tools import _severity_label
from nfl_mcp.response_validation import validate_practice_report_response


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield NFLDatabase(str(Path(tmp) / "t.db"))


def _inj(pid, name, team, status="Out", **extra):
    return {"player_id": pid, "player_name": name, "team_id": team,
            "injury_status": status, "sources": ["ESPN"], **extra}


def _statuses(db):
    return {r["player_name"]: r["injury_status"] for r in db.get_all_current_injuries()}


# 1 ---------------------------------------------------------------------------
class TestPracticeValidation:
    def test_name_and_team_rows_are_valid(self):
        rows = [{"player_name": "Tua Tagovailoa", "team": "ATL", "date": "2026-09-23",
                 "status": "FP", "source": "nfl.com"},
                {"player_name": "A B", "team": "KC", "date": "2026-09-23", "status": "REST"}]
        result = validate_practice_report_response(rows)
        assert result.is_valid()
        assert not result.warnings

    def test_a_row_without_identity_or_status_is_invalid(self):
        assert not validate_practice_report_response([{"player_name": "A B", "status": "FP"}]).is_valid()
        assert not validate_practice_report_response([{"player_name": "A B", "team": "KC"}]).is_valid()


# 2 ---------------------------------------------------------------------------
def _page(refs, page_count=1):
    r = MagicMock(status_code=200, headers={})
    r.json.return_value = {"pageCount": page_count, "items": [{"$ref": u} for u in refs]}
    return r


class TestCrawlCompleteness:
    @pytest.fixture(autouse=True)
    def _clean(self):
        InjuryAggregator.clear_caches()
        yield
        InjuryAggregator.clear_caches()

    async def _crawl(self, responses, detail):
        client = MagicMock()
        client.get = AsyncMock(side_effect=responses)
        agg = InjuryAggregator(http_client=client)
        with patch.object(agg, "_fetch_espn_injury_detail", AsyncMock(side_effect=detail)):
            got = await agg._fetch_team_espn_injuries("BUF", {})
        return agg, got

    @pytest.mark.asyncio
    async def test_fully_resolved_team_is_complete(self):
        rep = InjuryReport(player_id="1", player_name="A", team_id="")
        agg, got = await self._crawl([_page(["u1"])], [rep])
        assert len(got) == 1 and agg.complete_teams == {"BUF"}

    @pytest.mark.asyncio
    async def test_failed_detail_makes_the_team_partial(self):
        rep = InjuryReport(player_id="1", player_name="A", team_id="")
        agg, got = await self._crawl([_page(["u1", "u2"])], [rep, None])
        assert len(got) == 1 and agg.complete_teams == set()

    @pytest.mark.asyncio
    async def test_failed_second_page_makes_the_team_partial(self):
        rep = InjuryReport(player_id="1", player_name="A", team_id="")
        agg, _ = await self._crawl([_page(["u1"], page_count=2), MagicMock(status_code=500)], [rep])
        assert agg.complete_teams == set()

    @pytest.mark.asyncio
    async def test_team_with_no_reports_is_complete(self):
        agg, got = await self._crawl([_page([])], [])
        assert got == [] and agg.complete_teams == {"BUF"}

    def test_only_complete_teams_are_pruned(self, db):
        db.upsert_injuries([_inj("1", "A", "CLE"), _inj("2", "B", "CLE"),
                            _inj("3", "C", "KC"), _inj("4", "D", "NYJ")])
        # CLE crawled partially (B missing), KC completely, NYJ completely with
        # no reports left (absent from the batch).
        db.upsert_injuries([_inj("1", "A", "CLE"), _inj("3", "C", "KC")],
                           prune_missing=True, complete_teams={"KC", "NYJ"})
        assert _statuses(db) == {"A": "Out", "B": "Out", "C": "Out", "D": "Active"}

    def test_empty_batch_still_prunes_complete_teams(self, db):
        db.upsert_injuries([_inj("4", "D", "NYJ")])
        db.upsert_injuries([], prune_missing=True, complete_teams={"NYJ"})
        assert _statuses(db) == {"D": "Active"}


# 3 ---------------------------------------------------------------------------
class TestAthleteNames:
    @pytest.mark.asyncio
    async def test_failed_name_fetch_is_not_cached(self):
        InjuryAggregator.clear_caches()
        detail = MagicMock(status_code=200)
        detail.json.return_value = {"athlete": {"$ref": "https://x/athletes/77?lang=en"},
                                    "status": "Out"}
        client = MagicMock()
        client.get = AsyncMock(side_effect=[detail, MagicMock(status_code=503)])
        agg = InjuryAggregator(http_client=client)
        try:
            rep = await agg._fetch_espn_injury_detail("https://x/inj/1", {})
            assert rep.player_name == "Unknown"
            assert "77" not in InjuryAggregator._athlete_name_cache
        finally:
            InjuryAggregator.clear_caches()

    def test_unknown_name_does_not_overwrite_the_stored_one(self, db):
        db.upsert_injuries([_inj("1", "Josh Allen", "BUF", "Questionable")])
        db.upsert_injuries([_inj("1", "Unknown", "BUF", "Out")])
        assert _statuses(db) == {"Josh Allen": "Out"}
        db.upsert_injuries([_inj("1", "Joshua Allen", "BUF", "Out")])
        assert _statuses(db) == {"Joshua Allen": "Out"}


# 4 ---------------------------------------------------------------------------
REPORT_ROWS = [
    {"player_name": "Hurt", "team_id": "KC", "injury_status": "Out", "severity": 4},
    {"player_name": "Fine", "team_id": "KC", "injury_status": "Active", "severity": 1},
    {"player_name": "Q", "team_id": "KC", "injury_status": "Questionable", "severity": 2},
    {"player_name": "Nostatus", "team_id": "KC", "injury_status": "Unknown", "severity": 1},
]


class TestInjuryReportDefaults:
    async def _run(self, **kw):
        with patch("nfl_mcp.injury_service.get_injury_reports",
                   AsyncMock(return_value=[dict(r) for r in REPORT_ROWS])):
            return await tool_registry.get_injury_report(include_practice=False, **kw)

    @pytest.mark.asyncio
    async def test_healthy_rows_are_excluded_by_default(self):
        out = await self._run()
        assert [r["player_name"] for r in out["injuries"]] == ["Hurt", "Q", "Nostatus"]
        assert out["healthy_excluded"] == 1

    @pytest.mark.asyncio
    async def test_include_healthy_returns_everything(self):
        out = await self._run(include_healthy=True)
        assert len(out["injuries"]) == 4 and out["healthy_excluded"] == 0


# 5 ---------------------------------------------------------------------------
def test_unknown_never_outranks_questionable_and_reserve_is_out():
    from nfl_mcp.projections import availability
    assert worst_status("Unknown", "Questionable") == "Questionable"
    assert worst_status("Reserve", "Questionable") == "Reserve"
    assert availability("Reserve") == "out"
    assert availability("Unknown") == "uncertain"


# 6 ---------------------------------------------------------------------------
class TestNicknameMatching:
    def _index(self, *rows):
        return build_injury_index([_inj(str(i), n, t) for i, (n, t) in enumerate(rows)])

    @pytest.mark.parametrize("espn,sleeper", [
        ("Zachary Carter", "Zach Carter"),
        ("Robert Beal Jr.", "Rob Beal"),
        ("Robert Hunt", "Bobby Hunt"),
        ("Michael Pittman Jr.", "Mike Pittman"),
        ("Anthony Brown", "Tony Brown"),
    ])
    def test_short_and_formal_first_names_match(self, espn, sleeper):
        index = self._index((espn, "CIN"))
        assert find_report({"full_name": sleeper}, index, "CIN")["player_name"] == espn

    def test_other_team_or_other_first_name_does_not_match(self):
        index = self._index(("Zachary Carter", "CIN"), ("Jordan Smith", "KC"))
        assert find_report({"full_name": "Zach Carter"}, index, "NYJ") is None
        assert find_report({"full_name": "Jalen Smith"}, index, "KC") is None

    def test_ambiguous_fallback_is_refused(self):
        index = self._index(("Deonte Smith", "KC"), ("Deontay Smith", "KC"))
        assert find_report({"full_name": "Deon Smith"}, index, "KC") is None

    def test_position_contradiction_is_refused(self):
        index = build_injury_index([_inj("1", "Zachary Carter", "CIN", position="DT")])
        assert find_report({"full_name": "Zach Carter", "position": "WR"}, index, "CIN") is None
        assert find_report({"full_name": "Zach Carter", "position": "DT"}, index, "CIN") is not None

    def test_exact_match_still_wins(self):
        index = self._index(("Zach Carter", "CIN"), ("Zachary Carter", "CIN"))
        assert find_report({"full_name": "Zach Carter"}, index, "CIN")["player_name"] == "Zach Carter"


# 7 / 8 -----------------------------------------------------------------------
POSTED_THU = "2026-09-24T22:00Z"  # Thursday 18:00 ET


class TestEspnNotes:
    def test_two_days_pick_the_latest_days_phrase(self):
        note = ("Chase (hip) was a full participant in Thursday's practice after "
                "he was limited Wednesday.")
        assert pr.parse_espn_practice_note(note, POSTED_THU) == {
            "status": "FP", "date": "2026-09-24", "estimated": False}

    def test_earlier_day_named_first(self):
        note = "Chase (hip) was limited Wednesday but practiced fully Thursday."
        assert pr.parse_espn_practice_note(note, POSTED_THU)["status"] == "FP"

    def test_two_statuses_for_one_day_are_ambiguous(self):
        note = "Chase (hip) was limited in Thursday's practice and did not practice Thursday afternoon."
        assert pr.parse_espn_practice_note(note, POSTED_THU) is None

    def test_forward_wording_does_not_hide_a_past_report(self):
        note = ("Kelce (ankle) was a full participant in Wednesday's practice and "
                "will be ready for Sunday.")
        got = pr.parse_espn_practice_note(note, POSTED_THU)
        assert got["status"] == "FP" and got["date"] == "2026-09-23"

    def test_pure_forward_wording_is_not_a_report(self):
        assert pr.parse_espn_practice_note(
            "Kelce (ankle) is expected to be a limited participant in Friday's practice.",
            POSTED_THU) is None
        assert pr.parse_espn_practice_note(
            "Kelce (ankle) will practice Friday.", POSTED_THU) is None


# 9 ---------------------------------------------------------------------------
@pytest.mark.parametrize("injury,expected", [
    ("Not Injury Related - Illness", "DNP"),
    ("Illness", "DNP"),
    ("Not Injury Related - Rest", "REST"),
    ("Not Injury Related - Personal Matter", "REST"),
    ("Rest", "REST"),
])
def test_only_rest_or_personal_is_rest(injury, expected):
    assert pr.normalize_practice("Did Not Participate In Practice", injury) == expected


# 10 --------------------------------------------------------------------------
class TestShortWeekLookup:
    def test_week_lookup_reads_next_week_and_keeps_the_latest_practice_week(self, db):
        db.upsert_practice_status([
            # Last week's report, stored under week 3.
            {"player_name": "Thu Guy", "team": "NYG", "date": "2026-09-17", "status": "LP",
             "season": 2026, "week": 3, "source": "nfl.com"},
            # This short week: Monday ESPN note under week 3, NFL.com under week 4.
            {"player_name": "Thu Guy", "team": "NYG", "date": "2026-09-21", "status": "DNP",
             "season": 2026, "week": 3, "source": "espn_news"},
            {"player_name": "Thu Guy", "team": "NYG", "date": "2026-09-22", "status": "LP",
             "season": 2026, "week": 4, "source": "nfl.com"},
        ])
        got = pr.lookup_practice(db, "Thu Guy", "NYG", season=2026, week=3)
        assert got["pattern"] == "DNP-LP"
        assert got["latest_date"] == "2026-09-22"

    def test_default_window_includes_this_weeks_monday(self, db):
        today = pr.to_eastern(datetime.now(UTC)).date()
        monday = pr.practice_week_start(today, include_monday=True)
        db.upsert_practice_status([
            {"player_name": "Thu Guy", "team": "NYG", "date": (monday - timedelta(days=3)).isoformat(),
             "status": "FP", "source": "nfl.com"},
            {"player_name": "Thu Guy", "team": "NYG", "date": monday.isoformat(),
             "status": "DNP", "estimated": True, "source": "nfl.com"},
        ])
        rows = db.get_practice_reports("Thu Guy", "NYG")
        assert [(r["date"], r["status"]) for r in rows] == [(monday.isoformat(), "DNP")]

    def test_latest_practice_week_helper(self):
        rows = [{"date": "2026-09-18", "status": "FP"}, {"date": "2026-09-21", "status": "DNP"},
                {"date": "2026-09-22", "status": "LP"}]
        assert [r["date"] for r in pr.latest_practice_week(rows)] == ["2026-09-21", "2026-09-22"]
        assert pr.latest_practice_week([]) == []


# 11 --------------------------------------------------------------------------
KICKOFF = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)


class TestInactivesFromStoredAthletes:
    @pytest.fixture(autouse=True)
    def _reset(self):
        gi._sleeper_dump = None
        yield
        gi._sleeper_dump = None

    def _client(self, sleeper_payload=None):
        async def _get(url, **_):
            r = MagicMock(status_code=200)
            r.json = MagicMock(return_value=sleeper_payload if "sleeper" in url else {})
            return r
        client = MagicMock()
        client.get = AsyncMock(side_effect=_get)
        return client

    def _sleeper_calls(self, client):
        return [c for c in client.get.await_args_list if "sleeper" in c.args[0]]

    @pytest.mark.asyncio
    async def test_stored_athletes_are_read_instead_of_the_dump(self):
        db = MagicMock()
        db.get_week_kickoffs = MagicMock(return_value={"JAX": KICKOFF.isoformat()})
        db.get_athletes_by_team = MagicMock(return_value=[
            {"id": "1", "team_id": "JAX", "raw": json.dumps(
                {"full_name": "A B", "team": "JAX", "injury_status": "Inactive"})},
            {"id": "2", "team_id": "JAX", "raw": json.dumps(
                {"full_name": "C D", "team": "JAX", "injury_status": None})},
        ])
        client = self._client()
        out = await gi.get_official_inactives(db, 2026, 2, now=KICKOFF - timedelta(minutes=60),
                                              client=client)
        assert [r["player_name"] for r in out["inactives"]] == ["A B"]
        assert gi.SOURCE_SLEEPER in out["sources_checked"]
        assert self._sleeper_calls(client) == []

    @pytest.mark.asyncio
    async def test_dump_fallback_is_fetched_once_a_day(self):
        db = MagicMock()
        db.get_week_kickoffs = MagicMock(return_value={"JAX": KICKOFF.isoformat()})
        db.get_athletes_by_team = MagicMock(return_value=[])
        payload = {"1": {"full_name": "A B", "team": "JAX", "injury_status": "Inactive"}}
        client = self._client(payload)
        now = KICKOFF - timedelta(minutes=60)
        for minutes in (0, 30):
            out = await gi.get_official_inactives(db, 2026, 2, now=now + timedelta(minutes=minutes),
                                                  client=client)
            assert [r["player_name"] for r in out["inactives"]] == ["A B"]
        assert len(self._sleeper_calls(client)) == 1


# 12 --------------------------------------------------------------------------
@pytest.mark.parametrize("status,label", [
    ("Out", "High"), ("Injured Reserve", "High"), ("Suspension", "High"),
    ("Suspended", "High"), ("PUP", "High"), ("Physically Unable to Perform", "High"),
    ("NFI", "High"), ("Reserve/COVID-19", "High"), ("Doubtful", "Medium"),
    ("Questionable", "Medium"), ("Day-To-Day", "Medium"), ("Probable", "Low"),
    ("Active", "Unknown"), (None, "Unknown"),
])
def test_severity_labels_use_the_shared_vocabulary(status, label):
    assert _severity_label(status) == label
