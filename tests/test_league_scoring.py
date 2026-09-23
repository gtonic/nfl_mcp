"""The league's full Sleeper scoring_settings, not just `rec`.

Only the reception value used to reach the projections; pass TD value, INT,
fumbles, TE premium, bonuses, K distance and DEF points-allowed tiers were all
hard-coded, so a 6-point-passing-TD or TE-premium league got stock advice.
"""
import types
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import opportunity_tools
from nfl_mcp.opportunity import project_opportunity
from nfl_mcp.player_values import scoring_to_ppr
from nfl_mcp.projections import ProjectionEngine, base_ppg
from nfl_mcp.scoring import (
    SLEEPER_DEFAULTS,
    LeagueScoring,
    ScoringModel,
    league_scoring,
    resolve_scoring,
)


def _league(**overrides):
    """A complete Sleeper league block: half PPR at Sleeper defaults + overrides."""
    return {**SLEEPER_DEFAULTS, "rec": 0.5, **overrides}


def _games(weeks=4, **stats):
    return [{**stats, "week": w} for w in range(1, weeks + 1)]


TE_GAMES = _games(targets=7, receptions=5, receiving_yards=55, receiving_tds=0.4)
WR_GAMES = TE_GAMES
QB_GAMES = _games(attempts=34, completions=22, passing_yards=250, passing_tds=1.8,
                  interceptions=0.7, carries=3, rushing_yards=12)


class TestModel:
    def test_presets_are_sleeper_defaults_at_the_reception_value(self):
        m = resolve_scoring("half_ppr")
        assert m.rec == 0.5 and m.w("pass_td") == 4.0 and m.w("pass_int") == -1.0
        assert resolve_scoring("0.5").rec == 0.5
        assert resolve_scoring("standard").rec == 0.0
        assert resolve_scoring(None).rec == 1.0
        assert m.non_default() == {}

    def test_a_league_block_scores_omitted_keys_as_zero(self):
        # Sleeper leaves zero-valued keys out; a missing fgm_40_49 is not 4 pts.
        settings = _league()
        del settings["fgm_40_49"]
        m = ScoringModel.from_settings(settings)
        assert m.source == "league"
        assert m.w("fgm_40_49") == 0.0

    def test_a_partial_dict_is_read_as_overrides(self):
        m = ScoringModel.from_settings({"rec": 0.5, "bonus_rec_te": 0.5})
        assert m.source == "overrides"
        assert m.w("pass_td") == 4.0
        assert m.reception_value("TE") == 1.0
        assert m.reception_value("WR") == 0.5

    def test_league_scoring_is_a_string_that_carries_the_model(self):
        carrier = league_scoring({"name": "L", "scoring_settings": _league(pass_td=6.0)})
        assert isinstance(carrier, str) and carrier == "0.5"
        assert scoring_to_ppr(carrier) == 0.5
        assert resolve_scoring(carrier).w("pass_td") == 6.0
        # Plain string operations degrade to the preset, never to garbage.
        assert resolve_scoring(str(carrier)).w("pass_td") == 4.0

    def test_summary_reports_the_differences_and_what_is_not_priced(self):
        m = ScoringModel.from_settings(_league(pass_td=6.0, kr_yd=0.04), name="L")
        s = m.summary()
        assert s["label"] == "half_ppr" and s["league"] == "L"
        assert s["non_default"]["pass_td"] == {"league": 6.0, "default": 4.0}
        assert "kr_yd" in s["unmodelled"]

    def test_yardage_bonuses_do_not_stack(self):
        m = ScoringModel.from_settings(_league(bonus_rec_yd_100=3.0, bonus_rec_yd_200=5.0))
        base = ScoringModel.from_settings(_league())
        assert m.rec_points({"receiving_yards": 150}) - base.rec_points({"receiving_yards": 150}) == 3.0
        assert m.rec_points({"receiving_yards": 210}) - base.rec_points({"receiving_yards": 210}) == 5.0

    def test_fumbles_and_first_downs_are_priced(self):
        m = ScoringModel.from_settings(_league(rush_fd=0.5))
        g = {"carries": 15, "rushing_yards": 70, "rushing_first_downs": 4,
             "rushing_fumbles_lost": 1}
        assert m.rush_points(g) == pytest.approx(7.0 + 2.0 - 2.0)


