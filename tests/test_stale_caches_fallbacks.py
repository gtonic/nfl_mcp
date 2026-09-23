"""Regression tests: fallbacks must never be cached, persisted or served as fresh.

Network is always mocked.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from nfl_mcp import matchup_tools, opportunity_tools
from nfl_mcp import player_values as pv
from nfl_mcp.database import NFLDatabase
from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer, season_cache_fresh
from nfl_mcp.sleeper_enrichment import _cached_defense_rankings, _enrich_usage_and_opponent
from nfl_mcp.vegas_tools import VegasLinesAnalyzer


def _temp_db():
    return NFLDatabase(tempfile.mktemp(suffix=".db"))


def _client(resp=None, exc=None):
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp, side_effect=exc)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _real_rankings():
    teams = list(matchup_tools.ESPN_TEAM_MAP.values())
    return {
        pos: [
            {"team": t, "rank": i, "points_allowed_avg": 10.0 + i,
             "matchup_tier": matchup_tools._get_matchup_tier(i), "is_provisional": False}
            for i, t in enumerate(sorted(teams), 1)
        ]
        for pos in ("QB", "RB", "WR", "TE")
    }


# ---------------------------------------------------------------------------
# Bug 1: defense-ranking fallbacks
# ---------------------------------------------------------------------------

class TestDefenseFallbackNotPersisted:
    @pytest.mark.asyncio
    async def test_outage_does_not_write_placeholders_to_db(self):
        db = _temp_db()
        an = DefenseRankingsAnalyzer(db=db)
        with patch("nfl_mcp.matchup_tools.create_http_client",
                   return_value=_client(exc=RuntimeError("nflverse down"))):
            rankings = await an.fetch_defense_rankings(2026)
        assert all(r["is_fallback"] for r in rankings["QB"])
        assert db.get_defense_rankings(2026, max_age_hours=24 * 7) == {}

    def test_upsert_skips_fallback_entries(self):
        db = _temp_db()
        an = DefenseRankingsAnalyzer(db=None)
        assert db.upsert_defense_rankings({"QB": an._get_fallback_rankings("QB")}, 2026) == 0

    def test_provisional_flag_round_trips(self):
        db = _temp_db()
        real = _real_rankings()
        for rows in real.values():
            for r in rows:
                r["is_provisional"] = True
        db.upsert_defense_rankings(real, 2026)
        back = db.get_defense_rankings(2026)
        assert back["QB"][0]["is_provisional"] is True

    @pytest.mark.asyncio
    async def test_outage_serves_last_real_rankings_from_db(self):
        db = _temp_db()
        db.upsert_defense_rankings(_real_rankings(), 2026)
        an = DefenseRankingsAnalyzer(db=db)
        with patch("nfl_mcp.matchup_tools.create_http_client",
                   return_value=_client(exc=RuntimeError("nflverse down"))):
            rankings = await an.fetch_defense_rankings(2026)
        assert not any(r.get("is_fallback") for r in rankings["QB"])
        assert {r["rank"] for r in rankings["QB"]} == set(range(1, 33))

    @pytest.mark.asyncio
    async def test_fallback_cached_briefly_then_retried(self):
        an = DefenseRankingsAnalyzer(db=None)
        client = _client(exc=RuntimeError("nflverse down"))
        with patch("nfl_mcp.matchup_tools.create_http_client", return_value=client):
            await an.fetch_defense_rankings(2026)
            await an.fetch_defense_rankings(2026)
            assert client.get.call_count == 1  # short TTL still holds
            entry = an._rankings_cache["defense_rankings_2026"]
            entry["timestamp"] -= timedelta(minutes=an._fallback_ttl_minutes + 1)
            await an.fetch_defense_rankings(2026)
        assert client.get.call_count == 2  # well within 6h, but retried

    def test_migration_purges_legacy_placeholder_rows(self):
        path = tempfile.mktemp(suffix=".db")
        db = NFLDatabase(path)
        an = DefenseRankingsAnalyzer(db=None)
        # Simulate a pre-v13 write of the placeholder table.
        with db._pool.get_connection() as conn:
            for r in an._get_fallback_rankings("WR"):
                conn.execute(
                    "INSERT INTO defense_rankings (season, week, team, position, rank, "
                    "points_allowed_avg, matchup_tier, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (2026, 0, r["team"], "WR", 16, 15.0, "neutral", datetime.now(UTC).isoformat()),
                )
            conn.commit()
            db._migration_v13_defense_rankings_flags(conn)
            conn.commit()
        assert db.get_defense_rankings(2026) == {}

    def test_enrichment_placeholder_in_memory_does_not_shadow_db(self):
        an = DefenseRankingsAnalyzer(db=None)
        an._rankings_cache["defense_rankings_2026"] = {
            "data": {p: an._get_fallback_rankings(p) for p in ("QB", "WR")},
            "timestamp": datetime.now(UTC),
            "is_fallback": True,
        }
        db = MagicMock()
        db.get_defense_rankings.return_value = _real_rankings()
        assert _cached_defense_rankings(an, db, 2026) == _real_rankings()

    def test_enrichment_flags_fallback_matchup(self):
        db = MagicMock()
        db.get_player_snap_pct.return_value = None
        db.get_week_kickoffs.return_value = {}
        db.get_opponent.return_value = "KC"
        db.find_player_injury.return_value = None
        db.get_latest_practice_status.return_value = None
        db.get_usage_last_n_weeks.return_value = None
        db.get_defense_rankings.return_value = {}
        an = DefenseRankingsAnalyzer(db=None)
        athlete = {"id": "1", "full_name": "X", "position": "WR", "team_id": "DAL"}
        with patch("nfl_mcp.matchup_tools.get_defense_analyzer", return_value=an):
            out = _enrich_usage_and_opponent(db, athlete, 2026, 3)
        assert out["matchup_source"] == "fallback"
        assert out["matchup_is_fallback"] is True


# ---------------------------------------------------------------------------
# Bug 2: season caches and value/odds fallbacks
# ---------------------------------------------------------------------------

class TestSeasonCacheTTL:
    def test_past_season_never_expires(self):
        now = datetime(2026, 11, 1, tzinfo=UTC)
        assert season_cache_fresh(2025, now - timedelta(days=60), now)

    def test_current_season_expires(self):
        now = datetime(2026, 11, 1, tzinfo=UTC)
        assert season_cache_fresh(2026, now - timedelta(minutes=30), now)
        assert not season_cache_fresh(2026, now - timedelta(hours=4), now)

    def test_january_belongs_to_previous_season(self):
        now = datetime(2027, 1, 10, tzinfo=UTC)
        assert not season_cache_fresh(2026, now - timedelta(hours=4), now)

    @pytest.mark.asyncio
    async def test_game_logs_refetched_after_ttl(self):
        season = matchup_tools._nfl_season_now()
        resp = Mock(status_code=200, text="season_type,position,player_id,week\n")
        resp.raise_for_status = Mock()
        client = _client(resp)
        opportunity_tools._logs_cache.pop(season, None)
        with patch("nfl_mcp.opportunity_tools.create_http_client", return_value=client):
            await opportunity_tools._fetch_game_logs(season)
            await opportunity_tools._fetch_game_logs(season)
            assert client.get.call_count == 1
            ts, logs = opportunity_tools._logs_cache[season]
            opportunity_tools._logs_cache[season] = (ts - timedelta(hours=4), logs)
            await opportunity_tools._fetch_game_logs(season)
        assert client.get.call_count == 2
        opportunity_tools._logs_cache.pop(season, None)

    @pytest.mark.asyncio
    async def test_offense_rankings_refetched_after_ttl(self):
        season = matchup_tools._nfl_season_now()
        resp = Mock(status_code=200, text="season_type,position,team,week,fantasy_points_ppr\n"
                                          "REG,QB,KC,1,20.0\n")
        resp.raise_for_status = Mock()
        client = _client(resp)
        matchup_tools._offense_rankings_cache.pop(season, None)
        with patch("nfl_mcp.matchup_tools.create_http_client", return_value=client):
            await matchup_tools.fetch_offense_rankings(season)
            await matchup_tools.fetch_offense_rankings(season)
            assert client.get.call_count == 1
            ts, data = matchup_tools._offense_rankings_cache[season]
            matchup_tools._offense_rankings_cache[season] = (ts - timedelta(hours=4), data)
            await matchup_tools.fetch_offense_rankings(season)
        assert client.get.call_count == 2
        matchup_tools._offense_rankings_cache.pop(season, None)


class TestPlayerValuesStaleFallback:
    @pytest.mark.asyncio
    async def test_db_fallback_stays_stale_and_retries(self):
        db = _temp_db()
        entries = [{"player_id": "9509", "name": "Bijan Robinson", "position": "RB",
                    "team": "ATL", "value": 10000, "overall_rank": 1, "position_rank": 1,
                    "source": "fantasycalc"}]
        db.upsert_player_values(entries, pv.build_format_key(1.0, 1, 12, False))
        svc = pv.PlayerValuesService(db=db)
        down = AsyncMock(side_effect=RuntimeError("api down"))
        with patch.object(svc, "_fetch_from_fantasycalc", down):
            first = await svc.get_values(1.0, 1, 12, False)
            second = await svc.get_values(1.0, 1, 12, False)
        assert first["stale"] is True and first["source"] == "db_cache"
        # The memory copy must not turn into a fresh FantasyCalc answer.
        assert second["stale"] is True and second["source"] == "db_cache"
        assert down.call_count == 1  # short retry window holds

        key = pv.build_format_key(1.0, 1, 12, False)
        svc._mem[key]["cached_at"] -= timedelta(minutes=pv.STALE_RETRY_MINUTES + 1)
        up = AsyncMock(return_value=entries)
        with patch.object(svc, "_fetch_from_fantasycalc", up):
            third = await svc.get_values(1.0, 1, 12, False)
        assert up.call_count == 1  # retried long before the 12h TTL
        assert third["stale"] is False and third["source"] == "fantasycalc"


def _odds_client(payload):
    resp = Mock(status_code=200, headers={})
    resp.json = Mock(return_value=payload)
    resp.raise_for_status = Mock()
    return _client(resp)


class TestVegasFallbacks:
    @pytest.mark.asyncio
    async def test_empty_answer_backs_off(self):
        an = VegasLinesAnalyzer(api_key="k")
        client = _odds_client([])
        with patch("nfl_mcp.vegas_tools.httpx.AsyncClient", return_value=client):
            assert await an.fetch_current_lines() == {}
            assert await an.fetch_current_lines() == {}
        assert client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_error_backs_off(self):
        import httpx
        an = VegasLinesAnalyzer(api_key="k")
        client = _client(exc=httpx.ConnectError("down"))
        with patch("nfl_mcp.vegas_tools.httpx.AsyncClient", return_value=client):
            await an.fetch_current_lines()
            await an.fetch_current_lines()
        assert client.get.call_count == 1

    @staticmethod
    def _game(markets):
        kickoff = (datetime.now(UTC) + timedelta(days=2)).isoformat().replace("+00:00", "Z")
        return {
            "home_team": "Kansas City Chiefs", "away_team": "Buffalo Bills",
            "commence_time": kickoff, "bookmakers": [{"markets": markets}],
        }

    _SPREADS = {"key": "spreads", "outcomes": [
        {"name": "Kansas City Chiefs", "point": -3.0},
        {"name": "Buffalo Bills", "point": 3.0},
    ]}
    _TOTALS = {"key": "totals", "outcomes": [{"name": "Over", "point": 48.5},
                                             {"name": "Under", "point": 48.5}]}

    @pytest.mark.asyncio
    async def test_missing_total_keeps_the_real_spread(self):
        """Only the total is a fallback; the spread is the book's."""
        an = VegasLinesAnalyzer(api_key="k")
        with patch("nfl_mcp.vegas_tools.httpx.AsyncClient",
                   return_value=_odds_client([self._game([self._SPREADS])])):
            lines = await an.fetch_current_lines()
        line = lines["KC"]
        assert line["home_spread"] == -3.0 and line["away_spread"] == 3.0
        assert line["home_is_favorite"] is True
        assert not line.get("is_fallback")
        assert line["total_is_fallback"] is True
        assert "spread_is_fallback" not in line
        # No invented 45.0 and no implied totals split from it.
        assert line["total"] is None
        assert line["home_implied_total"] is None and line["away_implied_total"] is None
        assert line["game_environment"]["tier"] == "unknown"
        assert line["home_game_script"]["projection"] == "slight_favorite"

    @pytest.mark.asyncio
    async def test_missing_spread_flags_only_the_spread(self):
        an = VegasLinesAnalyzer(api_key="k")
        with patch("nfl_mcp.vegas_tools.httpx.AsyncClient",
                   return_value=_odds_client([self._game([self._TOTALS])])):
            lines = await an.fetch_current_lines()
        line = lines["KC"]
        assert line["total"] == 48.5 and line["home_implied_total"] == 24.2
        assert line["spread_is_fallback"] is True
        assert not line.get("is_fallback") and "total_is_fallback" not in line

    @pytest.mark.asyncio
    async def test_no_markets_is_a_full_fallback(self):
        an = VegasLinesAnalyzer(api_key="k")
        with patch("nfl_mcp.vegas_tools.httpx.AsyncClient",
                   return_value=_odds_client([self._game([])])):
            lines = await an.fetch_current_lines()
        assert lines["KC"]["is_fallback"] is True

    @pytest.mark.asyncio
    async def test_spread_only_game_is_safe_for_consumers(self):
        from nfl_mcp import vegas_tools
        an = VegasLinesAnalyzer(api_key="k")
        with patch("nfl_mcp.vegas_tools.httpx.AsyncClient",
                   return_value=_odds_client([self._game([self._SPREADS])])):
            await an.fetch_current_lines()
        with patch.object(vegas_tools, "get_vegas_analyzer", return_value=an):
            env = await vegas_tools.get_game_environment("KC")
            stacks = await vegas_tools.get_stack_opportunities()
            listing = await vegas_tools.get_vegas_lines()
        assert env["success"] is True
        assert env["spread"] == -3.0 and env["total_is_fallback"] is True
        assert env["is_fallback"] is False
        assert stacks["total_opportunities"] == 0
        assert listing["success"] is True and listing["total_games"] == 1

    def test_spread_only_game_projects_with_a_neutral_environment(self):
        from nfl_mcp.projections import _environment_mult
        # implied None, not a fallback: neutral, same as no line at all.
        assert _environment_mult(None, False) == _environment_mult(None, True)


