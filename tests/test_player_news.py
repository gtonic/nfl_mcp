"""Player news sources (`news_sources`), their store (schema v19), the news
flags read from them (`news_signals`) and `get_player_news`.

Network is mocked: the payloads below are trimmed copies of what each source
returned live (2026-10-08).
"""
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from nfl_mcp import news_signals, news_sources, player_news_tools
from nfl_mcp.database import NFLDatabase

NOW = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)  # a Thursday

# --- ESPN fantasy player-news feed (?playerId=...), trimmed -----------------
ESPN_NEWS = {
    "timestamp": "2026-10-08T19:55:38Z", "status": "success", "resultsCount": 4,
    "feed": [
        {"id": 64135328, "type": "Rotowire",
         "headline": "Jackson (ankle) remained absent from practice Thursday, Sam Cohn of the "
                     "Baltimore Sun reports.",
         "description": "Jackson (ankle) remained absent from practice Thursday.",
         "published": "2026-10-08T17:09:17Z",
         "story": "Jackson is set for a second consecutive absence to begin Week 5 prep. "
                  "Tyler Huntley would be in line to start against the Falcons if Jackson "
                  "isn't available.",
         "links": {"mobile": {"href": "http://m.espn.go.com/wireless/story?storyId=64135328"}},
         "playerId": 3916387},
        {"id": 64107028, "type": "Rotowire",
         "headline": "Jackson appears \"unlikely\" to play Sunday against the Falcons due to a "
                     "\"rarer type of ankle sprain\" that could put him at risk of missing "
                     "multiple games, Ian Rapoport of NFL Network reports.",
         "published": "2026-10-06T20:50:56Z",
         "story": "The ankle injury sidelined Jackson for the entire second half.",
         "playerId": 3916387},
        {"id": 49949224, "type": "Story",
         "headline": "Rest-of-season rankings: Tetairoa McMillan, George Kittle rise",
         "published": "2026-10-06T18:17:19Z", "story": "Need a quick free agent pickup?",
         "links": {"web": {"href": "https://www.espn.com/fantasy/football/story/_/id/49949224"}}},
        {"id": 64100002, "type": "Rotowire",
         "headline": "Hill (knee) is working out for teams.", "published": "2026-10-07T14:00:00Z",
         "story": "", "playerId": 3116406},
        {"id": 64100001, "type": "Rotowire",
         "headline": "Swift was benched after his second-quarter fumble and didn't get another "
                     "carry.",
         "published": "2026-10-05T23:00:00Z", "story": "", "playerId": 4259545},
    ],
}

# --- ESPN team roster (/teams/BAL/roster), trimmed ---------------------------
ESPN_ROSTER_BAL = {
    "team": {"abbreviation": "BAL"},
    "athletes": [
        {"position": "offense", "items": [
            {"id": "3916387", "fullName": "Lamar Jackson", "position": {"abbreviation": "QB"}},
            {"id": "4035538", "fullName": "Tyler Huntley", "position": {"abbreviation": "QB"}},
            {"id": "4429615", "fullName": "Zay Flowers", "position": {"abbreviation": "WR"}},
            {"id": "9999001", "fullName": "Ronnie Stanley", "position": {"abbreviation": "OT"}},
        ]},
        {"position": "defense", "items": [
            {"id": "9999002", "fullName": "Roquan Smith", "position": {"abbreviation": "LB"}},
        ]},
        {"position": "specialTeam", "items": [
            {"id": "9999003", "fullName": "Tyler Loop", "position": {"abbreviation": "PK"}},
        ]},
        {"position": "practiceSquad", "items": [
            {"id": "9999004", "fullName": "Practice Guy", "position": {"abbreviation": "WR"}},
        ]},
    ],
}
ESPN_ROSTER_CHI = {
    "team": {"abbreviation": "CHI"},
    "athletes": [{"position": "offense", "items": [
        {"id": "4259545", "fullName": "D'Andre Swift", "position": {"abbreviation": "RB"}},
    ]}],
}

# --- NBC Sports / Rotoworld player-news page, trimmed -----------------------
NBC_POST = """
<li class="PlayerNewsModuleList-item">
 <div class="PlayerNewsPost">
  <div class="PlayerNewsPost-player"><div class="PlayerNewsPost-player-info">
   <h2 class="PlayerNewsPost-name"><div class="PlayerNewsPost-name-container">
    <a href="https://www.nbcsports.com/nfl/x/1">
     <span class="PlayerNewsPost-firstName">{first}</span>
     <span class="PlayerNewsPost-lastName">{last}</span></a></div></h2>
   <div class="PlayerNewsPost-team">
    <span class="PlayerNewsPost-team-abbr">{team}</span>
    <span class="PlayerNewsPost-position">{position}</span>
    <span class="PlayerNewsPost-number">#1</span></div></div>
   <div class="PlayerNewsPost-actions"><div class="PlayerNewsPost-share">
    <button class="Icon-button" data-share-url="https://www.nbcsports.com/fantasy/football/player-news/{slug}"></button>
   </div></div></div>
  <div class="PlayerNewsPost-content">
   <h3 class="PlayerNewsPost-headline">{headline}</h3>
   <div class="PlayerNewsPost-analysis">{analysis}
    <div class="PlayerNewsPost-author">- <a href="/author/x">Nic Bodiford</a></div></div>
   <div class="PlayerNewsPost-footer"><div class="PlayerNewsPost-date" data-date="{date}"></div></div>
  </div>
 </div>
</li>"""


