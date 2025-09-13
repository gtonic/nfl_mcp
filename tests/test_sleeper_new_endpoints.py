import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from nfl_mcp import sleeper_tools


@pytest.mark.asyncio
async def test_get_user_success():
    with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_client_factory:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"user_id": "123", "username": "tester"}
        mock_resp.raise_for_status.return_value = None
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client_factory.return_value = mock_client
        result = await sleeper_tools.get_user("tester")
        assert result["success"] is True
        assert result["user"]["user_id"] == "123"


@pytest.mark.asyncio
async def test_get_user_leagues_success():
    with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_client_factory:
        leagues_data = [{"league_id": "L1"}, {"league_id": "L2"}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = leagues_data
        mock_resp.raise_for_status.return_value = None
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client_factory.return_value = mock_client
        result = await sleeper_tools.get_user_leagues("123", 2025)
        assert result["success"] is True
        assert result["count"] == 2


@pytest.mark.asyncio
async def test_get_league_drafts_success():
    with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_client_factory:
        data = [{"draft_id": "D1"}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.raise_for_status.return_value = None
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client_factory.return_value = mock_client
        result = await sleeper_tools.get_league_drafts("L1")
        assert result["success"] is True and result["count"] == 1


@pytest.mark.asyncio
async def test_get_draft_and_picks_success():
    with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_client_factory:
        # First call draft, second picks, third traded picks
        draft_resp = MagicMock(); draft_resp.json.return_value = {"draft_id": "D1"}; draft_resp.raise_for_status.return_value = None
        picks_resp = MagicMock(); picks_resp.json.return_value = [{"player_id": "111"}]; picks_resp.raise_for_status.return_value = None
        traded_resp = MagicMock(); traded_resp.json.return_value = [{"season": "2025", "round": 1}]; traded_resp.raise_for_status.return_value = None
        mock_client = AsyncMock()
        mock_client.get.side_effect = [draft_resp, picks_resp, traded_resp]
        mock_client.__aenter__.return_value = mock_client
        mock_client_factory.return_value = mock_client
        draft = await sleeper_tools.get_draft("D1")
        picks = await sleeper_tools.get_draft_picks("D1")
        traded = await sleeper_tools.get_draft_traded_picks("D1")
        assert draft["draft"]["draft_id"] == "D1"
        assert picks["count"] == 1
        assert traded["count"] == 1


@pytest.mark.asyncio
async def test_fetch_all_players_cache_behavior(monkeypatch):
    # Reset cache
    sleeper_tools._PLAYERS_CACHE["data"] = None
    sleeper_tools._PLAYERS_CACHE["fetched_at"] = 0
    with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_client_factory:
        players_map = {"1": {"player_id": "1"}, "2": {"player_id": "2"}}
        mock_resp = MagicMock(); mock_resp.json.return_value = players_map; mock_resp.raise_for_status.return_value = None
        mock_client = AsyncMock(); mock_client.get.return_value = mock_resp; mock_client.__aenter__.return_value = mock_client
        mock_client_factory.return_value = mock_client
        first = await sleeper_tools.fetch_all_players(force_refresh=True)
        second = await sleeper_tools.fetch_all_players(force_refresh=False)
        assert first["success"] is True and first["cached"] is False
        assert second["success"] is True and second["cached"] is True
        # network call only once
        assert mock_client.get.call_count == 1


@pytest.mark.asyncio
async def test_playoff_bracket_losers():
    with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_client_factory:
        mock_resp = MagicMock(); mock_resp.json.return_value = [{"r":1}]; mock_resp.raise_for_status.return_value=None
        mock_client = AsyncMock(); mock_client.get.return_value = mock_resp; mock_client.__aenter__.return_value = mock_client
        mock_client_factory.return_value = mock_client
        losers = await sleeper_tools.get_playoff_bracket("L1", bracket_type="losers")
        assert losers["success"] is True and losers["bracket_type"] == "losers"


@pytest.mark.asyncio
async def test_playoff_bracket_invalid_type():
    result = await sleeper_tools.get_playoff_bracket("L1", bracket_type="invalid")
    assert result["success"] is False
    assert "bracket_type" in result["error"].lower()


@pytest.mark.asyncio
async def test_transactions_require_week():
    result = await sleeper_tools.get_transactions("L1")  # no week/round
    assert result["success"] is False and "required" in result["error"].lower()


@pytest.mark.asyncio
async def test_transactions_round_alias():
    with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_client_factory:
        mock_resp = MagicMock(); mock_resp.json.return_value = [{"type":"trade"}]; mock_resp.raise_for_status.return_value=None
        mock_client = AsyncMock(); mock_client.get.return_value = mock_resp; mock_client.__aenter__.return_value = mock_client
        mock_client_factory.return_value = mock_client
        result = await sleeper_tools.get_transactions("L1", round=3)
        assert result["success"] is True and result["week"] == 3


@pytest.mark.asyncio
async def test_trending_players_structure():
    trending_payload = [
        {"player_id": "1001", "count": 42},
        {"player_id": "1002", "count": 10},
    ]
    with patch('nfl_mcp.sleeper_tools.create_http_client') as mock_client_factory:
        mock_resp = MagicMock(); mock_resp.json.return_value = trending_payload; mock_resp.raise_for_status.return_value=None
        mock_client = AsyncMock(); mock_client.get.return_value = mock_resp; mock_client.__aenter__.return_value = mock_client
        mock_client_factory.return_value = mock_client
        # Provide a lightweight stub NFLDatabase via direct parameter (bypasses internal import path)
        stub_db = MagicMock()
        stub_db.search_athletes_by_name.return_value = [1]
        stub_db.get_athlete_by_id.side_effect = lambda pid: {"player_id": pid, "full_name": f"Name {pid}"}
        result = await sleeper_tools.get_trending_players(stub_db, "add", 24, 10)
        assert result["success"] is True
        assert result["count"] == len(result["trending_players"]) == 2
        first_item = result["trending_players"][0]
        assert "player_id" in first_item and "count" in first_item and "enriched" in first_item
