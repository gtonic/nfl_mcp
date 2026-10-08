"""News source health (schema v20): the HTML contracts of the NBC and CBS
parsers, a markup change marked degraded / failing while the other sources
continue, the RSS fallback, the stored failure streak and its exponential
backoff, and where the health is surfaced (/health, data_freshness,
refresh_data, get_player_news).

The HTML fixtures (``tests/fixtures/news_html``) are the live pages of
2026-10-08 trimmed to their markup skeleton -- names, teams and dates kept,
every line of prose replaced by a synthetic one (see the fixture headers).
"""
import json
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from nfl_mcp import news_sources
from nfl_mcp.database import NFLDatabase
from tests.test_player_news import (
    ESPN_NEWS,
    ESPN_ROSTER_BAL,
    ESPN_ROSTER_CHI,
    NOW,
    _athletes,
)

HTML = Path(__file__).parent / "fixtures" / "news_html"
NBC_OK = (HTML / "nbc_player_news.html").read_text()
NBC_REDESIGN = (HTML / "nbc_player_news_redesign.html").read_text()
NBC_WRAPPER = (HTML / "nbc_player_news_wrapper_changed.html").read_text()
NBC_RSS = (HTML / "nbc_player_news.rss").read_text()
CBS_OK = (HTML / "cbs_player_news.html").read_text()
CBS_REDESIGN = (HTML / "cbs_player_news_redesign.html").read_text()


# CBS is the source under test; NBC answers, so the poll as a whole does not
# fail (it raises only when every source does).
CBS_AND_NBC = ["cbs", "nbc"]


def transport(nbc=NBC_OK, cbs=CBS_OK, rss=NBC_RSS, calls=None, nbc_status=200):
    """ESPN as in test_player_news; NBC page 1 = `nbc` (later pages empty),
    NBC RSS = `rss` (None: 404), CBS = `cbs`."""
    roster = {"BAL": ESPN_ROSTER_BAL, "CHI": ESPN_ROSTER_CHI}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if "/roster" in url:
            team = request.url.path.split("/teams/")[1].split("/")[0]
            return httpx.Response(200, json=roster.get(team, {"team": {"abbreviation": team},
                                                                "athletes": []}))
        if "fantasy/v2/games/ffl/news/players" in url:
            wanted = set(request.url.params.get_list("playerId"))
            return httpx.Response(200, json={**ESPN_NEWS, "feed": [
                x for x in ESPN_NEWS["feed"] if str(x.get("playerId")) in wanted]})
        if url.endswith("player-news.rss"):
            return httpx.Response(200, text=rss) if rss is not None else httpx.Response(404)
        if "nbcsports.com" in url:
            if request.url.params.get("p"):
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(nbc_status, text=nbc)
        if "cbssports.com" in url:
            return httpx.Response(200, text=cbs)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def ingest(db, now=NOW, sources=None, **kw):
    async with httpx.AsyncClient(transport=transport(**kw)) as client:
        return await news_sources.ingest_news(db, sources=sources, now=now, client=client)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        _athletes(database)
        yield database
        database.close()


@pytest.fixture(autouse=True)
def _fresh_roster_cache():
    news_sources._roster_cache.update(at=0.0, players=[], fresh_fetch=False)
    yield
    news_sources._roster_cache.update(at=0.0, players=[], fresh_fetch=False)


# ---------------------------------------------------------------------------
# Contracts: the saved pages parse to complete items
# ---------------------------------------------------------------------------

