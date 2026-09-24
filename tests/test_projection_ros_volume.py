"""Inherited volume, ROS absence windows, K/DEF ROS pricing and small
season/scoring fixes (fix/projection-ros-volume)."""
import types
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nfl_mcp import cbs_fantasy_tools, opportunity_tools, projections, ros, usage_trends
from nfl_mcp import week_context as wc
from nfl_mcp.opportunity_tools import VACATED_VOLUME_SHARE, trailing_volume
from nfl_mcp.projections import ProjectionEngine, _depth_map, _inherited_shares
from tests.test_ros_projections import SETTINGS, TEAMS, FakeDB, _player, _stub_projection
from tests.test_vacated_volume import _games, _index

WRS = ["WR One", "WR Two", "WR Three", "WR Four", "WR Five"]


def _engine(values):
    engine = ProjectionEngine.__new__(ProjectionEngine)
    engine.db = None
    by_name = {e["name"]: e for e in values["list"]}
    engine.values = types.SimpleNamespace(
        lookup=lambda _idx, player_id=None, name=None, position=None: by_name.get(name))
    engine.defense = types.SimpleNamespace(
        get_matchup_difficulty=lambda *a, **k: {"matchup_tier": "neutral"})
    engine.vegas = types.SimpleNamespace(get_game_lines=lambda *a, **k: {"is_fallback": True})
    return engine


def _receivers(team="BUF"):
    values = {"list": [{"name": n, "position": "WR", "team": team, "position_rank": 10 * (i + 1)}
                       for i, n in enumerate(WRS)]}
    index = _index([{"player_id": str(i), "name": n, "position": "WR", "team": team,
                     "games": _games([1, 2], targets=10 if i == 0 else 4)}
                    for i, n in enumerate(WRS)])
    return values, index


def _out(name):
    return lambda n, t: "Out" if n == name else None


class TestInheritedVolumeIsShared:
    def test_teammates_together_inherit_at_most_the_share(self):
        values, index = _receivers()
        engine, status = _engine(values), _out("WR One")
        vacated = trailing_volume(index, "WR One", week=3)["targets"]
        got = [engine._project_one({"name": n, "position": "WR", "team": "BUF", "opponent": "MIA"},
                                   values, {}, {}, index, 3, 1.0, _depth_map(values), status)
               for n in WRS[1:]]
        inherited = [g["breakdown"]["vacated_volume"].get("targets", 0.0) for g in got]
        assert sum(inherited) <= VACATED_VOLUME_SHARE * vacated + 1e-6
        # Next man up takes the most; the fifth receiver gets nothing.
        assert inherited[0] > inherited[1] > inherited[2] > 0
        assert inherited[3] == 0.0

    def test_shares_skip_another_out_teammate(self):
        values, _ = _receivers()
        depth = _depth_map(values)
        status = lambda n, t: "Out" if n in ("WR One", "WR Two") else None  # noqa: E731
        # WR3 is the next available receiver behind both of them.
        three = _inherited_shares(depth, "BUF", "WR", 30, ["WR One", "WR Two"], status)
        four = _inherited_shares(depth, "BUF", "WR", 40, ["WR One", "WR Two"], status)
        assert three["WR One"] > four["WR One"]
        assert sum(_inherited_shares(depth, "BUF", "WR", r, ["WR One"], status).get("WR One", 0)
                   for r in (30, 40, 50)) == pytest.approx(VACATED_VOLUME_SHARE)

    def test_sleeper_team_codes_reach_the_depth_map(self):
        """WAS is Sleeper's spelling; the depth map is keyed WSH."""
        values, index = _receivers(team="WSH")
        got = _engine(values)._project_one(
            {"name": "WR Two", "position": "WR", "team": "WAS", "opponent": "DAL"},
            values, {}, {}, index, 3, 1.0, _depth_map(values), _out("WR One"))
        assert got["breakdown"]["vacated_volume"].get("targets", 0) > 0
        assert got["team"] == "WAS"

    def test_breakdown_keeps_his_own_base(self):
        values, index = _receivers()
        got = _engine(values)._project_one(
            {"name": "WR Two", "position": "WR", "team": "BUF", "opponent": "MIA"},
            values, {}, {}, index, 3, 1.0, _depth_map(values), _out("WR One"))
        bd = got["breakdown"]
        assert bd["own_base_ppg"] < bd["base_ppg"]
        assert bd["inherited_from"] == {"WR One": {"status": "Out"}}