class TestTePremium:
    def test_raises_the_te_opportunity_projection(self):
        stock = ScoringModel.from_settings(_league())
        premium = ScoringModel.from_settings(_league(bonus_rec_te=0.5))
        te_stock = project_opportunity(TE_GAMES, "TE", scoring=stock)
        te_prem = project_opportunity(TE_GAMES, "TE", scoring=premium)
        # ~5 catches a game at +0.5 each.
        assert te_prem - te_stock == pytest.approx(2.5, abs=0.4)
        # Receivers are untouched.
        assert (project_opportunity(WR_GAMES, "WR", scoring=premium)
                == pytest.approx(project_opportunity(WR_GAMES, "WR", scoring=stock)))

    def test_raises_the_te_rank_bucket(self):
        premium = ScoringModel.from_settings(_league(bonus_rec_te=0.5))
        assert base_ppg("TE", 2, scoring=premium) > base_ppg("TE", 2, 0.5)
        assert base_ppg("WR", 2, scoring=premium) == base_ppg("WR", 2, 0.5)


class TestSixPointPassTd:
    def test_raises_the_qb_opportunity_projection(self):
        four = ScoringModel.from_settings(_league())
        six = ScoringModel.from_settings(_league(pass_td=6.0))
        gain = (project_opportunity(QB_GAMES, "QB", scoring=six)
                - project_opportunity(QB_GAMES, "QB", scoring=four))
        # 1.8 TD a game at +2 each, a little shrunk toward the prior.
        assert gain == pytest.approx(3.6, abs=0.5)

    def test_raises_the_qb_rank_bucket_and_market_value(self):
        six = ScoringModel.from_settings(_league(pass_td=6.0))
        assert base_ppg("QB", 5, scoring=six) > base_ppg("QB", 5, 0.5)
        assert six.value_multiplier("QB") > 1.1
        assert six.value_multiplier("RB") == 1.0


class TestFormatOrderingUnchanged:
    @pytest.mark.parametrize("extra", [{}, {"pass_td": 6.0}, {"bonus_rec_te": 0.5},
                                       {"rec_fd": 0.5, "rush_fd": 0.5}])
    def test_standard_below_half_below_full(self, extra):
        models = [ScoringModel.from_settings(_league(rec=r, **extra)) for r in (0.0, 0.5, 1.0)]
        for pos, games in (("WR", WR_GAMES), ("TE", TE_GAMES)):
            std, half, full = (project_opportunity(games, pos, scoring=m) for m in models)
            assert std < half < full
            std, half, full = (base_ppg(pos, 10, scoring=m) for m in models)
            assert std < half < full

    def test_preset_models_reproduce_the_ppr_only_numbers(self):
        for ppr in (0.0, 0.5, 1.0):
            m = ScoringModel.preset(ppr)
            assert project_opportunity(WR_GAMES, "WR", ppr=ppr) == project_opportunity(
                WR_GAMES, "WR", scoring=m)
            for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
                assert base_ppg(pos, 8, ppr) == base_ppg(pos, 8, scoring=m)


class TestDefenseAndKicker:
    def test_points_allowed_tiers_move_the_defense(self):
        default = ScoringModel.from_settings(_league())
        rich = ScoringModel.from_settings(_league(pts_allow_7_13=8.0, pts_allow_14_20=5.0))
        flat = ScoringModel.from_settings(_league(**{k: 0.0 for k in SLEEPER_DEFAULTS
                                                     if k.startswith("pts_allow")}))
        assert default.defense_scale(18.0) == 1.0
        assert rich.defense_scale(18.0) > 1.15
        assert flat.defense_scale(18.0) < 1.0
        # The tiers matter most where the defense is expected to hold them.
        assert rich.defense_scale(16.0) > rich.defense_scale(30.0)

    def test_default_tiers_track_the_calibrated_baseline(self):
        from nfl_mcp.projections import defense_base
        d = ScoringModel.preset(0.5)
        for total in (16.0, 22.0, 28.0):
            assert d.expected_defense_points(total) == pytest.approx(defense_base(total), abs=1.2)

    def test_kicker_distance_values_and_unscored_kickers(self):
        assert ScoringModel.preset(0.5).kicker_scale() == 1.0
        long_range = ScoringModel.from_settings(_league(fgm_50_59=7.0, fgm_60p=9.0))
        assert long_range.kicker_scale() > 1.0
        no_k = _league(**{k: 0.0 for k in SLEEPER_DEFAULTS if k.startswith(("fgm", "xp", "fgmiss"))})
        assert ScoringModel.from_settings(no_k).kicker_scale() == 0.0

    def test_single_50_plus_key(self):
        settings = _league(fgm_50p=5.0)
        del settings["fgm_50_59"], settings["fgm_60p"]
        m = ScoringModel.from_settings(settings)
        assert m.kicker_points({"fg_made_50_59": 1, "fg_made_60_": 1}) == 10.0
        assert "fgm_50_59" not in m.non_default()


def _engine():
    eng = ProjectionEngine.__new__(ProjectionEngine)
    eng.values = types.SimpleNamespace(lookup=lambda *a, **k: {"position_rank": 8})
    eng.defense = types.SimpleNamespace(get_matchup_difficulty=lambda *a, **k: {"matchup_tier": "neutral"})
    eng.vegas = types.SimpleNamespace(get_game_lines=lambda *a, **k: {
        "is_fallback": False, "home_team": "BUF", "home_implied_total": 24.0,
        "away_implied_total": 17.0})
    return eng