class TestContracts:
    def test_nbc_page(self):
        items, stats = news_sources.parse_nbc_page(NBC_OK)
        assert stats == {"containers": 10, "parsed": 10, "fallbacks": []}
        for it in items:
            assert it["name"] and it["headline"] and it["text"] and it["published_at"], it
            assert it["position"], it
            assert it["url"].startswith("https://www.nbcsports.com/fantasy/football/player-news/")
            assert "Bodiford" not in it["text"]  # the author line is not the note
        assert items[0]["name"] == "DeVonta Smith" and items[0]["team"] == "PHI"
        # A released player has no team on the page.
        assert sum(bool(it["team"]) for it in items) == 9
        assert news_sources.assess("nbc", {"parse": {**stats, "expected": 10}}) == ("ok", None)

    def test_cbs_page(self):
        items, stats = news_sources.parse_cbs_page(CBS_OK, NOW)
        assert stats == {"containers": 10, "parsed": 10, "fallbacks": []}
        for it in items:
            assert it["name"] and it["team"] and it["position"] and it["headline"], it
            assert it["published_at"] and it["url"].startswith("https://www.cbssports.com/")
        assert (items[0]["name"], items[0]["team"]) == ("Tee Higgins", "CIN")

    def test_nbc_rss(self):
        items = news_sources.parse_nbc_rss(NBC_RSS)
        # The transaction headline ("Commanders waive RB ...") names no player first.
        assert [i["name"] for i in items] == ["DeVonta Smith", "Jaylen Wright"]
        assert items[1]["headline"] == "Jaylen Wright (foot) was limited in Thursday's practice."
        assert items[0]["published_at"] == "2026-10-08T20:52:19+00:00"
        assert news_sources.parse_nbc_rss("not xml") == []

    def test_redesigned_pages_parse_to_nothing(self):
        assert news_sources.parse_nbc_page(NBC_REDESIGN)[1]["parsed"] == 0
        assert news_sources.parse_cbs_page(CBS_REDESIGN)[1]["parsed"] == 0

    def test_a_fallback_selector_reads_a_changed_wrapper_and_is_reported(self):
        items, stats = news_sources.parse_nbc_page(NBC_WRAPPER)
        assert len(items) == 10 and stats["fallbacks"] == ["post"]
        health, detail = news_sources.assess("nbc", {"parse": {**stats, "expected": 10}})
        assert health == "degraded" and "fallback selector" in detail and "post" in detail

    def test_assess(self):
        partial = {"parse": {"containers": 10, "parsed": 3, "fallbacks": [], "expected": 10}}
        health, detail = news_sources.assess("nbc", partial)
        assert health == "degraded"
        assert "parsed 3 of 10" in detail and "7 of 10 posts could not be read" in detail
        assert news_sources.assess("espn_fantasy", {"requests": 40, "failed_requests": 3}) == (
            "degraded", "3 of 40 requests failed")
        assert news_sources.assess("espn_fantasy", {"requests": 40}) == ("ok", None)

    def test_selectors_live_in_one_table(self):
        # Every selector the parsers use is in the tables (a redesign is an
        # edit there); each has a primary.
        for table in (news_sources.NBC_SELECTORS, news_sources.CBS_SELECTORS):
            assert all(isinstance(v, tuple) and v for v in table.values())


# ---------------------------------------------------------------------------
# Markup changed: the source is marked, the others continue
# ---------------------------------------------------------------------------

class TestMarkupChanged:
    @pytest.mark.asyncio
    async def test_nbc_redesign_falls_back_to_rss_degraded(self, db):
        out = await ingest(db, nbc=NBC_REDESIGN)
        nbc = out["sources"]["nbc"]
        assert nbc["status"] == "ok" and nbc["health"] == "degraded"
        assert "parsed to 0 items" in nbc["detail"] and "RSS" in nbc["detail"]
        assert nbc["fetched"] == 2  # the RSS items with a player name
        assert out["sources"]["cbs"]["health"] == "ok"
        assert out["sources"]["espn_fantasy"]["status"] == "ok"
        st = db.get_news_fetch_state()["nbc"]
        assert st["health"] == "degraded" and st["consecutive_failures"] == 0
        assert st["next_attempt_at"] is None

    @pytest.mark.asyncio
    async def test_nbc_redesign_without_feed_fails_and_backs_off(self, db):
        out = await ingest(db, nbc=NBC_REDESIGN, rss=None)
        nbc = out["sources"]["nbc"]
        assert nbc["status"] == "error" and nbc["health"] == "failing"
        assert "parser broken: 0 items parsed from a 200 response" in nbc["error"]
        assert nbc["consecutive_failures"] == 1
        assert nbc["next_attempt_at"] == (NOW + timedelta(minutes=30)).isoformat()
        assert out["sources"]["cbs"]["status"] == "ok"
        assert out["sources"]["espn_fantasy"]["written"] > 0
        st = db.get_news_fetch_state()["nbc"]
        assert st["health"] == "failing" and st["last_error"].startswith("parser broken")
        assert st["last_success_at"] is None

    @pytest.mark.asyncio
    async def test_cbs_redesign_fails_others_continue(self, db):
        out = await ingest(db, cbs=CBS_REDESIGN)
        assert out["sources"]["cbs"]["health"] == "failing"
        assert "parser broken" in out["sources"]["cbs"]["error"]
        assert out["sources"]["nbc"]["health"] == "ok"
        assert out["written"] > 0

    @pytest.mark.asyncio
    async def test_nbc_http_error_on_page_one_tries_the_feed(self, db):
        out = await ingest(db, sources=["nbc", "cbs"], nbc_status=403)
        nbc = out["sources"]["nbc"]
        assert nbc["status"] == "ok" and nbc["health"] == "degraded"
        out = await ingest(db, sources=["nbc", "cbs"], nbc_status=403, rss=None,
                           now=NOW + timedelta(hours=1))
        assert out["sources"]["nbc"]["health"] == "failing"
        assert "HTTP 403" in out["sources"]["nbc"]["error"]


