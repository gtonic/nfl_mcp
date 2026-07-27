"""Tests for the opportunity-based projection library and tool."""
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp.opportunity import (
    _weighted_mean,
    project_opportunity,
    rec_points,
    rush_points,
)
from nfl_mcp.opportunity_tools import get_opportunity_projections, parse_game_logs


def _wr_game(week, targets, rec, rec_yds, rec_td=0.0, carries=0.0):
    return {
        "week": week, "targets": targets, "receptions": rec,
        "receiving_yards": rec_yds, "receiving_tds": rec_td, "carries": carries,
    }


class TestScoring:
    def test_rec_and_rush_points(self):
        # 6 rec + 80 yds + 1 TD = 6 + 8 + 6 = 20 (PPR)
        assert rec_points({"receptions": 6, "receiving_yards": 80, "receiving_tds": 1}) == 20.0
        # 50 yds + 1 TD = 5 + 6 = 11
        assert rush_points({"rushing_yards": 50, "rushing_tds": 1}) == 11.0


class TestWeightedMean:
    def test_recency_weights_favor_recent(self):
        # targets 2,4,6,8 with weights 1,2,3,4 -> 60/10 = 6.0 (> flat mean 5.0)
        assert _weighted_mean([2, 4, 6, 8], [1, 2, 3, 4]) == 6.0


class TestProjectOpportunity:
    def test_wr_projection_matches_model(self):
        games = [_wr_game(w, targets=8, rec=6, rec_yds=80) for w in range(1, 5)]
        # rec_pts/gm = 6 + 8 = 14; ppt = (56 + 20*1.55)/(32+20) = 87/52 = 1.673
        # exp_targets = 8 -> 8 * 1.673 = 13.38
        proj = project_opportunity(games, "WR")
        assert proj == pytest.approx(13.38, abs=0.1)

    def test_shrinkage_pulls_small_sample_toward_prior(self):
        # One monster game (huge efficiency) should be shrunk toward the prior,
        # not projected at face value.
        hot = [_wr_game(1, targets=2, rec=2, rec_yds=100, rec_td=2)]  # 2+10+12 = 24 pts on 2 tgt
        proj = project_opportunity(hot, "WR")
        raw_ppt = 24 / 2  # 12 pts/target if we trusted the sample fully
        assert proj is not None
        assert proj < 2 * raw_ppt * 0.5  # heavily regressed, nowhere near 2*12

    def test_qb_uses_attempts(self):
        games = [
            {"week": w, "attempts": 35, "passing_yards": 280, "passing_tds": 2,
             "interceptions": 1, "carries": 3, "rushing_yards": 15, "rushing_tds": 0}
            for w in range(1, 5)
        ]
        proj = project_opportunity(games, "QB")
        assert proj == pytest.approx(18.77, abs=0.2)

    def test_unknown_position_and_empty(self):
        assert project_opportunity([_wr_game(1, 5, 4, 50)], "K") is None
        assert project_opportunity([], "WR") is None


class TestParseGameLogs:
    def test_groups_and_filters(self):
        csv_text = (
            "season_type,position,player_id,player_display_name,recent_team,week,"
            "targets,carries,receptions,receiving_yards,receiving_tds\n"
            "REG,WR,a1,Alpha,BUF,1,9,0,6,80,0\n"
            "REG,WR,a1,Alpha,BUF,2,11,0,8,110,1\n"
            "POST,WR,a1,Alpha,BUF,3,20,0,15,200,3\n"   # dropped: not REG
            "REG,K,k1,Kicker,BUF,1,0,0,0,0,0\n"        # dropped: position
        )
        logs = parse_game_logs(csv_text)
        assert set(logs) == {"a1"}
        assert logs["a1"]["name"] == "Alpha"
        assert len(logs["a1"]["games"]) == 2          # POST dropped
        assert logs["a1"]["games"][0]["targets"] == 9.0


class TestGetOpportunityProjections:
    def _logs(self):
        return {
            "a1": {"player_id": "a1", "name": "Alpha WR", "position": "WR", "team": "BUF",
                   "games": [_wr_game(w, targets=10, rec=7, rec_yds=95) for w in range(1, 6)]},
            "b1": {"player_id": "b1", "name": "Bravo WR", "position": "WR", "team": "MIA",
                   "games": [_wr_game(w, targets=3, rec=2, rec_yds=22) for w in range(1, 6)]},
        }

    @pytest.mark.asyncio
    async def test_ranks_highest_first(self):
        with patch("nfl_mcp.opportunity_tools._fetch_game_logs",
                   new=AsyncMock(return_value=self._logs())):
            result = await get_opportunity_projections(season=2025, week=6)
        assert result["success"] is True
        assert result["count"] == 2
        projs = result["projections"]
        assert projs[0]["name"] == "Alpha WR"     # more volume -> higher projection
        assert projs[0]["projected_ppr"] > projs[1]["projected_ppr"]
        assert projs[0]["exp_targets"] == 10.0

    @pytest.mark.asyncio
    async def test_player_filter(self):
        with patch("nfl_mcp.opportunity_tools._fetch_game_logs",
                   new=AsyncMock(return_value=self._logs())):
            result = await get_opportunity_projections(season=2025, week=6, players=["Bravo"])
        assert result["count"] == 1
        assert result["projections"][0]["name"] == "Bravo WR"

    @pytest.mark.asyncio
    async def test_week_1_rejected(self):
        result = await get_opportunity_projections(season=2025, week=1)
        assert result["success"] is False
        assert "week must be" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_logs_errors(self):
        with patch("nfl_mcp.opportunity_tools._fetch_game_logs",
                   new=AsyncMock(return_value={})):
            result = await get_opportunity_projections(season=1999, week=6)
        assert result["success"] is False
        assert "No nflverse game logs" in result["error"]
