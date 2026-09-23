"""K and DEF in start/sit, the DEF streaming label, byes in a streaming window,
and the NFL-points scale the K/DEF pricing reads (network mocked)."""
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp import matchup_tools, streaming_tools
from nfl_mcp import sleeper_projections as sp
from nfl_mcp.scoring import ScoringModel
from nfl_mcp.streaming_tools import compute_streaming_scores, unit_matchup, unit_points


def _offense(entries, games=6):
    # {team: (rank, real points per game)}
    return {t: {"rank": r, "points_scored_avg": 100.0 - r, "real_points_avg": ppg, "games": games}
            for t, (r, ppg) in entries.items()}


class TestRealPoints:
    def test_row_points_rebuild_the_score(self):
        row = {"rushing_tds": "1", "receiving_tds": "2", "fg_made": "2", "pat_made": "3",
               "rushing_2pt_conversions": "0", "receiving_2pt_conversions": "0"}
        assert matchup_tools._row_points(row) == 6 * 3 + 6 + 3

    @pytest.mark.asyncio
    async def test_offense_rankings_carry_shrunk_nfl_points(self):
        csv_text = (
            "season_type,position,team,week,fantasy_points_ppr,receiving_tds,fg_made,pat_made\n"
            "REG,WR,KC,1,90,4,0,0\n"
            "REG,K,KC,1,0,0,2,4\n"          # kickers add to the score, not the rank
            "REG,WR,BUF,1,50,1,0,0\n"
        )
        resp = Mock(status_code=200, text=csv_text)
        resp.raise_for_status = Mock()
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        matchup_tools._offense_rankings_cache.pop(2098, None)
        with patch("nfl_mcp.matchup_tools.create_http_client", return_value=client):
            r = await matchup_tools.fetch_offense_rankings(2098)
        matchup_tools._offense_rankings_cache.pop(2098, None)
        # KC scored 24 + 6 + 4 = 34 in one game; shrunk with 6 games of 22.
        assert r["KC"]["real_points_avg"] == round((34 + 22 * 6) / 7, 1)
        assert r["KC"]["games"] == 1 and r["KC"]["rank"] == 1


class TestStreaming:
    def test_def_label_is_scored_like_dst(self):
        opps = {"KC": {3: "LV"}}
        off = _offense({"LV": (30, 15.0)})
        s = compute_streaming_scores(opps, ["DEF"], {}, off, analyzer=None)
        assert s["DEF"]["KC"]["stream_score"] > 90
        assert s["DEF"]["KC"]["weeks"][0]["opponent_points_per_game"] == 15.0

    def test_a_bye_in_the_window_counts_as_zero(self):
        opps = {"KC": {5: "LV", 7: "DEN"}, "BUF": {5: "MIA", 6: "NE", 7: "NYJ"}}
        off = _offense({"LV": (30, 15.0), "DEN": (30, 15.0), "MIA": (30, 15.0),
                        "NE": (30, 15.0), "NYJ": (30, 15.0)})
        s = compute_streaming_scores(opps, ["DST"], {}, off, analyzer=None)
        kc, buf = s["DST"]["KC"], s["DST"]["BUF"]
        assert kc["byes"] == [6] and buf["byes"] == []
        assert kc["stream_score"] == pytest.approx(buf["stream_score"] * 2 / 3, abs=0.1)
        assert [w["week"] for w in kc["weeks"]] == [5, 6, 7]
        assert unit_points("DST", kc["weeks"][1], ScoringModel.preset(1.0)) == 0.0

    def test_a_week_without_any_schedule_is_not_a_bye(self):
        s = compute_streaming_scores({"KC": {5: "LV"}}, ["DST"], {},
                                     _offense({"LV": (20, 22.0)}), analyzer=None)
        assert s["DST"]["KC"]["byes"] == []

    def test_unit_points_read_nfl_points_not_fantasy_points(self):
        model = ScoringModel.preset(1.0)
        # A 15-point opponent is an 11-point defense week; the old code read
        # ~85 fantasy points here and priced every defense at 3.5.
        assert unit_points("DEF", {"opponent_points_scored_avg": 85.0,
                                   "opponent_points_per_game": 15.0}, model) == 11.0
        assert unit_points("K", {"own_points_per_game": 25.0}, model) == 9.5