# ---------------------------------------------------------------------------
# Backoff: a failing source is not hammered
# ---------------------------------------------------------------------------

class TestBackoff:
    def test_delay_doubles_and_is_capped(self):
        assert news_sources.backoff_delay(0) == timedelta(0)
        assert [news_sources.backoff_delay(n) for n in (1, 2, 3, 4)] == [
            timedelta(minutes=30), timedelta(hours=1), timedelta(hours=2), timedelta(hours=4)]
        assert news_sources.backoff_delay(50) == timedelta(hours=news_sources.BACKOFF_MAX_HOURS)

    @pytest.mark.asyncio
    async def test_streak_backoff_probe_and_recovery(self, db):
        await ingest(db, sources=CBS_AND_NBC, cbs=CBS_REDESIGN)
        # Ten minutes later: still waiting -- no request to CBS at all.
        calls: list[str] = []
        out = await ingest(db, sources=CBS_AND_NBC, cbs=CBS_REDESIGN, calls=calls,
                           now=NOW + timedelta(minutes=10))
        assert out["sources"]["cbs"]["status"] == "backoff"
        assert out["sources"]["cbs"]["consecutive_failures"] == 1
        assert not any("cbssports" in c for c in calls)
        # After the wait: one probe; it fails again, the wait doubles.
        t2 = NOW + timedelta(minutes=31)
        out = await ingest(db, sources=CBS_AND_NBC, cbs=CBS_REDESIGN, calls=calls, now=t2)
        assert sum("cbssports" in c for c in calls) == 1
        assert out["sources"]["cbs"]["consecutive_failures"] == 2
        assert out["sources"]["cbs"]["next_attempt_at"] == (t2 + timedelta(hours=1)).isoformat()
        # The site is fixed: the next probe succeeds and closes the breaker.
        t3 = t2 + timedelta(hours=1, minutes=1)
        out = await ingest(db, sources=CBS_AND_NBC, now=t3)
        assert out["sources"]["cbs"]["health"] == "ok"
        st = db.get_news_fetch_state()["cbs"]
        assert st["consecutive_failures"] == 0 and st["next_attempt_at"] is None
        assert st["last_success_at"] == t3.isoformat()
        assert st["last_error"].startswith("parser broken")  # kept for the record

    @pytest.mark.asyncio
    async def test_backoff_is_not_an_error_for_the_refresh(self, db):
        await ingest(db, sources=CBS_AND_NBC, cbs=CBS_REDESIGN)
        out = await ingest(db, sources=CBS_AND_NBC, now=NOW + timedelta(minutes=5))
        assert out["sources"]["cbs"]["status"] == "backoff"  # no raise

    @pytest.mark.asyncio
    async def test_ignore_backoff(self, db):
        await ingest(db, sources=CBS_AND_NBC, cbs=CBS_REDESIGN)
        async with httpx.AsyncClient(transport=transport()) as client:
            out = await news_sources.ingest_news(db, sources=CBS_AND_NBC, client=client,
                                                 now=NOW + timedelta(minutes=5),
                                                 ignore_backoff=True)
        assert out["sources"]["cbs"]["health"] == "ok"


# ---------------------------------------------------------------------------
# Where the health shows up
# ---------------------------------------------------------------------------

