"""ESPN `$ref` athlete-id extraction, shared by both injury fetchers."""
import pytest

from nfl_mcp.injury_service import extract_athlete_id

BASE = "http://sports.core.api.espn.com/v2/sports/football/leagues/nfl"


class TestExtractAthleteId:
    def test_query_string_form_is_what_espn_actually_sends(self):
        # The injury payload's `athlete.$ref` ends at the id. A pattern that
        # required a trailing slash matched nothing and the prefetch loop
        # silently dropped every record across all 32 teams.
        url = f"{BASE}/seasons/2026/athletes/4684527?lang=en&region=us"
        assert extract_athlete_id(url) == "4684527"

    def test_trailing_path_segment_form(self):
        url = f"{BASE}/seasons/2026/athletes/4684527/injuries/-2004214?lang=en"
        assert extract_athlete_id(url) == "4684527"

    def test_bare_end_of_string_form(self):
        assert extract_athlete_id(f"{BASE}/athletes/12345") == "12345"

    @pytest.mark.parametrize("value", [None, "", "not-a-url", f"{BASE}/athletes/abc"])
    def test_returns_none_when_absent(self, value):
        assert extract_athlete_id(value) is None

    def test_first_id_wins_when_url_repeats_the_segment(self):
        url = f"{BASE}/athletes/111/related/athletes/222"
        assert extract_athlete_id(url) == "111"


class TestFetchInjuriesProducesRecords:
    """End-to-end regression: the prefetch fetcher must return rows."""

    @pytest.mark.asyncio
    async def test_real_payload_shape_yields_a_record(self, monkeypatch):
        injury_ref = f"{BASE}/seasons/2026/athletes/4684527/injuries/-2004214?lang=en"
        athlete_ref = f"{BASE}/seasons/2026/athletes/4684527?lang=en&region=us"

        # Shapes copied from the live ESPN Core API: the list returns bare
        # $refs, the detail's `athlete` carries only a $ref (no displayName).
        responses = {
            "injuries?limit=50&page=1": {
                "count": 1, "pageCount": 1, "items": [{"$ref": injury_ref}],
            },
            injury_ref: {
                "shortComment": "Questionable for Sunday.",
                "status": "Questionable",
                "date": "2026-09-15T12:00Z",
                "athlete": {"$ref": athlete_ref},
                "type": {"name": "INJURY_STATUS_QUESTIONABLE", "description": "questionable"},
            },
            athlete_ref: {"displayName": "Test Player"},
        }

        class MockResponse:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        class MockClient:
            async def get(self, url, **kwargs):
                for key, payload in responses.items():
                    if key in url:
                        return MockResponse(payload)
                return MockResponse({"count": 0, "pageCount": 1, "items": []})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        monkeypatch.setenv("NFL_MCP_ADVANCED_ENRICH", "1")
        from nfl_mcp import sleeper_enrichment as se

        monkeypatch.setattr(se, "ADVANCED_ENRICH_ENABLED", True)
        # `_fetch_injuries` imports this from .config inside the function body,
        # so the patch has to land on the defining module.
        monkeypatch.setattr(
            "nfl_mcp.config.create_http_client", lambda *a, **k: MockClient()
        )

        result = await se._fetch_injuries()

        assert result, "fetcher returned no records at all"
        first = result[0]
        assert first["player_id"] == "4684527"
        assert first["player_name"] == "Test Player"
        assert first["injury_status"] == "Questionable"
        assert first["injury_description"] == "Questionable for Sunday."
