"""The draft handcuff index joins two sources that spell names differently."""
import pytest

from nfl_mcp.draft_tools import _handcuff_index, _norm_name


class TestNameKeyBridgesTheSources:
    @pytest.mark.parametrize("espn,sleeper", [
        ("James Cook III", "James Cook"),
        ("Kenneth Walker III", "Kenneth Walker"),
        ("Marvin Harrison Jr.", "Marvin Harrison Jr"),
        ("De'Von Achane", "DeVon Achane"),
    ])
    def test_suffixes_and_punctuation_are_bridged(self, espn, sleeper):
        # A raw `starter not in names` compare missed all of these. The tell
        # was the asymmetry: _norm_name was applied to the backup but not the
        # starter.
        assert espn != sleeper
        assert _norm_name(espn) == _norm_name(sleeper)


class TestHandcuffIndex:
    @pytest.mark.asyncio
    async def test_starter_with_a_suffix_still_resolves(self, monkeypatch):
        chart = {"depth_chart": [
            {"position": "RB", "players": ["James Cook III", "Ray Davis", "Ty Johnson"]},
        ]}

        async def fake_chart(team):
            return chart

        monkeypatch.setattr("nfl_mcp.draft_tools._cached_depth_chart", fake_chart)

        # Sleeper spelling, without the suffix.
        index = await _handcuff_index([{"name": "James Cook", "team": "BUF", "position": "RB"}])

        assert index, "handcuff index came back empty"
        assert index[_norm_name("Ray Davis")] == "James Cook"
        assert index[_norm_name("Ty Johnson")] == "James Cook"

    @pytest.mark.asyncio
    async def test_unknown_starter_yields_an_empty_index(self, monkeypatch):
        async def fake_chart(team):
            return {"depth_chart": [{"position": "RB", "players": ["Someone Else"]}]}

        monkeypatch.setattr("nfl_mcp.draft_tools._cached_depth_chart", fake_chart)
        index = await _handcuff_index([{"name": "Traded Away", "team": "BUF", "position": "RB"}])
        assert index == {}

    @pytest.mark.asyncio
    async def test_only_the_next_two_backs_are_indexed(self, monkeypatch):
        async def fake_chart(team):
            return {"depth_chart": [{"position": "RB", "players": [
                "Starter Guy", "Backup One", "Backup Two", "Backup Three",
            ]}]}

        monkeypatch.setattr("nfl_mcp.draft_tools._cached_depth_chart", fake_chart)
        index = await _handcuff_index([{"name": "Starter Guy", "team": "BUF", "position": "RB"}])
        assert _norm_name("Backup Three") not in index
        assert len(index) == 2