class TestUnitMatchup:
    @pytest.mark.asyncio
    async def test_defense_reads_the_opponents_offense(self):
        off = _offense({"LV": (30, 16.0), "KC": (2, 28.0)})
        with patch.object(streaming_tools, "_resolve_offense", AsyncMock(return_value=(off, 2026, False))):
            d = await unit_matchup("DEF", "KC", "LV", 2026, ScoringModel.preset(1.0))
            k = await unit_matchup("K", "KC", "LV", 2026, ScoringModel.preset(1.0))
        assert d["offense_side"] == "opponent" and d["offense_rank"] == 30
        assert d["matchup_tier"] == "smash" and d["projected_points"] == 11.0
        assert k["offense_side"] == "own" and k["offense_rank"] == 2
        assert k["matchup_tier"] == "smash"

    @pytest.mark.asyncio
    async def test_tier_is_withheld_on_a_thin_sample(self):
        off = _offense({"LV": (30, 16.0)}, games=2)
        with patch.object(streaming_tools, "_resolve_offense", AsyncMock(return_value=(off, 2026, False))):
            d = await unit_matchup("DEF", "KC", "LV", 2026, ScoringModel.preset(1.0))
        assert d["matchup_tier"] == "neutral" and d["tier_withheld"]

    @pytest.mark.asyncio
    async def test_skill_positions_are_not_units(self):
        assert await unit_matchup("WR", "KC", "LV", 2026, ScoringModel.preset(1.0)) is None


class _Engine:
    def __init__(self, vegas=False, points=7.0):
        self.vegas, self.points = vegas, points

    async def project_many(self, players, **kwargs):
        return {"projections": [{
            "projected_points": self.points, "floor": 1.0, "ceiling": 13.0,
            "vegas_active": self.vegas, "implied_total": 27.0, "opponent_implied_total": 17.5,
            "breakdown": {"base_source": "opponent_total", "injury_mult": 1.0},
        } for _ in players]}


def _optimizer():
    opt = lo.LineupOptimizer.__new__(lo.LineupOptimizer)
    opt.db, opt.defense_analyzer, opt.auto_project = None, None, True
    return opt


class TestStartSit:
    async def _analyze(self, position, team, opponent, engine, off, scoring="ppr"):
        with patch("nfl_mcp.projections.get_projection_engine", return_value=engine), \
             patch.object(streaming_tools, "_resolve_offense", AsyncMock(return_value=(off, 2026, False))), \
             patch("nfl_mcp.injury_match.lookup_injury", return_value=None):
            return await _optimizer().analyze_player(
                player_name=team if position == "DEF" else "Some Kicker", player_id="",
                position=position, team=team, opponent=opponent, scoring=scoring,
                season=2026, week=3)

    @pytest.mark.asyncio
    async def test_defense_without_vegas_is_priced_off_the_opponents_scoring(self):
        off = _offense({"LV": (30, 16.0)})
        a = await self._analyze("DEF", "KC", "LV", _Engine(vegas=False), off)
        assert a.base_source == "offense_rank"
        assert a.projected_points == 11.0 and a.matchup_tier == "smash"
        assert a.decision == "must_start"            # 11 >= the 10-point DEF mark
        assert any("offense ranks #30" in r for r in a.reasoning)

    @pytest.mark.asyncio
    async def test_vegas_projection_is_kept_and_its_totals_reported(self):
        off = _offense({"KC": (3, 27.0)})
        a = await self._analyze("K", "KC", "LV", _Engine(vegas=True, points=9.0), off)
        assert a.projected_points == 9.0 and a.base_source == "opponent_total"
        assert a.implied_total == 27.0
        assert any("Team implied total 27.0" in r for r in a.reasoning)

    @pytest.mark.asyncio
    async def test_good_week_marks_follow_the_leagues_defense_scoring(self):
        rich = ScoringModel.from_settings({"sack": 2.0, "int": 4.0})
        assert lo.unit_threshold_scale("DEF", rich) > 1.2
        assert lo.unit_threshold_scale("WR", rich) == 1.0

    @pytest.mark.asyncio
    async def test_sleeper_second_opinion_on_a_defense(self, monkeypatch):
        index = sp._index({"KC": {"sack": 3.0, "int": 1.0, "pts_allow": 17.0, "pts_ppr": 9.0}})
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=index))
        off = _offense({"LV": (30, 16.0)})
        a = await self._analyze("DEF", "KC", "LV", _Engine(vegas=False), off)
        assert a.sleeper_projection is not None and a.consensus is not None
        assert a.to_dict()["sleeper_projection"] == a.sleeper_projection

    @pytest.mark.asyncio
    async def test_compare_ranks_two_defenses(self):
        off = _offense({"LV": (30, 16.0), "BUF": (1, 30.0)})
        with patch("nfl_mcp.projections.get_projection_engine", return_value=_Engine()), \
             patch.object(streaming_tools, "_resolve_offense", AsyncMock(return_value=(off, 2026, False))), \
             patch("nfl_mcp.injury_match.lookup_injury", return_value=None), \
             patch.object(lo, "get_lineup_optimizer", return_value=_optimizer()):
            out = await lo.compare_players_for_slot(
                players=[{"name": "MIA", "position": "DEF", "team": "MIA", "opponent": "BUF"},
                         {"name": "KC", "position": "DEF", "team": "KC", "opponent": "LV"}],
                slot="DEF", scoring="ppr", season=2026, week=3)
        assert out["winner"]["player"] == "KC"
        assert out["ineligible"] == []