def nbc_page(*posts: dict) -> str:
    return ("<html><body><ul class='PlayerNewsModuleList'>"
            + "".join(NBC_POST.format(**p) for p in posts) + "</ul></body></html>")


NBC_PAGE_1 = nbc_page(
    {"first": "D’Andre", "last": "Swift", "team": "CHI", "position": "Running Back",
     "slug": "2026-10-08/swift-limited", "date": "2026-10-08T19:22:17.876Z",
     "headline": "D’Andre Swift (hip/knee) was limited in Thursday’s practice.",
     "analysis": "Swift is expected to play Sunday, but Kyle Monangai will split the work."},
    {"first": "Justin", "last": "Jefferson", "team": "CLE", "position": "Linebacker",
     "slug": "2026-10-08/browns-lb-jefferson", "date": "2026-10-08T18:00:00.000Z",
     "headline": "Browns LB Justin Jefferson (ankle) did not practice on Thursday.",
     "analysis": "Jefferson is in danger of missing Week 5."},
    {"first": "Christian", "last": "Darrisaw", "team": "MIN", "position": "Tackle",
     "slug": "2026-10-08/vikings-lt-darrisaw", "date": "2026-10-08T17:59:08.672Z",
     "headline": "Vikings LT Christian Darrisaw (concussion) was not seen practicing on Thursday.",
     "analysis": "Darrisaw is on the wrong side of questionable."},
)
NBC_PAGE_2 = nbc_page(
    {"first": "Zay", "last": "Flowers", "team": "BAL", "position": "Wide Receiver",
     "slug": "2026-10-07/flowers", "date": "2026-10-07T15:00:00.000Z",
     "headline": "Zay Flowers (knee) was a full participant Wednesday.",
     "analysis": "Flowers should be fine for Sunday night."},
    {"first": "Old", "last": "Note", "team": "BAL", "position": "Wide Receiver",
     "slug": "2026-09-01/old", "date": "2026-09-01T15:00:00.000Z",
     "headline": "Old Note is old.", "analysis": ""},
)

# --- CBS fantasy player news (/fantasy/football/players/news/all/), trimmed ---
CBS_ITEM = """
<li><div class="row">
 <div class="col-3"><div class="player-team-info"><div class="row"><div class="col-2">
  <div class="players-annotated"><p><a href="/nfl/players/1/x/fantasy/">{name}</a>
   <span>{label}</span></p></div></div></div></div></div>
 <div class="col-5"><div class="player-news-desc">
  <time class="eyebrow">{ago}</time>
  <h4><a href="/fantasy/football/news/{slug}/">{title}</a></h4>
  <span class="byline">By RotoWire Staff</span>
  <div class="latest-updates"><p><a class="Annotation-link" href="/x">{short}</a> {first}</p>
   <p>{second}</p></div>
 </div></div>
</div></li>"""
CBS_PAGE = ("<html><body><ul class='player-news-by-sport'>" + "".join(CBS_ITEM.format(**i) for i in (
    {"name": "D'Andre Swift", "label": "RB | CHI", "ago": "28M ago", "slug": "bears-swift",
     "title": "Bears' D'Andre Swift: Logs limited practice Thursday", "short": "Swift",
     "first": "(hip/knee) returned to practice on a limited basis Thursday, Sean Hammond of the "
              "Chicago Tribune reports.",
     "second": "Swift was benched after his second-quarter fumble and didn't get another carry."},
    {"name": "Justin Jefferson", "label": "WR | MIN", "ago": "2H ago", "slug": "vikings-jj",
     "title": "Vikings' Justin Jefferson: Full practice", "short": "Jefferson",
     "first": "(hamstring) practiced fully Thursday.", "second": "He will play Sunday."},
)) + "</ul></body></html>")