class TestRosInheritedVolume:
    async def _run(self, monkeypatch, starter_status, db=None):
        bd = {"base_ppg": 14.0, "own_base_ppg": 10.0, "base_source": "opportunity",
              "usage_games": 6, "position_rank": None, "usage_mult": 1.0,
              "inherited_from": {"WR One": starter_status}}
        _stub_projection(monkeypatch, {"WR Two": 14.0}, bd)
        out = await ros.ros_projections([_player("WR Two")], season=2026, week=3,
                                        settings=SETTINGS, db=db or FakeDB())
        return out["players"][0]

    @pytest.mark.asyncio
    async def test_a_one_week_absence_lifts_only_this_week(self, monkeypatch):
        p = await self._run(monkeypatch, "Out")
        own = ros.regressed_rate(10.0, projections.base_ppg("WR", None), 6)
        assert p["weekly_points"][3] == 14.0
        assert p["weekly_points"][4] == pytest.approx(own, abs=0.01)
        assert p["per_game"] == pytest.approx(own, abs=0.01)

    @pytest.mark.asyncio
    async def test_an_ir_absence_lifts_the_next_games_then_stops(self, monkeypatch):
        # Four games from this week: 3, 4, 6, 7 — the week-5 bye is not one.
        p = await self._run(monkeypatch, "IR", db=FakeDB({5: {"BUF"}}))
        own = p["per_game"]
        assert p["weekly_points"][4] == pytest.approx(own + 4.0, abs=0.01)
        assert p["weekly_points"][5] == 0.0
        assert p["weekly_points"][7] == pytest.approx(own + 4.0, abs=0.01)
        assert p["weekly_points"][8] == pytest.approx(own, abs=0.01)


class TestAbsenceCountsGames:
    @pytest.mark.asyncio
    async def test_a_bye_does_not_use_up_an_absence_week(self, monkeypatch):
        _stub_projection(monkeypatch, {"Hurt": 0.0},
                         {"base_ppg": 12.0, "base_source": "rank_bucket"})
        out = await ros.ros_projections(
            [_player("Hurt", status="IR")], season=2026, week=3, settings=SETTINGS,
            db=FakeDB({5: {"BUF"}}))
        p = out["players"][0]
        assert p["bye_weeks"] == [5]
        assert p["injury_weeks"] == [3, 4, 6, 7]
        assert p["weekly_points"][8] == pytest.approx(12.0)

    def test_the_ir_minimum_counts_from_placement(self):
        today = date(2026, 9, 23)
        assert ros.expected_absence("IR", today=today)[0] == ros.IR_MIN_WEEKS
        weeks, reason = ros.expected_absence("IR", today=today, placed_on=today - timedelta(days=15))
        assert weeks == ros.IR_MIN_WEEKS - 2 and "since 2026-09-08" in reason
        assert ros.expected_absence("IR", today=today, placed_on=date(2026, 8, 1))[0] == 1

    def test_reserve_since_reads_the_unbroken_reserve_run(self):
        history = [  # newest first
            {"injury_status": "Injured Reserve", "recorded_at": "2026-09-16T10:00:00+00:00"},
            {"injury_status": "Injured Reserve", "recorded_at": "2026-09-09T10:00:00+00:00"},
            {"injury_status": "Out", "recorded_at": "2026-09-05T10:00:00+00:00"},
            {"injury_status": "Injured Reserve", "recorded_at": "2025-11-01T10:00:00+00:00"},
        ]
        assert ros.reserve_since(history) == date(2026, 9, 9)
        assert ros.reserve_since(history[2:3]) is None
        assert ros.reserve_since([]) is None

    def test_placement_is_attached_from_the_history(self):
        class DB:
            def get_injury_history(self, pid, limit=10):
                assert pid == "espn-1"
                return [{"injury_status": "Injured Reserve",
                         "recorded_at": "2026-09-01T00:00:00+00:00"}]

        inputs = [{"name": "A", "injury": {"status": "IR", "report_id": "espn-1"}},
                  {"name": "B", "injury": {"status": "Out", "report_id": "espn-2"}}]
        out = ros._with_reserve_dates(inputs, DB())
        assert out[0]["injury"]["placed_on"] == date(2026, 9, 1)
        assert "placed_on" not in out[1]["injury"]


class TestSeasonEndingText:
    @pytest.mark.parametrize("text", [
        "questionable for the season opener", "out for the season-opener",
        "expected back for the season finale", "limited for the season debut",
    ])
    def test_a_single_game_is_not_the_season(self, text):
        assert ros.expected_absence("Out", text)[0] == 1

    def test_the_season_still_is(self):
        assert ros.expected_absence("IR", "done for the season")[0] == ros.SEASON_ENDING_WEEKS


class TestFetchedSchedule:
    def test_a_partial_response_is_unknown_not_byes(self):
        rows = [{"team": "BUF", "opponent": "MIA"}, {"team": "KC", "opponent": "LV"}]
        assert ros._rows_to_schedule(rows) is None

    def test_a_full_week_listed_one_side_per_game(self):
        rows = [{"team": a, "opponent": b} for a, b in zip(TEAMS[::2], TEAMS[1::2], strict=True)]
        sched = ros._rows_to_schedule(rows)
        assert sched["ATL"] == "ARI" and len(sched) == 32


