"""The weekly briefing joins six sources; the joins are what can go wrong."""
import pytest

from nfl_mcp import briefing_tools


class TestScoringLabel:
    @pytest.mark.parametrize("rec,expected", [
        (1.0, "ppr"), (0.5, "half_ppr"), (0, "standard"), (None, "ppr"),
    ])
    def test_reception_value_maps_to_label(self, rec, expected):
        # Guessing this is how a half-PPR league silently gets full-PPR advice.
        assert briefing_tools._scoring_label({"scoring_settings": {"rec": rec}}) == expected

    def test_missing_settings_defaults_to_ppr(self):
        assert briefing_tools._scoring_label({}) == "ppr"


class TestSlotParsing:
    def test_bench_and_reserve_slots_are_excluded(self):
        positions = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX",
                     "K", "DEF", "BN", "BN", "BN", "IR", "TAXI"]
        slots = briefing_tools._slots_from_positions(positions)
        assert slots == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "DEF": 1}

    def test_sleeper_flex_aliases_are_normalized(self):
        slots = briefing_tools._slots_from_positions(["SUPER_FLEX", "WRRB_FLEX", "REC_FLEX"])
        assert slots == {"SUPERFLEX": 1, "FLEX": 2}


class TestBuildPlayer:
    def _athlete(self, **kw):
        base = {"full_name": "Jayden Daniels", "position": "QB", "team_id": "WAS"}
        base.update(kw)
        return {"p1": base}

    def test_team_code_is_canonicalized_before_the_opponent_lookup(self):
        # The athlete row says WAS, the schedule says WSH. Without
        # normalization the player looks like he is on a bye.
        player = briefing_tools._build_player(
            "p1", self._athlete(), {"WSH": "DAL"}, {}, {}
        )
        assert player is not None
        assert player["team"] == "WSH"
        assert player["opponent"] == "DAL"

    def test_bye_week_player_is_skipped(self):
        assert briefing_tools._build_player("p1", self._athlete(), {}, {}, {}) is None

    def test_team_defense_is_not_projected(self):
        # A nameless 0-point candidate would drag every lineup total down.
        rows = {"SF": {"full_name": "", "position": "DEF", "team_id": "SF"}}
        assert briefing_tools._build_player("SF", rows, {"SF": "MIA"}, {}, {}) is None

    def test_weather_and_usage_are_attached_when_known(self):
        weather = {"WSH": {"wind_mph": 12.0, "precip_in": 0.5, "temp_f": 50, "is_dome": False}}
        usage = {"p1": {"snap_share": 88.5}}
        player = briefing_tools._build_player(
            "p1", self._athlete(), {"WSH": "DAL"}, weather, usage
        )
        assert player["weather"]["precip_in"] == 0.5
        assert player["usage"]["snap_percentage"] == 88.5

    def test_absent_context_is_simply_omitted(self):
        player = briefing_tools._build_player("p1", self._athlete(), {"WSH": "DAL"}, {}, {})
        assert "weather" not in player and "usage" not in player

    def test_unknown_player_id_is_skipped(self):
        assert briefing_tools._build_player("nope", {}, {"WSH": "DAL"}, {}, {}) is None


class TestIdentifyingTheRoster:
    @pytest.mark.asyncio
    async def test_missing_identifier_is_refused_not_guessed(self, monkeypatch):
        async def fake_state():
            return {"nfl_state": {"week": 2, "season": "2026"}}

        monkeypatch.setattr("nfl_mcp.sleeper_tools.get_nfl_state", fake_state)
        result = await briefing_tools.get_weekly_briefing(league_id="L1")
        assert result["success"] is False
        assert "roster_id" in result["error"]
