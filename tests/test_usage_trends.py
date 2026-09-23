"""Usage trends: per-week shares, trend direction and flags (network mocked)."""
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import opportunity_tools, usage_trends
from nfl_mcp.usage_trends import metric_trends, team_week_carries, teams_with_games, week_row

pytestmark = pytest.mark.usefixtures("offline_sources")  # no ambient network reads


def _game(week, team="BUF", targets=6.0, carries=0.0, ts=0.2, ays=0.25, wopr=0.475, opp="MIA"):
    return {"week": week, "team": team, "opponent": opp, "targets": targets,
            "carries": carries, "target_share": ts, "air_yards_share": ays,
            "wopr": wopr, "racr": 1.1, "receiving_air_yards": 60.0}


def _played(week, **metrics):
    return {"week": week, "status": "played", **metrics}


class TestParse:
    def test_usage_shares_and_the_weeks_team_are_kept(self):
        csv_text = (
            "season_type,position,player_id,player_display_name,team,opponent_team,week,"
            "targets,target_share,air_yards_share,wopr,racr\n"
            "REG,WR,w1,Wide Out,BUF,MIA,1,8,0.25,0.3,0.585,1.2\n"
            "REG,WR,w1,Wide Out,LA,SF,2,5,,,,\n"
        )
        games = opportunity_tools.parse_game_logs(csv_text)["w1"]["games"]
        assert games[0]["target_share"] == 0.25
        assert games[0]["opponent"] == "MIA"
        # Blank is unknown, not zero; the traded week keeps the team he played for.
        assert games[1]["target_share"] is None
        assert games[1]["team"] == "LAR"


class TestWeekRow:
    def test_shares_snaps_and_red_zone(self):
        carries = {("BUF", 1): 20.0}
        sleeper = {"off_snp": 45, "tm_off_snp": 60, "rec_rz_tgt": 2, "rush_rz_att": 1}
        row = week_row(1, _game(1, carries=5.0), sleeper, "BUF", carries, {"BUF"})
        assert row["status"] == "played"
        assert row["target_share"] == 20.0
        assert row["air_yards_share"] == 25.0
        assert row["carries_share"] == 25.0
        assert row["snap_share"] == 75.0
        assert row["rz_opportunities"] == 3.0

    def test_a_missing_red_zone_key_with_a_stat_line_is_zero(self):
        row = week_row(1, _game(1), {"off_snp": 30, "tm_off_snp": 60}, "BUF", {}, {"BUF"})
        assert row["rz_opportunities"] == 0.0

    def test_no_sleeper_line_leaves_snaps_and_red_zone_unknown(self):
        row = week_row(1, _game(1), None, "BUF", {}, {"BUF"})
        assert row["snap_share"] is None and row["rz_opportunities"] is None

    def test_bye_versus_did_not_play(self):
        assert week_row(5, None, None, "BUF", {}, {"MIA"})["status"] == "bye"
        assert week_row(5, None, None, "BUF", {}, {"BUF", "MIA"})["status"] == "did_not_play"


class TestTrends:
    def test_three_straight_rises_are_flagged(self):
        rows = [_played(w, target_share=ts) for w, ts in ((1, 15.0), (2, 19.0), (3, 24.0), (4, 28.0))]
        trends, flags = metric_trends(rows, "WR")
        assert trends["target_share"]["direction"] == "rising"
        assert "target share up 3 weeks in a row" in flags

    def test_a_wobble_is_stable(self):
        rows = [_played(w, target_share=ts) for w, ts in ((1, 20.0), (2, 21.5), (3, 19.5), (4, 20.5))]
        trends, flags = metric_trends(rows, "WR")
        assert trends["target_share"]["direction"] == "stable"
        assert not any("target share" in f for f in flags)

    def test_two_weeks_give_a_direction_but_no_flag(self):
        rows = [_played(1, snap_share=50.0), _played(2, snap_share=70.0)]
        trends, flags = metric_trends(rows, "RB")
        assert trends["snap_share"]["direction"] == "rising"
        assert not any("snap share" in f for f in flags)

    def test_byes_and_missed_weeks_are_left_out_of_the_trend(self):
        rows = [_played(1, snap_share=80.0), {"week": 2, "status": "bye"},
                {"week": 3, "status": "did_not_play"}, _played(4, snap_share=40.0)]
        trends, flags = metric_trends(rows, "WR")
        assert trends["snap_share"]["weeks"] == 2
        assert "did not play week 3" in flags
        assert any(f.startswith("part-time role: 40%") for f in flags)

    def test_quarterback_receiving_shares_are_not_trended(self):
        rows = [_played(w, target_share=0.0, carries_share=c) for w, c in ((1, 10.0), (2, 20.0))]
        trends, _ = metric_trends(rows, "QB")
        assert "target_share" not in trends and "carries_share" in trends