class TestKickerDefenseRos:
    @pytest.mark.asyncio
    async def test_later_weeks_follow_the_opponent(self, monkeypatch):
        async def _unit(position, team, opponent, season, model):
            return {"projected_points": 11.0 if opponent == "ATL" else 5.0,
                    "matchup_tier": "smash" if opponent == "ATL" else "tough"}

        monkeypatch.setattr(projections, "_unit_matchup", _unit)
        _stub_projection(monkeypatch, {"ARI": 7.0, "KC": 7.0})
        players = [_player("ARI", team="ARI", position="DEF"),
                   _player("KC", team="KC", position="DEF")]
        out = await ros.ros_projections(players, season=2026, week=3, settings=SETTINGS,
                                        db=FakeDB(), include_weekly=True)
        by = {p["player"]: p for p in out["players"]}
        # FakeDB pairs ARI with ATL every week; KC plays someone else.
        assert by["ARI"]["weekly_points"][4] == 11.0
        assert by["KC"]["weekly_points"][4] == 5.0
        assert by["ARI"]["ros_points"] != by["KC"]["ros_points"]
        assert by["ARI"]["weekly"][1]["reason"] == "smash matchup"

    @pytest.mark.asyncio
    async def test_no_offense_read_keeps_the_neutral_rate(self, monkeypatch):
        # conftest stubs `_unit_matchup` to None.
        _stub_projection(monkeypatch, {"KC": 7.0})
        out = await ros.ros_projections([_player("KC", team="KC", position="K")], season=2026,
                                        week=3, settings=SETTINGS, db=FakeDB())
        p = out["players"][0]
        assert p["weekly_points"][4] == p["per_game"]


class TestTradedPlayerTeam:
    def test_team_is_the_latest_weeks(self):
        header = "player_id,player_display_name,position,recent_team,season_type,week,opponent_team"
        rows = ["p1,Moved Guy,WR,NYJ,REG,2,MIA", "p1,Moved Guy,WR,NYJ,REG,1,BUF",
                "p1,Moved Guy,WR,PIT,REG,4,CLE", "p1,Moved Guy,WR,NYJ,REG,3,NE"]
        logs = opportunity_tools.parse_game_logs("\n".join([header, *rows]))
        assert logs["p1"]["team"] == "PIT"
        assert {g["week"]: g["team"] for g in logs["p1"]["games"]}[1] == "NYJ"


class TestUsageTrendsPastSeason:
    @pytest.mark.asyncio
    async def test_a_past_season_ends_at_its_last_week(self):
        game = {"targets": 5.0, "carries": 0.0, "team": "BUF", "target_share": 0.2}
        logs = {"w1": {"player_id": "w1", "name": "Wide Out", "position": "WR", "team": "BUF",
                       "games": [{**game, "week": w} for w in range(1, 18)]}}
        with patch.object(usage_trends, "resolve_season_week",
                          AsyncMock(return_value=(2026, 3, True))), \
             patch.object(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=logs)), \
             patch.object(usage_trends, "_fetch_week_stats", AsyncMock(return_value={})):
            out = await usage_trends.get_usage_trends(player_names=["Wide Out"], weeks=4,
                                                      season=2024)
        assert out["window"] == [14, 15, 16, 17]


class TestCbsScoringAndSeason:
    @staticmethod
    def _client():
        client = AsyncMock()
        resp = Mock(text="<html><body><table class='stats'></table></body></html>",
                    raise_for_status=Mock())
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        return client

    @pytest.mark.asyncio
    @pytest.mark.parametrize("given,expected", [
        ("half_ppr", "half-ppr"), ("0.5", "half-ppr"), ("Half PPR", "half-ppr"),
        ("non-ppr", "standard"), ("standard", "standard"), ("ppr", "ppr"), ("junk", "ppr"),
    ])
    async def test_scoring_spellings(self, given, expected):
        client = self._client()
        with patch("nfl_mcp.cbs_fantasy_tools.create_http_client", return_value=client):
            out = await cbs_fantasy_tools.get_cbs_projections(position="QB", week=1,
                                                              scoring=given)
        assert out["scoring"] == expected
        assert f"/projections/{expected}/" in client.get.call_args.args[0]

    @pytest.mark.asyncio
    async def test_season_defaults_to_the_current_one(self):
        with patch("nfl_mcp.cbs_fantasy_tools.create_http_client", return_value=self._client()), \
             patch.object(cbs_fantasy_tools, "infer_from_calendar", return_value=(2031, 3)):
            out = await cbs_fantasy_tools.get_cbs_projections(position="QB", week=1)
        assert out["season"] == 2031


class TestLastStateExpires:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(wc, "_last_state", None)

    @pytest.mark.asyncio
    async def test_a_stale_state_falls_through_to_the_schedule(self, monkeypatch):
        from nfl_mcp import sleeper_tools
        monkeypatch.setattr(sleeper_tools, "get_nfl_state", AsyncMock(side_effect=OSError("down")))
        monkeypatch.setattr(wc, "infer_from_schedule", lambda db: (2026, 5))
        fresh = datetime.now(UTC) - timedelta(hours=1)
        monkeypatch.setattr(wc, "_last_state", {"season": 2026, "week": 3, "at": fresh})
        assert await wc.current_season_week() == {"season": 2026, "week": 3,
                                                  "source": "cached_state"}
        stale = datetime.now(UTC) - wc.LAST_STATE_MAX_AGE - timedelta(minutes=1)
        monkeypatch.setattr(wc, "_last_state", {"season": 2026, "week": 3, "at": stale})
        assert await wc.current_season_week() == {"season": 2026, "week": 5,
                                                  "source": "schedule"}
