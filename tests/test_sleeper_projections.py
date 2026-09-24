"""Sleeper projections as a second opinion (network mocked)."""
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nfl_mcp import sleeper_projections as sp
from nfl_mcp.scoring import ScoringModel

WR_LINE = {"rec": 6.0, "rec_yd": 80.0, "rec_td": 0.5, "rec_tgt": 9.0, "fum_lost": 0.05,
           "bonus_rec_wr": 6.0, "pts_ppr": 17.9, "adp_dd_ppr": 20.0}


def _named_payload():
    return [
        {"player_id": "6794", "team": "MIN", "opponent": "GB",
         "player": {"first_name": "Justin", "last_name": "Jefferson", "position": "WR", "team": "MIN"},
         "stats": WR_LINE},
        {"player_id": "WAS", "team": "WAS", "opponent": "NYG",
         "player": {"first_name": "Washington", "last_name": "Commanders", "position": "DEF"},
         "stats": {"sack": 3.0, "int": 1.0, "pts_allow": 17.0, "pts_allow_14_20": 1.0, "pts_ppr": 9.0}},
        {"player_id": "999", "player": {"position": "WR"}, "stats": {"adp_dd_ppr": 1000.0}},
    ]


class TestPricing:
    def test_each_stat_at_the_leagues_value(self):
        half = ScoringModel.preset(0.5)
        # 6*0.5 + 8 + 3 - 0.1 = 13.9 (targets, adp and pts_* are not scoring keys)
        assert sp.price_stats(WR_LINE, half) == 13.9

    def test_te_premium_is_priced_from_the_bonus_stat(self):
        model = ScoringModel.from_settings({"bonus_rec_te": 0.5})
        assert sp.price_stats({"rec": 4.0, "bonus_rec_te": 4.0}, model) == 6.0

    def test_long_field_goals_use_the_50_59_value_without_a_50p_key(self):
        league = {"pass_yd": 0.04, "pass_td": 4, "rush_yd": 0.1, "rush_td": 6, "rec_yd": 0.1,
                  "rec_td": 6, "fgm_40_49": 4, "fgm_50_59": 5, "fgmiss": -1, "xpm": 1}
        model = ScoringModel.from_settings(league)
        stats = {"fgm_40_49": 1.0, "fgm_50p": 1.0, "fgmiss_40_49": 0.5, "fgmiss_50p": 0.5, "xpm": 2.0}
        # 4 + 5 - 1 (flat miss over every projected miss) + 2
        assert sp.price_stats(stats, model) == 10.0

    def test_points_allowed_tier_is_a_probability_not_a_step(self):
        model = ScoringModel.preset(1.0)
        at_20 = sp.price_stats({"pts_allow": 20.4, "pts_allow_14_20": 1.0}, model)
        at_21 = sp.price_stats({"pts_allow": 20.6, "pts_allow_21_27": 1.0}, model)
        # A step would jump a full point between these; the smoothed tiers barely move.
        assert abs(at_20 - at_21) < 0.2


class TestIndexAndLookup:
    def test_named_payload_indexes_ids_names_and_defenses(self):
        index = sp._index(_named_payload())
        assert set(index["by_id"]) == {"6794", "WAS"}   # the empty line is dropped
        assert sp.lookup(index, name="Justin Jefferson", team="MIN")["player_id"] == "6794"
        # Sleeper's WAS is found under the canonical code and any spelling of it.
        assert sp.lookup(index, name="WSH", team="WSH", position="DEF")["player_id"] == "WAS"
        assert sp.lookup(index, team="WAS", position="DEF")["player_id"] == "WAS"

    def test_v1_payload_is_keyed_by_id_only(self):
        index = sp._index({"6794": WR_LINE, "KC": {"sack": 2.0, "pts_ppr": 8.0}})
        assert sp.lookup(index, player_id="6794")["stats"] is WR_LINE
        assert sp.lookup(index, team="KC", position="DEF")["team"] == "KC"

    def test_an_id_from_another_id_space_is_not_trusted(self):
        index = sp._index(_named_payload())
        assert sp.lookup(index, player_id="6794", name="Someone Else", team="DAL") is None