def _athletes(db: NFLDatabase) -> None:
    db.upsert_athletes({
        "4881": {"full_name": "Lamar Jackson", "first_name": "Lamar", "last_name": "Jackson",
                 "position": "QB", "team": "BAL", "espn_id": 3916387, "status": "Active"},
        "6994": {"full_name": "Lamar Jackson", "first_name": "Lamar", "last_name": "Jackson",
                 "position": "CB", "team": None, "espn_id": 4034849, "status": "Active"},
        "4035538": {"full_name": "Tyler Huntley", "first_name": "Tyler", "last_name": "Huntley",
                    "position": "QB", "team": "BAL", "status": "Active"},
        "7526": {"full_name": "Zay Flowers", "first_name": "Zay", "last_name": "Flowers",
                 "position": "WR", "team": "BAL", "status": "Active"},
        "6790": {"full_name": "D'Andre Swift", "first_name": "D'Andre", "last_name": "Swift",
                 "position": "RB", "team": "CHI", "status": "Active"},
        "12520": {"full_name": "Kyle Monangai", "first_name": "Kyle", "last_name": "Monangai",
                  "position": "RB", "team": "CHI", "status": "Active"},
        "6794": {"full_name": "Justin Jefferson", "first_name": "Justin", "last_name": "Jefferson",
                 "position": "WR", "team": "MIN", "espn_id": 4262921, "status": "Active"},
        "13524": {"full_name": "Justin Jefferson", "first_name": "Justin",
                  "last_name": "Jefferson", "position": "LB", "team": "CLE", "status": "Active"},
        "4984": {"full_name": "Travis Etienne", "first_name": "Travis", "last_name": "Etienne",
                 "position": "RB", "team": "NO", "status": "Active"},
        "3321": {"full_name": "Tyreek Hill", "first_name": "Tyreek", "last_name": "Hill",
                 "position": "WR", "team": None, "espn_id": 3116406, "status": "Active",
                 "news_updated": 1791382506000},  # 2026-10-07
        "1111": {"full_name": "Retired Guy", "first_name": "Retired", "last_name": "Guy",
                 "position": "WR", "team": None, "espn_id": 1, "status": "Active",
                 "news_updated": 1600000000000},
        "5110": {"full_name": "D.J. Moore", "first_name": "D.J.", "last_name": "Moore",
                 "position": "WR", "team": "BUF", "status": "Active"},
    })


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        _athletes(database)
        yield database


def _transport(roster=None, news=ESPN_NEWS, nbc=(NBC_PAGE_1, NBC_PAGE_2), cbs=CBS_PAGE,
               calls=None):
    roster = roster if roster is not None else {"BAL": ESPN_ROSTER_BAL, "CHI": ESPN_ROSTER_CHI}

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
            feed = [x for x in news["feed"] if str(x.get("playerId")) in wanted
                    or x.get("type") == "Story"]
            return httpx.Response(200, json={**news, "feed": feed})
        if "nbcsports.com" in url:
            page = int(request.url.params.get("p") or 1)
            return httpx.Response(200, text=nbc[page - 1] if page <= len(nbc) else nbc_page())
        if "cbssports.com" in url:
            return httpx.Response(200, text=cbs)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _fresh_roster_cache():
    news_sources._roster_cache.update(at=0.0, players=[], fresh_fetch=False)
    yield
    news_sources._roster_cache.update(at=0.0, players=[], fresh_fetch=False)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

class TestParsers:
    def test_espn_news_keeps_player_notes_only(self):
        items = news_sources.parse_espn_news(ESPN_NEWS)
        assert [i["source_id"] for i in items] == ["64135328", "64107028", "64100002",
                                                   "64100001"]
        first = items[0]
        assert first["espn_id"] == "3916387" and first["source"] == "espn_fantasy"
        assert first["headline"].startswith("Jackson (ankle) remained absent")
        assert "Tyler Huntley" in first["text"]
        assert first["published_at"] == "2026-10-08T17:09:17+00:00"
        assert first["url"] == "https://www.espn.com/nfl/player/_/id/3916387"

    def test_espn_roster_fantasy_groups_and_positions(self):
        rows = news_sources.parse_espn_roster(ESPN_ROSTER_BAL)
        assert {(r["name"], r["position"]) for r in rows} == {
            ("Lamar Jackson", "QB"), ("Tyler Huntley", "QB"), ("Zay Flowers", "WR"),
            ("Tyler Loop", "K")}  # no OT, LB or practice squad
        assert all(r["team"] == "BAL" for r in rows)

    def test_nbc_page(self):
        items = news_sources.parse_nbc(NBC_PAGE_1)
        swift = items[0]
        assert swift["name"] == "D'Andre Swift" and swift["team"] == "CHI"
        assert swift["position"] == "Running Back"
        assert swift["headline"] == "D'Andre Swift (hip/knee) was limited in Thursday's practice."
        assert "Nic Bodiford" not in swift["text"] and "split the work" in swift["text"]
        assert swift["url"].endswith("/2026-10-08/swift-limited")
        assert swift["published_at"] == "2026-10-08T19:22:17+00:00"
        assert items[1]["position"] == "Linebacker" and items[1]["team"] == "CLE"

    def test_cbs_page(self):
        items = news_sources.parse_cbs(CBS_PAGE, NOW)
        swift = items[0]
        assert swift["name"] == "D'Andre Swift" and swift["team"] == "CHI"
        assert swift["position"] == "RB"
        assert swift["headline"].startswith("Swift (hip/knee) returned to practice")
        assert "benched" in swift["text"]
        assert swift["title"] == "Bears' D'Andre Swift: Logs limited practice Thursday"
        assert swift["url"] == "https://www.cbssports.com/fantasy/football/news/bears-swift/"
        assert swift["published_at"] == (NOW - timedelta(minutes=28)).isoformat(timespec="seconds")
        assert items[1]["published_at"] == (NOW - timedelta(hours=2)).isoformat(timespec="seconds")

    def test_item_text_does_not_repeat_the_headline(self):
        assert news_sources.item_text({"headline": "A b c.", "text": "A b c. More."}) == "A b c. More."
        assert news_sources.item_text({"headline": "A b c", "text": "More."}) == "A b c. More."
        assert news_sources.item_text({"headline": "A b c.", "text": ""}) == "A b c."

    def test_content_hash_ignores_case_punctuation_and_spacing(self):
        a = news_sources.content_hash("nbc", "1", "Swift  (hip) LIMITED.", "x")
        b = news_sources.content_hash("nbc", "1", "swift (hip) limited", "x")
        assert a == b != news_sources.content_hash("cbs", "1", "swift (hip) limited", "x")