# ---------------------------------------------------------------------------
# Bug 3: in-progress week snaps
# ---------------------------------------------------------------------------

def _snap_db(kickoff):
    db = MagicMock()
    db.get_week_kickoffs.return_value = {"DAL": kickoff} if kickoff else {}
    db.get_player_snap_pct.side_effect = lambda pid, season, week: (
        {"snap_pct": 20.0} if week == 5 else {"snap_pct": 85.0}
    )
    db.get_opponent.return_value = None
    db.find_player_injury.return_value = None
    db.get_latest_practice_status.return_value = None
    db.get_usage_last_n_weeks.return_value = None
    return db


class TestInProgressSnaps:
    athlete = {"id": "1", "full_name": "X", "position": "WR", "team_id": "DAL"}

    def test_game_in_progress_uses_previous_week(self):
        kickoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        out = _enrich_usage_and_opponent(_snap_db(kickoff), self.athlete, 2026, 5)
        assert out["snap_pct"] == 85.0
        assert out["snap_pct_week"] == 4

    def test_unknown_kickoff_uses_previous_week(self):
        out = _enrich_usage_and_opponent(_snap_db(None), self.athlete, 2026, 5)
        assert out["snap_pct_week"] == 4

    def test_final_game_uses_current_week(self):
        kickoff = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        out = _enrich_usage_and_opponent(_snap_db(kickoff), self.athlete, 2026, 5)
        assert out["snap_pct"] == 20.0
        assert out["snap_pct_week"] == 5
