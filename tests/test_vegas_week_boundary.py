"""The sportsbook publishes several weeks at once; lines must not mix them."""
import pytest

from nfl_mcp import vegas_tools


def _game(away, home, kickoff, total=45.0):
    """Minimal Odds-API game payload with one bookmaker."""
    return {
        "home_team": vegas_tools.ABBREVIATION_TO_FULL[home],
        "away_team": vegas_tools.ABBREVIATION_TO_FULL[away],
        "commence_time": kickoff,
        "bookmakers": [{
            "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": vegas_tools.ABBREVIATION_TO_FULL[home], "point": -3.0},
                    {"name": vegas_tools.ABBREVIATION_TO_FULL[away], "point": 3.0},
                ]},
                {"key": "totals", "outcomes": [{"name": "Over", "point": total}]},
            ]
        }],
    }


class _Resp:
    status_code = 200
    headers = {"x-requests-remaining": "100"}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


@pytest.fixture
def analyzer(monkeypatch):
    a = vegas_tools.VegasLinesAnalyzer(api_key="test-key")
    # Nothing has kicked off, so no game is filtered as live.
    monkeypatch.setattr(a, "_has_kicked_off", lambda ts: False)
    return a


class TestTeamIndexPicksEarliestGame:
    @pytest.mark.asyncio
    async def test_team_resolves_to_its_next_game_not_the_last_published(
        self, analyzer, monkeypatch
    ):
        # WSH plays DAL in week 2 and SEA in week 3. Blind assignment made the
        # week-3 game win for every team appearing in both, which silently fed
        # the wrong opponent and implied total into every projection.
        payload = [
            _game("WSH", "DAL", "2026-09-20T20:25:00Z", total=50.4),
            _game("SEA", "WSH", "2026-09-27T17:00:00Z", total=41.8),
        ]

        class Client:
            async def get(self, url, **kwargs):
                return _Resp(payload)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        # The analyzer builds its own httpx client inline.
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: Client())

        lines = await analyzer.fetch_current_lines()

        assert lines["WSH"]["commence_time"] == "2026-09-20T20:25:00Z"
        assert lines["WSH"]["home_team"] == "DAL"
        assert lines["WSH"]["total"] == 50.4
        # Both games remain individually addressable.
        assert "WSH@DAL" in lines and "SEA@WSH" in lines

    @pytest.mark.asyncio
    async def test_order_of_the_feed_does_not_matter(self, analyzer, monkeypatch):
        payload = [
            _game("SEA", "WSH", "2026-09-27T17:00:00Z"),
            _game("WSH", "DAL", "2026-09-20T20:25:00Z"),
        ]

        class Client:
            async def get(self, url, **kwargs):
                return _Resp(payload)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        # The analyzer builds its own httpx client inline.
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: Client())

        lines = await analyzer.fetch_current_lines()
        assert lines["WSH"]["commence_time"] == "2026-09-20T20:25:00Z"


class TestWeekLabellingAndFilter:
    @pytest.mark.asyncio
    async def test_games_carry_week_and_filter_applies(self, monkeypatch):
        index = {("DAL", "2026-09-20"): 2, ("WSH", "2026-09-27"): 3}
        monkeypatch.setattr(vegas_tools, "_build_week_index", lambda season: index)

        async def fake_lines():
            return {
                "WSH@DAL": {"home_team": "DAL", "away_team": "WSH", "total": 50.4,
                            "commence_time": "2026-09-20T20:25:00Z"},
                "SEA@WSH": {"home_team": "WSH", "away_team": "SEA", "total": 41.8,
                            "commence_time": "2026-09-27T17:00:00Z"},
            }

        a = vegas_tools.get_vegas_analyzer()
        monkeypatch.setattr(a, "fetch_current_lines", fake_lines)

        every = await vegas_tools.get_vegas_lines()
        assert sorted(g["week"] for g in every["games"]) == [2, 3]

        only_two = await vegas_tools.get_vegas_lines(week=2)
        assert [g["week"] for g in only_two["games"]] == [2]
        assert only_two["games"][0]["home_team"] == "DAL"

    @pytest.mark.asyncio
    async def test_unresolvable_week_is_kept_not_dropped(self, monkeypatch):
        # A cold schedule cache must not silently empty the slate.
        monkeypatch.setattr(vegas_tools, "_build_week_index", lambda season: {})

        async def fake_lines():
            return {"WSH@DAL": {"home_team": "DAL", "away_team": "WSH", "total": 50.4,
                                "commence_time": "2026-09-20T20:25:00Z"}}

        a = vegas_tools.get_vegas_analyzer()
        monkeypatch.setattr(a, "fetch_current_lines", fake_lines)

        result = await vegas_tools.get_vegas_lines(week=2)
        assert len(result["games"]) == 1
        assert result["games"][0]["week"] is None
