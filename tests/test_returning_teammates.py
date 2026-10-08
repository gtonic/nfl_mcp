"""A teammate who missed games and is back takes his inflated weeks out of the
backup's base (weekly), and out of ROS from his expected return."""
import pytest

from nfl_mcp import opportunity_tools, projections, ros
from nfl_mcp.projections import _depth_map, _returning_teammates
from tests.test_projection_ros_volume import _engine
from tests.test_ros_projections import SETTINGS, FakeDB, _player, _stub_projection
from tests.test_vacated_volume import _games, _index


def _backfield(lead_weeks=(1, 2), lead_rank=10, backup_with=6.0, backup_without=18.0,
               lead_games=True):
    values = {"list": [
        {"name": "Lead Back", "position": "RB", "team": "PIT", "position_rank": lead_rank,
         "value": 3000},
        {"name": "Backup Back", "position": "RB", "team": "PIT", "position_rank": 30,
         "value": 1500},
    ]}
    lead = _games(list(lead_weeks), carries=15.0) if lead_games else []
    backup = [g for w in (1, 2, 3, 4)
              for g in _games([w], carries=backup_with if w in lead_weeks else backup_without)]
    index = _index([
        {"player_id": "1", "name": "Lead Back", "position": "RB", "team": "PIT", "games": lead},
        {"player_id": "2", "name": "Backup Back", "position": "RB", "team": "PIT",
         "games": backup},
    ])
    return values, index


def _status(lead=None, description=None):
    def _get(name, team):
        return lead if name == "Lead Back" else None
    _get.detail = lambda name, team: (
        {"status": lead, "description": description} if name == "Lead Back" else {})
    return _get


def _project(values, index, status_of, name="Backup Back", week=5):
    return _engine(values)._project_one(
        {"name": name, "position": "RB", "team": "PIT", "opponent": "CLE"},
        values, {}, {}, index, week, 1.0, _depth_map(values), status_of)


class TestWeeklyDeflation:
    def test_a_teammate_back_this_week_takes_his_weeks_out(self):
        values, index = _backfield()
        got = _project(values, index, _status(None))
        bd = got["breakdown"]
        assert bd["returning_teammates"] == [{
            "name": "Lead Back", "status": None, "missed_weeks": [3, 4],
            "games_until_return": 0, "expected_return_week": 5}]
        # Half the rate is from the two games with him, half from all four.
        with_him = opportunity_tools.opportunity_base_for(
            index, "Backup Back", "RB", 5, exclude_weeks={3, 4})
        assert bd["deflated_base_ppg"] == round(with_him, 1) and bd["deflated_games"] == 2
        prior = projections.base_ppg("RB", 30)
        full = ros.regressed_rate(bd["base_ppg"], prior, 4)
        kept = ros.regressed_rate(bd["deflated_base_ppg"], prior, 2)
        w = projections.RETURNING_KEEP_WEIGHT
        assert bd["regressed_base_ppg"] == pytest.approx(w * full + (1 - w) * kept, abs=0.01)
        assert bd["deflated_volume"]["carries"]["with_teammate"] == pytest.approx(6.0)
        assert bd["deflated_volume"]["carries"]["trailing"] > 6.0

        plain = _project(values, index, _status("Out"))  # still out: no deflation now
        assert got["projected_points"] < plain["projected_points"]
        assert plain["breakdown"]["regressed_base_ppg"] == pytest.approx(full, abs=0.01)

    def test_questionable_with_a_full_practice_is_back(self):
        values, index = _backfield()
        status = _status("Questionable")
        status.practice = lambda name, team: "FP"
        got = _project(values, index, status)
        assert got["breakdown"]["returning_teammates"][0]["games_until_return"] == 0

    def test_questionable_without_practice_is_due_next_week(self):
        values, index = _backfield()
        status = _status("Questionable")
        status.practice = lambda name, team: "DNP"
        bd = _project(values, index, status)["breakdown"]
        assert bd["returning_teammates"][0]["games_until_return"] == 1
        assert bd["deflated_base_ppg"] < bd["base_ppg"]

    def test_still_out_keeps_the_base_and_does_not_add_vacated_volume(self):
        values, index = _backfield()
        bd = _project(values, index, _status("IR"))["breakdown"]
        # His four games already *are* the starter's role: no share of the
        # lead's volume on top of them.
        assert bd["starters_out_ahead"] == ["Lead Back"]
        assert bd["vacated_volume"] == {}
        assert bd["base_ppg"] == round(opportunity_tools.opportunity_base_for(
            index, "Backup Back", "RB", 5), 1)
        due = bd["returning_teammates"][0]
        assert due["games_until_return"] == ros.IR_MIN_WEEKS
        assert due["expected_return_week"] == 5 + ros.IR_MIN_WEEKS
        assert bd["deflated_games"] == 2
        assert bd["deflated_base_ppg"] < bd["base_ppg"]

    def test_designated_to_return_shortens_the_wait(self):
        values, index = _backfield()
        status = _status("IR", "Lead Back was designated to return from injured reserve")
        due = _project(values, index, status)["breakdown"]["returning_teammates"][0]
        assert due["games_until_return"] == projections.DESIGNATED_RETURN_GAMES

    def test_a_fresh_absence_is_still_priced_as_vacated_volume(self):
        # He played the backup's latest game: out *now* is new, not in the base.
        values, index = _backfield(lead_weeks=(1, 2, 3, 4))
        bd = _project(values, index, _status("Out"))["breakdown"]
        assert bd["returning_teammates"] == []
        assert bd["vacated_volume"].get("carries", 0) > 0

    def test_season_ending_is_not_returning(self):
        values, index = _backfield()
        bd = _project(values, index, _status("IR", "season-ending knee surgery"))["breakdown"]
        assert bd["returning_teammates"] == [] and bd["deflated_volume"] == {}

    def test_no_lift_when_his_weeks_without_him_were_not_bigger(self):
        values, index = _backfield(backup_with=18.0, backup_without=6.0)
        bd = _project(values, index, _status(None))["breakdown"]
        assert bd["returning_teammates"] == []
        assert bd["base_ppg"] == round(opportunity_tools.opportunity_base_for(
            index, "Backup Back", "RB", 5), 1)

    def test_a_reranked_starter_still_counts_when_he_shared_the_role(self):
        # The market has the backup ahead now; the volume split says otherwise.
        values, index = _backfield(lead_rank=42)
        bd = _project(values, index, _status(None))["breakdown"]
        assert [r["name"] for r in bd["returning_teammates"]] == ["Lead Back"]

    def test_a_teammate_who_never_played_and_has_no_report_is_ignored(self):
        values, index = _backfield(lead_games=False, backup_with=30.0, backup_without=30.0)
        assert _project(values, index, _status(None))["breakdown"]["returning_teammates"] == []
        # On a report (PUP), he is a starter coming back; with no game
        # together the rate after his return is the rank prior.
        bd = _project(values, index, _status("PUP"))["breakdown"]
        assert bd["returning_teammates"] and bd["deflated_games"] == 0
        assert bd["deflated_base_ppg"] == projections.base_ppg("RB", 30)

    def test_snaps_tell_a_quiet_game_from_an_absence(self):
        values, index = _backfield()
        found = _returning_teammates(
            _depth_map(values), "PIT", "RB", 30, 1500, _status(None), index, "Backup Back", 5,
            played_weeks=lambda pid: {3, 4})
        assert found == []

    def test_wr1_back_deflates_the_tight_end(self):
        values = {"list": [
            {"name": "Star WR", "position": "WR", "team": "MIN", "position_rank": 5,
             "value": 5000},
            {"name": "Pass TE", "position": "TE", "team": "MIN", "position_rank": 15,
             "value": 400},
        ]}
        index = _index([
            {"player_id": "1", "name": "Star WR", "position": "WR", "team": "MIN",
             "games": _games([1, 2, 3], targets=10.0)},
            {"player_id": "2", "name": "Pass TE", "position": "TE", "team": "MIN",
             "games": _games([1, 2, 3], targets=4.0) + _games([4], targets=12.0)},
        ])
        got = _engine(values)._project_one(
            {"name": "Pass TE", "position": "TE", "team": "MIN", "opponent": "NO"},
            values, {}, {}, index, 5, 1.0, _depth_map(values), lambda n, t: "Questionable")
        bd = got["breakdown"]
        assert [r["name"] for r in bd["returning_teammates"]] == ["Star WR"]
        assert bd["deflated_volume"]["targets"]["with_teammate"] == pytest.approx(4.0)