class TestSurfaced:
    @pytest.mark.asyncio
    async def test_data_freshness_news_sources_and_warning(self, db):
        await ingest(db, nbc=NBC_REDESIGN, rss=None)
        news = db.get_data_freshness()["news"]
        assert set(news["sources"]) == {"espn_fantasy", "nbc", "cbs"}
        nbc = news["sources"]["nbc"]
        assert nbc["health"] == "failing" and nbc["consecutive_failures"] == 1
        assert nbc["expected_items"] is None and nbc["enabled"] is True
        assert news["sources"]["cbs"]["parsed_items"] == 10
        assert news["sources"]["cbs"]["expected_items"] == 10
        assert any("NBC Sports / Rotoworld is failing" in w for w in news["warnings"])

    def test_a_disabled_source_is_not_a_warning(self, db, monkeypatch):
        db.record_news_fetch("nbc", 0, 0, None, "error", "boom")
        monkeypatch.setenv("NFL_MCP_NEWS_SOURCES", "-nbc")
        news = db.get_news_health()
        assert news["sources"]["nbc"]["enabled"] is False and "warnings" not in news

    @pytest.mark.asyncio
    async def test_health_endpoint(self, db):
        await ingest(db, cbs=CBS_REDESIGN)
        with patch("nfl_mcp.tool_registry.get_db", return_value=db), \
                patch("nfl_mcp.retry_utils.get_all_circuit_breaker_status", return_value={}):
            from nfl_mcp.health import health_check
            body = json.loads((await health_check()).body)
        news = body["data_freshness"]["news"]
        assert news["sources"]["cbs"]["health"] == "failing"
        assert news["sources"]["nbc"]["health"] == "ok"
        assert news["warnings"] and body["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_refresh_data_reports_the_degraded_source(self, db, monkeypatch):
        from nfl_mcp import data_refresh

        real = news_sources.ingest_news

        async def fake_ingest(database, **_):
            async with httpx.AsyncClient(transport=transport(nbc=NBC_REDESIGN)) as client:
                return await real(database, now=NOW, client=client)
        monkeypatch.setattr(news_sources, "ingest_news", fake_ingest)
        out = await data_refresh._refresh_news(db, 2026, 5)
        assert out["sources"]["nbc"]["health"] == "degraded"
        assert any(w.startswith("nbc: degraded") for w in out["warnings"])

    @pytest.mark.asyncio
    async def test_get_player_news_warns(self, db, monkeypatch):
        from nfl_mcp import player_news_tools
        await ingest(db, cbs=CBS_REDESIGN)
        monkeypatch.setattr(player_news_tools, "get_shared_db", lambda: db)
        monkeypatch.setattr(player_news_tools, "_now", lambda: NOW)
        out = await player_news_tools.get_player_news(["Lamar Jackson"])
        assert out["sources"]["cbs"]["health"] == "failing"
        assert out["sources"]["cbs"]["consecutive_failures"] == 1
        assert any("CBS Sports (RotoWire) is failing" in w for w in out["warnings"])


class TestConfig:
    def test_switch_one_source_off(self, monkeypatch):
        monkeypatch.setenv("NFL_MCP_NEWS_SOURCES", "-nbc")
        assert news_sources.enabled_sources() == ("espn_fantasy", "cbs")
        monkeypatch.setenv("NFL_MCP_NEWS_SOURCES", "-nbc,-cbs")
        assert news_sources.enabled_sources() == ("espn_fantasy",)
        monkeypatch.setenv("NFL_MCP_NEWS_SOURCES", "none")
        assert news_sources.enabled_sources() == ()
        monkeypatch.setenv("NFL_MCP_NEWS_SOURCES", "nbc,cbs,-cbs")
        assert news_sources.enabled_sources() == ("nbc",)

    @pytest.mark.asyncio
    async def test_no_source_enabled_fetches_nothing(self, db, monkeypatch):
        monkeypatch.setenv("NFL_MCP_NEWS_SOURCES", "none")
        calls: list[str] = []
        out = await ingest(db, calls=calls)
        assert out == {"fetched": 0, "written": 0, "unresolved": 0, "sources": {}}
        assert calls == []

    def test_v19_database_gets_the_health_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            NFLDatabase(path).close()
            with sqlite3.connect(path) as conn:
                conn.execute("DELETE FROM schema_version WHERE version >= 20")
                conn.execute("DROP TABLE news_fetch_state")
                conn.execute("""CREATE TABLE news_fetch_state (
                    source TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, newest_published TEXT,
                    items INTEGER, written INTEGER, status TEXT, error TEXT)""")
                conn.execute("INSERT INTO news_fetch_state VALUES "
                             "('cbs', '2026-10-08T20:00:00+00:00', NULL, 10, 2, 'ok', NULL)")
                conn.commit()
            database = NFLDatabase(path)
            st = database.get_news_fetch_state()["cbs"]
            assert st["last_success_at"] == "2026-10-08T20:00:00+00:00"
            assert st["consecutive_failures"] == 0 and st["health"] is None
            assert database.get_news_health()["sources"]["cbs"]["health"] == "ok"
            database.close()