# ---------------------------------------------------------------------------
# Player mapping
# ---------------------------------------------------------------------------

class TestResolve:
    def test_same_name_other_position_is_never_taken(self, db):
        lb = news_sources.resolve_player(db, "Justin Jefferson", "CLE", "Linebacker")
        wr = news_sources.resolve_player(db, "Justin Jefferson", "MIN", "WR")
        assert lb["id"] == "13524" and wr["id"] == "6794"
        # Off his team: name + position unique league-wide, or nothing.
        assert news_sources.resolve_player(db, "Justin Jefferson", "DAL", "WR")["id"] == "6794"
        assert news_sources.resolve_player(db, "Justin Jefferson", "DAL", "TE") is None
        assert news_sources.resolve_player(db, "Justin Jefferson", "DAL", None) is None
        assert news_sources.resolve_player(db, "Justin Jefferson") is None

    def test_suffixes_punctuation_and_apostrophes(self, db):
        assert news_sources.resolve_player(db, "Travis Etienne Jr.", "NO", "RB")["id"] == "4984"
        assert news_sources.resolve_player(db, "DJ Moore", "BUF", "WR")["id"] == "5110"
        assert news_sources.resolve_player(db, "D’Andre Swift", "CHI", "RB")["id"] == "6790"

    def test_unknown_player(self, db):
        assert news_sources.resolve_player(db, "Christian Darrisaw", "MIN", "Tackle") is None

    def test_map_items_espn_id_first_then_roster_name(self, db):
        roster = {p["espn_id"]: p for p in news_sources.parse_espn_roster(ESPN_ROSTER_BAL)}
        items = [{"source": "espn_fantasy", "espn_id": "3916387", "headline": "a", "text": "b"},
                 {"source": "espn_fantasy", "espn_id": "4429615", "headline": "c", "text": ""},
                 {"source": "espn_fantasy", "espn_id": "123", "headline": "d", "text": ""}]
        rows, unresolved = news_sources.map_items(db, items, roster, NOW)
        assert [r["player_id"] for r in rows] == ["4881", "7526"]  # QB Lamar, not the CB
        assert rows[0]["team"] == "BAL" and rows[0]["position"] == "QB"
        assert rows[1]["text"] == "c"  # headline-only note
        assert [u["espn_id"] for u in unresolved] == ["123"]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _row(pid="4881", source="nbc", text="Jackson is unlikely to play.", published=None,
         team="BAL", name="Lamar Jackson", url=None):
    return {"content_hash": news_sources.content_hash(source, pid, None, text), "source": source,
            "player_id": pid, "player_name": name, "name_key": name.lower(), "team": team,
            "position": "QB", "headline": None, "text": text, "url": url,
            "published_at": published or NOW.isoformat()}


class TestStore:
    def test_dedupe_and_filters(self, db):
        assert db.upsert_player_news([_row(), _row(), _row(source="cbs")]) == 2
        assert db.upsert_player_news([_row()]) == 0
        assert db.upsert_player_news([{**_row(), "player_id": ""}]) == 0
        db.upsert_player_news([_row(pid="6790", team="CHI", name="D'Andre Swift",
                                    published=(NOW - timedelta(days=20)).isoformat())])
        assert len(db.get_player_news(["4881"])) == 2
        assert len(db.get_player_news(teams=["CHI"])) == 1
        assert len(db.get_player_news(["4881"], teams=["CHI"])) == 3
        assert len(db.get_player_news(since=(NOW - timedelta(days=7)).isoformat())) == 2

    def test_fetch_state_and_freshness(self, db):
        db.record_news_fetch("nbc", 10, 4, "2026-10-08T19:00:00+00:00")
        db.record_news_fetch("nbc", 0, 0, None, "error", "boom")
        st = db.get_news_fetch_state()["nbc"]
        assert st["newest_published"] == "2026-10-08T19:00:00+00:00"
        assert st["status"] == "error" and st["error"] == "boom"
        assert db.get_data_freshness()["news"]["age_hours"] is not None

    def test_v18_database_gets_the_news_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            NFLDatabase(path).close()
            with sqlite3.connect(path) as conn:
                conn.execute("DELETE FROM schema_version WHERE version >= 19")
                conn.execute("DROP TABLE player_news")
                conn.execute("DROP TABLE news_fetch_state")
                conn.commit()
            NFLDatabase(path).close()
            with sqlite3.connect(path) as conn:
                assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 19
                assert conn.execute("SELECT COUNT(*) FROM player_news").fetchone()[0] == 0
                assert conn.execute("SELECT COUNT(*) FROM news_fetch_state").fetchone()[0] == 0

    def test_prune_keeps_the_season(self, db):
        db.upsert_player_news([_row(published="2025-01-01T00:00:00+00:00")])
        with db._pool.get_connection() as conn:
            conn.execute("UPDATE player_news SET recorded_at='2024-01-01T00:00:00+00:00'")
            conn.commit()
        db.upsert_player_news([_row(text="fresh")])
        assert db.prune_old_data()["player_news"] == 1
        assert [r["text"] for r in db.get_player_news(["4881"])] == ["fresh"]