class TestRosDeflation:
    async def _run(self, monkeypatch, games_until_return):
        bd = {"base_ppg": 14.0, "base_source": "opportunity", "usage_games": 6,
              "position_rank": None, "usage_mult": 1.0, "deflated_base_ppg": 8.0,
              "deflated_games": 2,
              "returning_teammates": [{"name": "Lead Back", "games_until_return":
                                       games_until_return,
                                       "expected_return_week": 3 + games_until_return}]}
        _stub_projection(monkeypatch, {"Backup Back": 14.0}, bd)
        out = await ros.ros_projections([_player("Backup Back", position="RB")], season=2026,
                                        week=3, settings=SETTINGS, db=FakeDB())
        return out["players"][0]

    @pytest.mark.asyncio
    async def test_inflated_until_the_return_then_deflated(self, monkeypatch):
        p = await self._run(monkeypatch, 2)
        prior = projections.base_ppg("RB", None)
        before = ros.regressed_rate(14.0, prior, 6)
        w = projections.RETURNING_KEEP_WEIGHT
        after = w * before + (1 - w) * ros.regressed_rate(8.0, prior, 2)
        assert p["weekly_points"][3] == 14.0          # this week: the weekly projection
        assert p["weekly_points"][4] == pytest.approx(before, abs=0.01)
        assert p["weekly_points"][5] == pytest.approx(after, abs=0.01)
        assert p["weekly_points"][9] == pytest.approx(after, abs=0.01)
        assert p["per_game"] == pytest.approx(after, abs=0.01)
        assert p["per_game_until_return"] == pytest.approx(before, abs=0.01)
        assert p["returning_teammates"] == [{"name": "Lead Back", "expected_return_week": 5,
                                             "games_until_return": 2, "status": None}]

    @pytest.mark.asyncio
    async def test_back_this_week_deflates_every_later_week(self, monkeypatch):
        p = await self._run(monkeypatch, 0)
        assert p["weekly_points"][4] == pytest.approx(p["per_game"], abs=0.01)
        assert p["per_game"] < p["per_game_until_return"]

    @pytest.mark.asyncio
    async def test_no_teammate_due_back_keeps_the_rate(self, monkeypatch):
        _stub_projection(monkeypatch, {"Backup Back": 14.0},
                         {"base_ppg": 14.0, "base_source": "opportunity", "usage_games": 6,
                          "position_rank": None, "usage_mult": 1.0,
                          "returning_teammates": [], "deflated_volume": {}})
        out = await ros.ros_projections([_player("Backup Back", position="RB")], season=2026,
                                        week=3, settings=SETTINGS, db=FakeDB())
        p = out["players"][0]
        assert "returning_teammates" not in p
        assert p["weekly_points"][9] == pytest.approx(
            ros.regressed_rate(14.0, projections.base_ppg("RB", None), 6), abs=0.01)