class TestTeamTotals:
    def test_carries_and_games_per_team_week(self):
        logs = {"a": {"team": "BUF", "games": [_game(1, carries=12.0)]},
                "b": {"team": "BUF", "games": [_game(1, carries=8.0), _game(2, team="MIA", carries=3.0)]}}
        assert team_week_carries(logs)[("BUF", 1)] == 20.0
        assert teams_with_games(logs) == {1: {"BUF"}, 2: {"MIA"}}


class _DB:
    def get_athletes_by_ids(self, ids):
        rows = {"100": {"full_name": "Wide Out", "position": "WR", "team_id": "BUF"},
                "KC": {"full_name": None, "position": "DEF", "team_id": "KC"}}
        return {i: rows[i] for i in ids if i in rows}

    def search_athletes_by_name(self, name, limit=10):
        return [{"id": "100", "full_name": "Wide Out", "position": "WR", "team_id": "BUF"}]


def _logs():
    return {"w1": {"player_id": "w1", "name": "Wide Out", "position": "WR", "team": "BUF",
                   "games": [_game(w, ts=ts) for w, ts in ((1, 0.15), (2, 0.2), (3, 0.26))]}}


class TestTool:
    @pytest.mark.asyncio
    async def test_roster_mode_skips_units_and_joins_both_sources(self):
        rosters = {"rosters": [{"roster_id": 1, "players": ["100", "KC"]}]}
        week_stats = AsyncMock(side_effect=lambda s, w: {"100": {"off_snp": 40 + w, "tm_off_snp": 60,
                                                                 "rec_rz_tgt": w}})
        with patch.object(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_logs())), \
             patch.object(usage_trends, "_fetch_week_stats", week_stats), \
             patch("nfl_mcp.sleeper_tools.get_rosters", AsyncMock(return_value=rosters)):
            out = await usage_trends.get_usage_trends(
                league_id="L", roster_id=1, weeks=3, season=2026, through_week=3, db=_DB())
        assert out["success"] and out["window"] == [1, 2, 3]
        assert [p["player"] for p in out["players"]] == ["Wide Out"]
        p = out["players"][0]
        assert [w["target_share"] for w in p["weeks"]] == [15.0, 20.0, 26.0]
        assert p["weeks"][2]["rz_opportunities"] == 3.0
        assert "target share up 2 weeks in a row" in p["flags"]

    @pytest.mark.asyncio
    async def test_names_mode_resolves_the_sleeper_id(self):
        with patch.object(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_logs())), \
             patch.object(usage_trends, "_fetch_week_stats", AsyncMock(return_value={})):
            out = await usage_trends.get_usage_trends(
                player_names=["wide out"], weeks=3, season=2026, through_week=3, db=_DB())
        p = out["players"][0]
        assert p["sleeper_id"] == "100" and p["found_in_nflverse"]
        assert out["notes"]  # Sleeper weeks missing are said, not hidden

    @pytest.mark.asyncio
    async def test_needs_players_or_a_roster(self):
        out = await usage_trends.get_usage_trends(league_id="L", season=2026)
        assert out["success"] is False


# 28 teams play; LV is among them every week of the window (its bye is 13).
_TEAMS = ["LV", "MIA", "LAC", "BUF", "KC", "DEN", "NE", "NYJ", "PIT", "BAL", "CLE", "CIN",
          "HOU", "IND", "JAX", "TEN", "DAL", "PHI", "NYG", "WAS", "CHI", "DET", "GB", "MIN",
          "ATL", "CAR", "NO", "TB"]