class TestEngine:
    def test_def_uses_the_league_tiers(self):
        eng = _engine()
        dst = {"name": "Bills", "position": "DEF", "team": "BUF", "opponent": "MIA"}
        stock = eng._project_one(dst, {}, {}, {}, None, 6, 0.5,
                                 scoring_model=ScoringModel.preset(0.5))
        rich = eng._project_one(dst, {}, {}, {}, None, 6, 0.5, scoring_model=ScoringModel.from_settings(
            _league(pts_allow_14_20=6.0, pts_allow_7_13=8.0)))
        assert rich["projected_points"] > stock["projected_points"]

    def test_kicker_in_a_league_without_kicker_scoring_is_zero(self):
        eng = _engine()
        k = {"name": "Kicker", "position": "K", "team": "BUF", "opponent": "MIA"}
        no_k = ScoringModel.from_settings(
            _league(**{key: 0.0 for key in SLEEPER_DEFAULTS if key.startswith(("fgm", "xp", "fgmiss"))}))
        assert eng._project_one(k, {}, {}, {}, None, 6, 0.5, scoring_model=no_k)["projected_points"] == 0.0

    def test_opportunity_path_prices_the_te_premium(self):
        eng = _engine()
        idx = opportunity_tools.build_name_index({"t": {
            "player_id": "t", "name": "Tight End", "position": "TE", "team": "BUF",
            "games": TE_GAMES}})
        te = {"name": "Tight End", "position": "TE", "team": "BUF", "opponent": "MIA"}
        stock = eng._project_one(te, {}, {}, {}, idx, 6, 0.5, scoring_model=ScoringModel.preset(0.5))
        prem = eng._project_one(te, {}, {}, {}, idx, 6, 0.5, scoring_model=ScoringModel.from_settings(
            _league(bonus_rec_te=0.5)))
        assert stock["breakdown"]["base_source"] == "opportunity"
        assert prem["projected_points"] > stock["projected_points"]

    @pytest.mark.asyncio
    async def test_project_many_reports_scoring_used(self):
        async def _v(*a, **k):
            return {"source": "test"}

        async def _empty(*a, **k):
            return {}
        eng = _engine()
        eng.values = types.SimpleNamespace(lookup=lambda *a, **k: None, get_values=_v)
        eng.defense = types.SimpleNamespace(fetch_defense_rankings=_empty,
                                            get_matchup_difficulty=lambda *a, **k: {})
        eng.vegas = types.SimpleNamespace(fetch_current_lines=_empty,
                                          get_game_lines=lambda *a, **k: {"is_fallback": True})
        carrier = league_scoring({"name": "L", "scoring_settings": _league(pass_td=6.0)})
        out = await eng.project_many(
            [{"name": "A", "position": "QB", "team": "BUF", "opponent": "MIA"}], scoring=carrier)
        assert out["scoring"] == "0.5" and out["ppr"] == 0.5
        assert out["scoring_used"]["non_default"]["pass_td"]["league"] == 6.0
        assert out["projections"][0]["projected_points"] > base_ppg("QB", None, 0.5)


class TestStartSitLeagueScoring:
    @pytest.mark.asyncio
    async def test_league_id_brings_the_full_settings(self):
        from nfl_mcp.lineup_optimizer_tools import _league_scoring
        league = {"league": {"name": "L", "total_rosters": 10,
                             "scoring_settings": _league(bonus_rec_te=0.5)}}
        with patch("nfl_mcp.sleeper_tools.get_league", new=AsyncMock(return_value=league)):
            scoring, teams, source = await _league_scoring("123", None)
            same, _, _ = await _league_scoring("123", "half_ppr")
            other, _, _ = await _league_scoring("123", "ppr")
        assert isinstance(scoring, LeagueScoring) and source == "league" and teams == 10
        assert resolve_scoring(scoring).w("bonus_rec_te") == 0.5
        assert isinstance(same, LeagueScoring)       # same PPR: keep the settings
        assert not isinstance(other, LeagueScoring)  # explicit other format wins


class TestGameLogColumns:
    def test_current_nflverse_interception_column_is_read(self):
        csv_text = (
            "season_type,position,player_id,player_display_name,team,week,attempts,"
            "passing_interceptions,sacks_suffered,rushing_first_downs\n"
            "REG,QB,q1,Quarterback,BUF,1,30,2,3,1\n"
        )
        game = opportunity_tools.parse_game_logs(csv_text)["q1"]["games"][0]
        assert game["interceptions"] == 2.0
        assert game["sacks_suffered"] == 3.0
        assert game["rushing_first_downs"] == 1.0