class TestSecondOpinion:
    @pytest.mark.parametrize("ours,theirs,flag", [
        (15.0, 10.0, True),    # 5 points
        (10.0, 7.0, True),     # 30%, 3 points
        (4.9, 3.5, False),     # 29% but only 1.4 points
        (2.0, 0.5, False),     # both too small to matter
        (12.0, 10.0, False),   # 17%
    ])
    def test_rule(self, ours, theirs, flag):
        assert sp.second_opinion(ours, theirs)["disagreement"] is flag

    def test_consensus_is_the_blend_and_gap_is_ours_minus_theirs(self):
        op = sp.second_opinion(12.0, 8.0)
        # 0.25 * 12 + 0.75 * 8
        assert op["consensus"] == 9.0 and op["gap"] == 4.0

    def test_a_ruled_out_player_keeps_a_zero_consensus(self):
        op = sp.second_opinion(0.0, 11.1, ruled_out=True)
        assert op["consensus"] == 0.0 and op["disagreement"]
        assert "ruled out" in op["disagreement_note"]

    def test_no_sleeper_projection(self):
        assert sp.second_opinion(10.0, None) == {
            "sleeper_projection": None, "consensus": None, "disagreement": False, "gap": None}


class TestAnnotate:
    @pytest.mark.asyncio
    async def test_projections_are_annotated_and_disagreements_listed(self, monkeypatch):
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_named_payload())))
        result = {"projections": [
            {"player": "Justin Jefferson", "position": "WR", "team": "MIN", "projected_points": 20.0,
             "breakdown": {"injury_mult": 1.0}},
            {"player": "WSH", "position": "DEF", "team": "WSH", "projected_points": 8.0},
            {"player": "Bye Guy", "position": "WR", "team": "KC", "projected_points": 0.0, "on_bye": True},
        ]}
        inputs = [{"player_id": "6794"}, {}, {}]
        await sp.attach(result, inputs, 2026, 3, "half_ppr")
        wr, dst, bye = result["projections"]
        assert wr["sleeper_projection"] == 13.9 and wr["disagreement"] and wr["gap"] == 6.1
        assert dst["sleeper_projection"] is not None
        assert bye["disagreement"] is False
        summary = result["sleeper_second_opinion"]
        assert summary["active"] and summary["disagreements"][0]["player"] == "Justin Jefferson"

    @pytest.mark.asyncio
    async def test_unavailable_feed_is_reported_not_raised(self):
        result = {"projections": [{"player": "X", "projected_points": 5.0}]}
        await sp.attach(result, [{}], 2026, 3, "ppr")   # conftest: feed is empty
        assert result["sleeper_second_opinion"]["active"] is False
        assert "sleeper_projection" not in result["projections"][0]

    @pytest.mark.asyncio
    async def test_fetch_prefers_the_named_endpoint_and_caches(self, monkeypatch):
        monkeypatch.undo()   # the real `_fetch`, with the HTTP client mocked
        resp = Mock(status_code=200)
        resp.json = Mock(return_value=_named_payload())
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        sp._cache.clear()
        with patch("nfl_mcp.sleeper_projections.create_http_client", return_value=client):
            first = await sp.fetch_week_projections(2026, 3)
            second = await sp.fetch_week_projections(2026, 3)
        assert "6794" in first["by_id"] and second is first
        assert client.get.call_count == 1
        url = client.get.call_args.args[0]
        assert url == "https://api.sleeper.app/projections/nfl/2026/3"
        sp._cache.clear()


class TestProjectPlayersHook:
    @pytest.mark.asyncio
    async def test_project_players_carries_the_second_opinion(self, monkeypatch):
        from nfl_mcp import projections
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_named_payload())))

        class _Engine:
            async def project_many(self, players, **kw):
                return {"projections": [{"player": "Justin Jefferson", "position": "WR", "team": "MIN",
                                         "projected_points": 14.0}]}

        with patch.object(projections, "get_projection_engine", return_value=_Engine()):
            out = await projections.project_players(
                [{"name": "Justin Jefferson", "position": "WR", "team": "MIN", "player_id": "6794"}],
                scoring="half_ppr", season=2026, week=3)
        p = out["projections"][0]
        assert p["projected_points"] == 14.0          # ours stays primary
        assert p["sleeper_projection"] == 13.9
        assert p["consensus"] == pytest.approx(13.95, abs=0.06)
        assert out["sleeper_second_opinion"]["matched"] == 1
