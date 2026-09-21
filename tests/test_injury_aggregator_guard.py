"""Using the aggregator without a client must fail loudly, not silently.

Without `async with`, `self._http_client` is None and every team fetch raises
`'NoneType' object has no attribute 'get'` — which the per-team handler logs at
debug level as "ESPN page 1 failed for BUF". That reads like a broken upstream
payload, and cost a real debugging session against an ESPN feed that was fine.
All 32 teams then return nothing and the caller gets an empty, successful-
looking result.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from nfl_mcp.injury_service import InjuryAggregator


class TestRequiresClient:
    @pytest.mark.asyncio
    async def test_espn_fetch_raises_without_a_client(self):
        agg = InjuryAggregator()
        with pytest.raises(RuntimeError) as err:
            await agg.fetch_espn_injuries(teams=["BUF"])
        assert "context manager" in str(err.value)

    @pytest.mark.asyncio
    async def test_the_message_names_both_ways_to_fix_it(self):
        agg = InjuryAggregator()
        with pytest.raises(RuntimeError) as err:
            await agg.fetch_espn_injuries(teams=["BUF"])
        message = str(err.value)
        assert "async with" in message
        assert "http_client=" in message
        # And says why the old behaviour was dangerous.
        assert "looks like" in message

    @pytest.mark.asyncio
    async def test_fetch_all_injuries_raises_too(self):
        """The aggregate entry point is the one callers actually use."""
        agg = InjuryAggregator()
        with pytest.raises(RuntimeError):
            await agg.fetch_all_injuries(teams=["BUF"], force_refresh=True)

    @pytest.mark.asyncio
    async def test_an_injected_client_is_accepted(self):
        """Passing a client explicitly must remain a supported path."""
        client = MagicMock()
        client.get = AsyncMock(return_value=MagicMock(
            status_code=200, headers={}, json=MagicMock(return_value={"items": [], "pageCount": 1})
        ))
        agg = InjuryAggregator(http_client=client)
        assert await agg.fetch_espn_injuries(teams=["BUF"]) == []

    @pytest.mark.asyncio
    async def test_context_manager_provides_one(self):
        async with InjuryAggregator() as agg:
            agg._require_client()   # must not raise
            assert agg._http_client is not None