class TestByeVersusInjured:
    SCHEDULE = dict.fromkeys(_TEAMS, "XXX")

    def test_team_on_the_schedule_is_not_on_bye(self):
        # The weekly file may not list his team (lagging or partial file);
        # the schedule decides.
        row = week_row(1, None, None, "LV", {}, {"MIA"}, schedule=self.SCHEDULE,
                       injury_status="Out")
        assert row["status"] == "injured"
        assert row["injury_status"] == "Out"

    def test_team_missing_from_the_schedule_is_on_bye(self):
        assert week_row(13, None, None, "SF", {}, None, schedule=self.SCHEDULE)["status"] == "bye"

    def test_no_report_is_did_not_play(self):
        row = week_row(1, None, None, "LV", {}, None, schedule=self.SCHEDULE)
        assert row["status"] == "did_not_play"

    def test_suspension_is_inactive(self):
        assert usage_trends.missed_week_status("Suspended") == "inactive"
        assert usage_trends.missed_week_status("Questionable") == "injured"
        assert usage_trends.missed_week_status("Active") == "did_not_play"

    def test_status_at_kickoff(self):
        history = [
            {"injury_status": "Questionable", "recorded_at": "2026-09-16T07:00:00+00:00"},
            {"injury_status": "Out", "recorded_at": "2026-09-20T19:00:00+00:00"},
        ]
        # Latest report before the game.
        assert usage_trends.status_at_kickoff(history, "2026-09-20T20:05Z") == "Out"
        # History starts after week 1: the first report within days of it.
        assert usage_trends.status_at_kickoff(history, "2026-09-13T20:25Z") == "Questionable"
        assert usage_trends.status_at_kickoff(history, "2026-09-01T20:25Z") is None
        assert usage_trends.status_at_kickoff([], "2026-09-13T20:25Z") is None


class _InjuryDB(_DB):
    def get_athletes_by_ids(self, ids):
        rows = {"11604": {"full_name": "Brock Bowers", "position": "TE", "team_id": "LV"}}
        return {i: rows[i] for i in ids if i in rows}

    def get_week_opponents(self, season, week):
        return dict(zip(_TEAMS, _TEAMS[1:] + _TEAMS[:1], strict=True))

    def get_week_kickoffs(self, season, week):
        return {"LV": {1: "2026-09-13T20:25Z", 2: "2026-09-20T20:05Z"}[week]}

    def get_all_current_injuries(self):
        return [{"player_id": "4432665", "player_name": "Brock Bowers", "team_id": "LV",
                 "injury_status": "Questionable"}]

    def get_injury_history(self, player_id, limit=10):
        assert player_id == "4432665"  # the report's id, not the Sleeper one
        return [
            {"injury_status": "Questionable", "recorded_at": "2026-09-16T07:02:23+00:00"},
            {"injury_status": "Out", "recorded_at": "2026-09-20T19:39:56+00:00"},
        ]


class TestInjuredWeeksInTheTool:
    @pytest.mark.asyncio
    async def test_bowers_weeks_1_2_are_injured_not_bye(self):
        rosters = {"rosters": [{"roster_id": 7, "players": ["11604"]}]}
        # Nobody from LV in the weekly file those weeks.
        logs = {"m1": {"player_id": "m1", "name": "Other Guy", "position": "WR", "team": "MIA",
                       "games": [_game(1, team="MIA"), _game(2, team="MIA")]}}
        with patch.object(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=logs)), \
             patch.object(usage_trends, "_fetch_week_stats", AsyncMock(return_value={})), \
             patch("nfl_mcp.sleeper_tools.get_rosters", AsyncMock(return_value=rosters)):
            out = await usage_trends.get_usage_trends(
                league_id="L", roster_id=7, weeks=2, season=2026, through_week=2,
                db=_InjuryDB())
        weeks = out["players"][0]["weeks"]
        assert [w["status"] for w in weeks] == ["injured", "injured"]
        assert weeks[1]["injury_status"] == "Out"
        assert "missed week 1, 2 injured" in out["players"][0]["flags"]
