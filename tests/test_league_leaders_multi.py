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

@pytest.mark.asyncio
async def test_get_league_leaders_multi(monkeypatch):
    sample = {
        "categories": [
            {"name": "passingYards", "displayName": "Passing Yards", "leaders": [{"leaders": [
                {"rank": 1, "value": 3500, "athlete": {"id": "1", "displayName": "QB One"}, "team": {"id": "10", "abbreviation": "AAA"}}
            ]}]},
            {"name": "rushingYards", "displayName": "Rushing Yards", "leaders": [{"leaders": [
                {"rank": 1, "value": 1200, "athlete": {"id": "3", "displayName": "RB One"}, "team": {"id": "12", "abbreviation": "CCC"}}
            ]}]}        
        ]
    }
    monkeypatch.setattr(nfl_tools, "create_http_client", lambda *a, **k: DummyClient(sample))
    result = await nfl_tools.get_league_leaders(category="pass, rush", season=2025, season_type=2)
    assert result['success'] is True
    assert 'categories' in result
    cats = {c['category']: c for c in result['categories']}
    assert 'pass' in cats and 'rush' in cats
    assert cats['pass']['players'][0]['athlete_name'] == 'QB One'
    assert cats['rush']['players'][0]['athlete_name'] == 'RB One'