# ---------------------------------------------------------------------------
# Ingest (all three sources, network mocked)
# ---------------------------------------------------------------------------

class TestIngest:
    @pytest.mark.asyncio
    async def test_ingest_all_sources_then_idempotent(self, db):
        calls: list[str] = []
        async with httpx.AsyncClient(transport=_transport(calls=calls)) as client:
            out = await news_sources.ingest_news(db, now=NOW, client=client)
        src = out["sources"]
        assert src["espn_fantasy"]["status"] == "ok"
        # 2 Lamar + 1 Swift + 1 Hill (no team: a free agent with recent news);
        # the Story is not a note.
        assert src["espn_fantasy"]["written"] == 4
        assert src["espn_fantasy"]["roster_players"] == 5
        assert src["espn_fantasy"]["free_agents"] == 1
        assert [r["player_id"] for r in db.get_player_news(["3321"])] == ["3321"]
        assert src["nbc"]["written"] == 3  # Swift, Jefferson (LB), Flowers; old one cut
        assert src["nbc"]["unresolved"] == 1  # Darrisaw: not in the athlete cache
        assert src["cbs"]["written"] == 2
        stored = db.get_player_news(["13524"])
        assert len(stored) == 1 and stored[0]["source"] == "nbc"  # the linebacker's note
        assert [r["source"] for r in db.get_player_news(["6794"])] == ["cbs"]  # the receiver's
        # NBC paged until it passed the lookback (page 2 holds a September note).
        assert sum("nbcsports.com" in c for c in calls) == 2
        state = db.get_news_fetch_state()
        assert state["nbc"]["newest_published"] == "2026-10-08T19:22:17+00:00"

        calls.clear()
        async with httpx.AsyncClient(transport=_transport(calls=calls)) as client:
            again = await news_sources.ingest_news(db, now=NOW, client=client)
        assert again["written"] == 0
        # Incremental: page 1 already reaches the newest note seen.
        assert sum("nbcsports.com" in c for c in calls) == 1
        # The ESPN roster is cached between polls.
        assert not any("/roster" in c for c in calls)

    @pytest.mark.asyncio
    async def test_espn_batches_and_limits(self, db, monkeypatch):
        monkeypatch.setattr(news_sources, "ESPN_BATCH", 2)
        calls: list[str] = []
        async with httpx.AsyncClient(transport=_transport(calls=calls)) as client:
            await news_sources.ingest_news(db, sources=["espn_fantasy"], now=NOW, client=client)
        news_calls = [c for c in calls if "news/players" in c]
        assert len(news_calls) == 3  # 5 roster players + 1 free agent, 2 per request
        assert f"limit={2 * news_sources.ESPN_ITEMS_PER_PLAYER}" in news_calls[0]

    @pytest.mark.asyncio
    async def test_a_failing_source_does_not_stop_the_others(self, db):
        base = _transport()

        def boom(request):
            if "cbssports" in str(request.url):
                return httpx.Response(503)
            return base.handle_request(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
            out = await news_sources.ingest_news(db, now=NOW, client=client)
        assert out["sources"]["cbs"]["status"] == "error"
        assert out["sources"]["espn_fantasy"]["status"] == "ok"
        assert db.get_news_fetch_state()["cbs"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_every_source_failing_raises(self, db):
        transport = httpx.MockTransport(lambda r: httpx.Response(500))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(RuntimeError):
                await news_sources.ingest_news(db, sources=["nbc", "cbs"], now=NOW, client=client)

    def test_enabled_sources_env(self, monkeypatch):
        monkeypatch.setenv("NFL_MCP_NEWS_SOURCES", "cbs, espn_fantasy,bogus")
        assert news_sources.enabled_sources() == ("espn_fantasy", "cbs")
        monkeypatch.delenv("NFL_MCP_NEWS_SOURCES")
        assert news_sources.enabled_sources() == news_sources.SOURCES

    def test_nbc_limiter_keeps_the_crawl_delay(self):
        from nfl_mcp import config
        assert config.rate_limiter_name_for_host("www.nbcsports.com") == "nbc"
        config._rate_limiters.pop("nbc", None)
        limiter = config.get_rate_limiter("nbc")
        assert limiter.capacity == 1 and round(limiter.rate * 60) == 6


# ---------------------------------------------------------------------------
# News flags from the stored items
# ---------------------------------------------------------------------------

def _news(text, published, source="espn_fantasy", name="Lamar Jackson", team="BAL", url=None,
          headline=None):
    return {"player_name": name, "team": team, "headline": headline, "text": text,
            "published_at": published, "source": source, "url": url}


class TestNewsFlags:
    def test_quoted_phrases_and_source_on_the_flag(self):
        items = news_signals.news_rows([_news(
            ESPN_NEWS["feed"][1]["headline"], "2026-10-07T20:50:56+00:00",
            url="https://www.espn.com/nfl/player/_/id/3916387")])
        flags = news_signals.signals_for(news_signals.build_index([], NOW, news=items),
                                         "Lamar Jackson", "BAL")
        by = {f["flag"]: f for f in flags}
        assert set(by) == {"unlikely_to_play", "week_to_week"}
        assert by["unlikely_to_play"]["source"] == "espn_fantasy"
        assert by["unlikely_to_play"]["url"].endswith("3916387")
        assert "unlikely to play" in by["unlikely_to_play"]["snippet"]

    def test_outside_chance_with_curly_quotes(self):
        hits = news_signals.classify("Jackson has only an “outside chance” of playing.",
                                     "lamar jackson", {}, "jackson")
        assert [h["flag"] for h in hits] == ["unlikely_to_play"]

    def test_one_flag_per_player_however_many_sources(self):
        text = "Swift was benched after his fumble."
        items = news_signals.news_rows([
            _news(text, "2026-10-06T12:00:00+00:00", source=s, name="D'Andre Swift", team="CHI")
            for s in ("espn_fantasy", "cbs", "nbc")])
        index = news_signals.build_index(
            [{"player_name": "D'Andre Swift", "team_id": "CHI", "injury_description": text,
              "date_reported": "2026-10-06T12:00Z"}], NOW, news=items)
        hits = index[("dandre swift", "CHI")]
        assert len(hits) == 1  # the same note is read once
        flags = news_signals.signals_for(index, "D'Andre Swift", "CHI")
        assert [f["flag"] for f in flags] == ["benched"]
        adj = news_signals.adjustment(flags)
        assert adj["model_mult"] >= news_signals.MIN_MODEL_MULT

    def test_availability_from_last_week_is_dropped_and_newest_wins(self):
        items = news_signals.news_rows([
            _news("Jackson was ruled out for Sunday.", "2026-10-03T15:00:00+00:00"),  # last week
            _news("Jackson is unlikely to play Sunday.", "2026-10-07T15:00:00+00:00"),
            _news("Jackson is expected to play Sunday.", "2026-10-08T18:00:00+00:00",
                  source="nbc"),
        ])
        flags = news_signals.signals_for(news_signals.build_index([], NOW, news=items),
                                         "Lamar Jackson", "BAL")
        assert [f["flag"] for f in flags] == ["expected_to_play"]
        assert flags[0]["source"] == "nbc"

    def test_role_flags_from_last_week_still_count(self):
        items = news_signals.news_rows([
            _news("Swift was benched after his fumble.", "2026-10-04T23:00:00+00:00",
                  name="D'Andre Swift", team="CHI")])
        flags = news_signals.signals_for(news_signals.build_index([], NOW, news=items),
                                         "D'Andre Swift", "CHI")
        assert [f["flag"] for f in flags] == ["benched"]

    def test_teammate_attribution_from_a_news_item(self):
        items = news_signals.news_rows([
            _news(ESPN_NEWS["feed"][0]["headline"] + " " + ESPN_NEWS["feed"][0]["story"],
                  "2026-10-08T17:09:17+00:00"),
            _news("Huntley took the first-team reps.", "2026-10-08T12:00:00+00:00",
                  name="Tyler Huntley")])
        index = news_signals.build_index([], NOW, news=items)
        huntley = news_signals.signals_for(index, "Tyler Huntley", "BAL")
        assert [f["flag"] for f in huntley] == ["lead_role"]
        assert huntley[0]["from_player"] == "Lamar Jackson"
        assert not any(f["flag"] == "lead_role"
                       for f in news_signals.signals_for(index, "Lamar Jackson", "BAL"))

    def test_not_ruled_out(self):
        for text in ("Moore has not been ruled out for the Week 5 matchup.",
                     "Vrabel noted that Stevenson hasn't been ruled out for this weekend."):
            assert news_signals.classify(text, "x", {}, "x") == []
        assert [h["flag"] for h in news_signals.classify("Smith won't play Sunday.", "x", {},
                                                         "x")] == ["ruled_out"]

    def test_index_for_is_cached_until_the_news_changes(self, db):
        news_signals._index_cache.clear()
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        db.upsert_player_news([_row(pid="6790", team="CHI", name="D'Andre Swift",
                                    text="Swift was benched after his fumble.", published=recent)])
        first = news_signals.index_for(db, [])
        assert news_signals.index_for(db, []) is first
        db.upsert_player_news([_row(pid="12520", team="CHI", name="Kyle Monangai",
                                    text="Monangai will be the lead back.", published=recent)])
        second = news_signals.index_for(db, [])
        assert second is not first
        assert [f["flag"] for f in news_signals.signals_for(second, "Kyle Monangai", "CHI")] \
            == ["lead_role"]

    def test_usage_week_to_week_and_outside_shot(self):
        hits = news_signals.classify("Love has had an oscillating workload from week-to-week.",
                                     "x", {}, "x")
        assert hits == []
        hits = news_signals.classify("Jackson has an outside shot at suiting up in Week 5.",
                                     "x", {}, "x")
        assert [h["flag"] for h in hits] == ["unlikely_to_play"]

    def test_a_lead_role_asked_about_is_not_one(self):
        text = ("The situation will be monitored to get a sense of who among the duo will be "
                "Chicago's lead runner this weekend.")
        assert news_signals.classify(text, "x", {}, "x") == []
        text = "It's fair to wonder if Swift will step back into the clear lead role."
        assert news_signals.classify(text, "x", {}, "x") == []
        assert [h["flag"] for h in news_signals.classify(
            "Wilson took over as the lead back.", "x", {}, "x")] == ["lead_role"]

    def test_week_start(self):
        assert news_signals.week_start(NOW) == datetime(2026, 10, 6, 9, tzinfo=UTC)
        tue_early = datetime(2026, 10, 6, 8, tzinfo=UTC)
        assert news_signals.week_start(tue_early) == datetime(2026, 9, 29, 9, tzinfo=UTC)

    def test_player_news_reads_the_store(self, db):
        db.upsert_player_news([_row(pid="6790", team="CHI", name="D'Andre Swift",
                                    text="Swift was benched after his fumble.",
                                    published=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                                    url="https://x/swift")])
        flags = news_signals.player_news(db, "D'Andre Swift", "CHI")
        assert [(f["flag"], f["url"]) for f in flags] == [("benched", "https://x/swift")]

    def test_projection_status_lookup_reads_the_store(self, db):
        from nfl_mcp.projections import ProjectionEngine
        db.upsert_player_news([_row(pid="6790", team="CHI", name="D'Andre Swift",
                                    text="Swift was benched after his fumble.",
                                    published=(datetime.now(UTC) - timedelta(days=1)).isoformat())])
        engine = ProjectionEngine.__new__(ProjectionEngine)
        engine.db = db
        lookup = engine._status_lookup()
        assert [f["flag"] for f in news_signals.signals_for(lookup.news, "D'Andre Swift", "CHI")] \
            == ["benched"]


class TestTimeline:
    def test_near_duplicates_across_sources_merge(self):
        rows = [
            {"source": "cbs", "headline": "Swift (hip/knee) returned to practice on a limited "
             "basis Thursday, Sean Hammond of the Chicago Tribune reports.", "text": "",
             "published_at": "2026-10-08T19:32:00+00:00", "url": "https://cbs/x"},
            {"source": "espn_fantasy", "headline": "Swift (hip/knee) returned to practice on a "
             "limited basis Thursday, Sean Hammond of the Chicago Tribune reports.",
             "text": "He is trending toward playing.", "published_at": "2026-10-08T19:20:00+00:00",
             "url": "https://espn/x"},
            {"source": "nbc", "headline": "Swift was benched after a fumble.", "text": "",
             "published_at": "2026-10-05T19:20:00+00:00", "url": "https://nbc/x"},
        ]
        timeline = news_sources.merge_timeline(rows)
        assert len(timeline) == 2
        assert timeline[0]["source"] == "cbs"
        assert [s["source"] for s in timeline[0]["sources"]] == ["cbs", "espn_fantasy"]
        assert timeline[1]["source"] == "nbc"


# ---------------------------------------------------------------------------
# get_player_news
# ---------------------------------------------------------------------------

class TestGetPlayerNews:
    @pytest.fixture
    def stored(self, db, monkeypatch):
        monkeypatch.setattr(player_news_tools, "get_shared_db", lambda *a, **k: db)
        now = datetime.now(UTC)
        db.upsert_player_news([
            _row(pid="4881", source="espn_fantasy",
                 text='Jackson appears "unlikely" to play Sunday and could miss multiple games.',
                 published=(now - timedelta(hours=30)).isoformat(), url="https://espn/l"),
            _row(pid="4881", source="nbc",
                 text='Jackson appears "unlikely" to play Sunday and could miss multiple games.',
                 published=(now - timedelta(hours=29)).isoformat(), url="https://nbc/l"),
            _row(pid="6790", source="nbc", team="CHI", name="D'Andre Swift",
                 text="Swift was benched after his fumble.",
                 published=(now - timedelta(days=2)).isoformat(), url="https://nbc/s"),
            _row(pid="6790", source="nbc", team="CHI", name="D'Andre Swift",
                 text="An old note.", published=(now - timedelta(days=20)).isoformat()),
        ])
        db.record_news_fetch("nbc", 3, 3, now.isoformat())
        return db

    @pytest.mark.asyncio
    async def test_names_and_ids(self, stored):
        out = await player_news_tools.get_player_news(["Lamar Jackson", "6790", "Nobody Here"])
        assert out["success"] is True
        lamar, swift = out["players"]
        assert lamar["player_id"] == "4881" and lamar["position"] == "QB"
        assert lamar["items"] == 1  # two sources, one note
        assert {s["source"] for s in lamar["timeline"][0]["sources"]} == {"espn_fantasy", "nbc"}
        assert set(lamar["timeline"][0]["flags"]) == {"unlikely_to_play", "week_to_week"}
        assert {f["flag"] for f in lamar["news_flags"]} >= {"week_to_week"}
        assert swift["name"] == "D'Andre Swift" and swift["items"] == 1  # the 20-day note is out
        assert swift["news_flags"][0]["url"] == "https://nbc/s"
        assert swift["news_adjustment"]["model_mult"] < 1
        assert out["unresolved"] == [{"name": "Nobody Here",
                                      "reason": "no player by that name in the athlete cache"}]
        assert out["sources"]["nbc"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_league_wide(self, stored, monkeypatch):
        from nfl_mcp import sleeper_tools

        async def rosters(league_id, force_refresh=False):
            return {"success": True, "rosters": [{"roster_id": 1, "players": ["6790", "7526"]},
                                                 {"roster_id": 2, "players": ["13524"]}]}
        monkeypatch.setattr(sleeper_tools, "get_rosters", rosters)
        out = await player_news_tools.get_player_news(league_id="123")
        # Only rostered fantasy players with news: Swift (Flowers has none, the LB is IDP).
        assert [p["name"] for p in out["players"]] == ["D'Andre Swift"]

    @pytest.mark.asyncio
    async def test_league_resolves_a_shared_name(self, stored, monkeypatch):
        from nfl_mcp import sleeper_tools

        async def rosters(league_id, force_refresh=False):
            return {"success": True, "rosters": [{"roster_id": 1, "players": ["13524"]}]}
        monkeypatch.setattr(sleeper_tools, "get_rosters", rosters)
        out = await player_news_tools.get_player_news(["Justin Jefferson"], league_id="9")
        assert out["players"][0]["player_id"] == "13524"

    @pytest.mark.asyncio
    async def test_empty_store_says_how_to_fill_it(self, db, monkeypatch):
        monkeypatch.setattr(player_news_tools, "get_shared_db", lambda *a, **k: db)
        out = await player_news_tools.get_player_news(["Lamar Jackson"])
        assert out["success"] is True and out["players"][0]["items"] == 0
        assert "refresh_data" in out["warnings"][0]

    @pytest.mark.asyncio
    async def test_nothing_asked(self, db, monkeypatch):
        monkeypatch.setattr(player_news_tools, "get_shared_db", lambda *a, **k: db)
        out = await player_news_tools.get_player_news([])
        assert out["success"] is False


# ---------------------------------------------------------------------------
# Refresh scope, prefetch cadence, CBS tool, backtest
# ---------------------------------------------------------------------------

class TestWiring:
    @pytest.mark.asyncio
    async def test_news_scope(self, db, monkeypatch):
        from nfl_mcp import data_refresh

        async def fake(database, **kw):
            return {"fetched": 5, "written": 2, "unresolved": 1, "sources": {"nbc": {}}}
        monkeypatch.setattr(news_sources, "ingest_news", fake)
        out = await data_refresh.run_scope("news", db, 2026, 5)
        assert out["status"] == "ok" and out["written"] == 2 and out["unresolved"] == 1
        assert "news" in data_refresh.REFRESH_SCOPES

    def test_prefetch_cadence(self, monkeypatch):
        from nfl_mcp import server
        monkeypatch.setattr(server, "PREFETCH_NEWS_INTERVAL_SECONDS", 2700)
        monkeypatch.setattr(server, "PREFETCH_NEWS_GAMEDAY_INTERVAL_SECONDS", 900)
        thursday_noon = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)  # 12:00 ET
        sunday_noon = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)
        assert server._news_due(thursday_noon, None)
        assert not server._news_due(thursday_noon, thursday_noon - timedelta(minutes=16))
        assert server._news_due(thursday_noon, thursday_noon - timedelta(minutes=44))
        assert server._news_due(sunday_noon, sunday_noon - timedelta(minutes=14))
        assert server._cycle_scopes(5, 3, news_due=True)[-1] == "news"
        assert "news" not in server._cycle_scopes(5, 3)

    @pytest.mark.asyncio
    async def test_cbs_tool_uses_the_real_list(self, monkeypatch):
        from nfl_mcp import cbs_fantasy_tools

        def client(*a, **k):
            return httpx.AsyncClient(transport=_transport())
        monkeypatch.setattr(cbs_fantasy_tools, "create_http_client", client)
        out = await cbs_fantasy_tools.get_cbs_player_news(limit=1)
        assert out["success"] is True and out["total_news"] == 1
        item = out["news"][0]
        assert item["player"] == "D'Andre Swift" and item["team"] == "CHI"
        assert item["position"] == "RB" and "benched" in item["description"]

    def test_backtest_reads_news_published_before_kickoff(self):
        from evals.backtest.signal_history import news_at_kickoff
        ko = datetime(2026, 10, 4, 17, tzinfo=UTC)
        items = [
            {**_row(pid="6790", team="CHI", name="D'Andre Swift",
                    text="Swift was benched after his fumble.",
                    published="2026-10-03T12:00:00+00:00"), "recorded_at": "2026-10-09T00:00:00"},
            {**_row(pid="12520", team="CHI", name="Kyle Monangai",
                    text="Monangai will be the lead back.",
                    published="2026-10-05T12:00:00+00:00"), "recorded_at": "2026-10-09T00:00:00"},
        ]
        index = news_at_kickoff([], {(5, "CHI"): ko}, items)[(5, "CHI")]
        assert [f["flag"] for f in signals(index, "D'Andre Swift")] == ["benched"]
        assert signals(index, "Kyle Monangai") == []


def signals(index, name, team="CHI"):
    return news_signals.signals_for(index, name, team)
