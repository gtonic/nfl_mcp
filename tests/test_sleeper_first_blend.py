"""The weekly projection is Sleeper-first: 0.25 × ours + 0.75 × Sleeper (network mocked)."""
import types
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp import opportunity_tools, projections
from nfl_mcp import sleeper_projections as sp
from nfl_mcp.scoring import ScoringModel

PPR = ScoringModel.preset(1.0)


def _row(pid, first, last, pos, team, stats):
    return {"player_id": pid, "team": team, "opponent": "MIA",
            "player": {"first_name": first, "last_name": last, "position": pos, "team": team},
            "stats": stats}


def _payload():
    return [
        # 5 catches, 60 yards -> 11.0 in full PPR
        _row("1", "Wide", "Receiver", "WR", "BUF", {"rec": 5.0, "rec_yd": 60.0, "pts_ppr": 11.0}),
        # Listed without points: Sleeper does not expect him to play.
        _row("2", "Benched", "Quarterback", "QB", "BUF", {"adp_dd_ppr": 1000.0}),
        # Listed without points on a team nobody is projected for yet: missing.
        _row("3", "Early", "Listing", "RB", "NYJ", {"adp_dd_ppr": 1000.0}),
        {"player_id": "BUF", "team": "BUF", "player": {"position": "DEF", "last_name": "Bills"},
         "stats": {"sack": 3.0, "int": 1.0, "pts_allow": 17.0, "pts_ppr": 9.0}},
    ]


class TestBlend:
    def test_healthy_is_a_quarter_ours(self):
        assert sp.blend(20.0, 12.0) == 14.0
        assert sp.BLEND_WEIGHTS == {"model": 0.25, "sleeper": 0.75}

    def test_out_is_zero_whatever_sleeper_says(self):
        assert sp.blend(0.0, 15.0, "out", 0.0) == 0.0

    def test_questionable_discounts_only_our_part(self):
        # ours already carries the 0.9; Sleeper's number is taken as-is.
        assert sp.blend(9.0, 10.0, "questionable", 0.9) == round(0.25 * 9.0 + 0.75 * 10.0, 1)

    def test_doubtful_is_capped_at_the_doubtful_share(self):
        # healthy ours 10 (x0.35 = 3.5), Sleeper still projects him in full.
        assert sp.blend(3.5, 12.0, "doubtful", 0.35) == round(0.35 * 12.0, 1)
        # Sleeper already cut him: the blend stands.
        assert sp.blend(3.5, 2.0, "doubtful", 0.35) == round(0.25 * 3.5 + 0.75 * 2.0, 1)


class TestPointsFor:
    def setup_method(self):
        self.index = sp._index(_payload())

    def test_projected(self):
        assert sp.points_for(self.index, PPR, name="Wide Receiver", team="BUF") == (11.0, "projected")

    def test_listed_without_points_is_an_explicit_zero(self):
        assert sp.points_for(self.index, PPR, player_id="2", name="Benched Quarterback",
                             team="BUF") == (0.0, "not_projected")

    def test_zero_for_an_unpublished_team_is_missing(self):
        assert sp.points_for(self.index, PPR, name="Early Listing", team="NYJ") == (None, "missing")

    def test_no_row_is_missing(self):
        assert sp.points_for(self.index, PPR, name="Nobody Here", team="BUF") == (None, "missing")

    def test_defense(self):
        pts, status = sp.points_for(self.index, PPR, name="BUF", team="BUF", position="DEF")
        assert status == "projected" and pts > 0


def _games():
    return [{"week": w, "targets": 8, "receptions": 6, "receiving_yards": 80,
             "receiving_tds": 0, "carries": 0} for w in (1, 2)]


def _engine():
    eng = projections.ProjectionEngine.__new__(projections.ProjectionEngine)
    eng.db = None
    eng.values = types.SimpleNamespace(
        get_values=AsyncMock(return_value={"list": [], "source": "test"}),
        lookup=lambda *a, **k: None)
    eng.defense = types.SimpleNamespace(
        fetch_defense_rankings=AsyncMock(return_value={}),
        get_matchup_difficulty=lambda *a, **k: {"matchup_tier": "neutral"})
    eng.vegas = types.SimpleNamespace(fetch_current_lines=AsyncMock(return_value={}),
                                      get_game_lines=lambda *a, **k: {"is_fallback": True})
    return eng


def _logs():
    return {"x1": {"player_id": "x1", "name": "Wide Receiver", "position": "WR",
                   "team": "BUF", "games": _games()}}


