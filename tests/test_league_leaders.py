import types
import pytest
from nfl_mcp import nfl_tools

class DummyResponse:
    def __init__(self, json_data):
        self._json = json_data
    def raise_for_status(self):
        return
    def json(self):
        return self._json

class DummyClient:
    def __init__(self, json_data):
        self._json = json_data
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        return False
    async def get(self, url, headers=None):
        return DummyResponse(self._json)

# Monkeypatch create_http_client for this test
@pytest.mark.asyncio
async def test_get_league_leaders_basic(monkeypatch):
    sample = {
        "categories": [
            {
                "name": "passingYards",
                "displayName": "Passing Yards",
                "leaders": [
                    {"leaders": [
                        {"rank": 1, "value": 3500, "athlete": {"id": "1", "displayName": "QB One"}, "team": {"id": "10", "abbreviation": "AAA"}},
                        {"rank": 2, "value": 3400, "athlete": {"id": "2", "displayName": "QB Two"}, "team": {"id": "11", "abbreviation": "BBB"}},
                    ]}
                ]
            }
        ]
    }
    async def dummy_client_factory(*args, **kwargs):
        return DummyClient(sample)
    monkeypatch.setattr(nfl_tools, "create_http_client", lambda *a, **k: DummyClient(sample))
    result = await nfl_tools.get_league_leaders(category="pass", season=2025, season_type=2)
    assert result['success'] is True
    assert result['category'] == 'pass'
    assert result['players_count'] == 2
    assert len(result['players']) == 2
    assert result['players'][0]['athlete_name'] == 'QB One'
