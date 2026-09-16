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


class TestEspnInjuryParsingProducesRecords:
    """End-to-end regression against the real ESPN payload shape.

    A pattern requiring a trailing slash returned zero records for every team
    for months; the failure was silent, so pin the parse to real shapes.
    """

    @pytest.mark.asyncio
    async def test_real_payload_shape_yields_a_record(self):
        injury_ref = f"{BASE}/seasons/2026/athletes/4684527/injuries/-2004214?lang=en"
        athlete_ref = f"{BASE}/seasons/2026/athletes/4684527?lang=en&region=us"

        # Shapes copied from the live ESPN Core API: the list returns bare
        # $refs, and the detail's `athlete` carries only a $ref — no
        # displayName, which is why the id must come out of the URL.
        responses = {
            "injuries?limit": {
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
                self.headers = {}
                self._payload = payload

            def json(self):
                return self._payload

        class MockClient:
            async def get(self, url, **kwargs):
                for key, payload in responses.items():
                    if key in url:
                        return MockResponse(payload)
                return MockResponse({"count": 0, "pageCount": 1, "items": []})

        from nfl_mcp.injury_service import InjuryAggregator

        aggregator = InjuryAggregator(http_client=MockClient())
        reports = await aggregator.fetch_espn_injuries(["ARI"])

        assert reports, "parser returned no records at all"
        first = reports[0]
        assert first.player_id == "4684527"
        assert first.player_name == "Test Player"
        assert first.injury_status == "Questionable"