PLAYERS = [
    {"name": "Wide Receiver", "position": "WR", "team": "BUF", "opponent": "MIA", "player_id": "1"},
    {"name": "Benched Quarterback", "position": "QB", "team": "BUF", "opponent": "MIA",
     "player_id": "2"},
    {"name": "Not Listed", "position": "TE", "team": "BUF", "opponent": "MIA"},
    {"name": "Bye Guy", "position": "WR", "team": "KC", "opponent": "BYE"},
]


@pytest.fixture
def sleeper_week(monkeypatch):
    monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_payload())))
    monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_logs()))


class TestEngine:
    @pytest.mark.asyncio
    async def test_project_many_is_sleeper_first(self, sleeper_week):
        out = await _engine().project_many(PLAYERS, scoring="ppr", season=2026, week=3)
        wr, qb, te, bye = out["projections"]

        # Ours: two games of opportunity regressed toward the unranked bucket.
        assert wr["breakdown"]["base_source"] == "opportunity"
        assert wr["breakdown"]["prior_weight"] == 0.5
        assert wr["projection_source"] == "sleeper_blend"
        assert wr["sleeper_projection"] == 11.0
        assert wr["projected_points"] == round(0.25 * wr["model_projection"] + 0.75 * 11.0, 1)
        assert wr["floor"] == round(wr["projected_points"] * (1 - projections._VOLATILITY["WR"]), 1)
        assert wr["blend_weights"] == {"model": 0.25, "sleeper": 0.75}

        # Listed without points: pulled down to a quarter of ours.
        assert qb["sleeper_projection"] == 0.0 and qb["projection_source"] == "sleeper_blend"
        assert qb["projected_points"] == round(0.25 * qb["model_projection"], 1)

        # Not in Sleeper's list at all: our model alone, and said so.
        assert te["projection_source"] == "model_only"
        assert te["projected_points"] == te["model_projection"] and te["warning"]
        assert te["blend_weights"] == {"model": 1.0, "sleeper": 0.0}

        assert bye["projection_source"] == "bye" and bye["projected_points"] == 0.0
        assert out["projection_sources"] == {"sleeper_blend": 2, "model_only": 1, "bye": 1}
        assert out["sleeper_projections_active"] is True
        assert any("Not Listed" in w for w in out["warnings"])

    @pytest.mark.asyncio
    async def test_outage_falls_back_to_the_model_with_a_warning(self, monkeypatch):
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_logs()))
        out = await _engine().project_many(PLAYERS[:1], scoring="ppr", season=2026, week=3)
        wr = out["projections"][0]
        assert wr["projection_source"] == "model_only"
        assert wr["breakdown"]["sleeper_status"] == "unavailable"
        assert out["sleeper_projections_active"] is False
        assert "unavailable" in out["warnings"][0]

    @pytest.mark.asyncio
    async def test_out_is_zero_even_when_sleeper_projects_him(self, sleeper_week):
        hurt = {**PLAYERS[0], "injury": {"status": "Out"}}
        out = await _engine().project_many([hurt], scoring="ppr", season=2026, week=3)
        p = out["projections"][0]
        assert p["projected_points"] == p["floor"] == p["ceiling"] == 0.0
        assert p["sleeper_projection"] == 11.0


class TestOneNumberEverywhere:
    @pytest.mark.asyncio
    async def test_start_sit_and_project_players_agree(self, sleeper_week):
        engine = _engine()
        with patch.object(projections, "get_projection_engine", return_value=engine):
            pp = await projections.project_players([PLAYERS[0]], scoring="ppr",
                                                   season=2026, week=3)
            with patch("nfl_mcp.injury_match.lookup_injury", return_value=None), \
                 patch("nfl_mcp.practice_reports.lookup_practice", return_value=None):
                opt = lo.LineupOptimizer.__new__(lo.LineupOptimizer)
                opt.db, opt.defense_analyzer, opt.auto_project = None, None, True
                a = await opt.analyze_player(
                    player_name="Wide Receiver", player_id="1", position="WR", team="BUF",
                    opponent="MIA", scoring="ppr", season=2026, week=3)
        proj = pp["projections"][0]
        assert a.projected_points == proj["projected_points"]
        assert a.projection_source == proj["projection_source"] == "sleeper_blend"
        assert a.model_projection == proj["model_projection"]
        # The disagreement check compares our model with Sleeper, not the blend.
        assert proj["gap"] == round(proj["model_projection"] - 11.0, 1)
        assert a.to_dict()["projection_source"] == "sleeper_blend"
