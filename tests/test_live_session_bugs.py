"""Regressions from the 2026-10-07/08 live session.

- analyze_lineup told a manager to "Fill empty WR with Emanuel Wilson" (an RB)
  while its own optimal lineup had Wilson at FLEX and Washington moved to WR;
  and at 99.3% efficiency it suggested nothing although the optimal lineup
  started a 9.5 bench player over an 8.7 FLEX.
- get_transactions(week=5) on Wednesday of week 5 returned nothing: Sleeper
  files that morning's waiver run under leg 4.
- A manual `_fetch_injuries()` returned 0 rows (NFL_MCP_ADVANCED_ENRICH not in
  the environment), there was no way to refresh stale feeds on demand, and the
  injury crawl took ~30 minutes.
- analyze_trade reported a QB need of 5 for a team whose lineup a QB lowered.
"""
import asyncio
from pathlib import Path

import pytest

from nfl_mcp import config, data_refresh, sleeper_enrichment, sleeper_transactions
from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp.trade_analyzer_tools import TradeAnalyzer, _ros_deltas

pytestmark = pytest.mark.usefixtures("offline_sources")  # no ambient network reads


# --------------------------------------------------------------------------
# analyze_lineup: slot-legal fills, displaced-starter pairing, marginal swaps
# --------------------------------------------------------------------------

@pytest.fixture
def offline_optimizer(monkeypatch):
    from tests.test_lineup_slots import _analyzer
    monkeypatch.setattr(lo, "get_lineup_optimizer",
                        lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                   defense_analyzer=_analyzer()))

    async def _state(db=None):
        return {"season": 2026, "week": 5, "source": "nfl_state"}
    monkeypatch.setattr("nfl_mcp.week_context.current_season_week", _state)


def _e(name, position, points):
    return {"name": name, "position": position, "team": "KC", "opponent": "OPP",
            "projection": {"projected_points": points}}


class TestLineupSuggestions:
    @pytest.mark.asyncio
    async def test_an_rb_never_fills_a_wr_seat(self, offline_optimizer):
        """The live case: WR2 dropped, Washington (WR) at FLEX, Wilson (RB) benched."""
        out = await lo.analyze_full_lineup({
            "WR": [_e("Other WR", "WR", 12.0)],
            "FLEX": [_e("Washington", "WR", 10.0), _e("Flex RB", "RB", 9.0)],
            "BENCH": [_e("Emanuel Wilson", "RB", 8.0)],
        }, empty_slots=["WR"])
        optimal = {(r["slot"], r["player"]) for r in out["optimal_lineup"]}
        assert ("WR", "Washington") in optimal and ("FLEX", "Emanuel Wilson") in optimal

        [fill] = out["suggested_changes"]
        assert fill["action"] == "fill" and fill["bench_in"] == "Emanuel Wilson"
        assert lo.slot_accepts(fill["slot"], "RB") and fill["slot"] == "FLEX"
        assert fill["empty_slot"] == "WR"
        assert fill["moves"] == [{"player": "Washington", "from_slot": "FLEX", "to_slot": "WR"}]
        assert "move Washington FLEX→WR" in fill["reason"]
        assert "Fill empty WR with Emanuel Wilson" not in fill["reason"]

    @pytest.mark.asyncio
    async def test_every_suggested_seat_is_legal(self, offline_optimizer):
        out = await lo.analyze_full_lineup({
            "QB": [_e("QB1", "QB", 20.0)],
            "RB": [_e("RB1", "RB", 15.0)],
            "WR": [_e("WR1", "WR", 14.0)],
            "TE": [_e("TE1", "TE", 7.0)],
            "FLEX": [_e("Flex WR", "WR", 11.0)],
            "BENCH": [_e("Bench RB", "RB", 10.0), _e("Bench TE", "TE", 3.0)],
        }, empty_slots=["RB", "WR", "FLEX"])
        positions = {"Bench RB": "RB", "Bench TE": "TE"}
        for change in out["suggested_changes"]:
            assert lo.slot_accepts(change["slot"], positions[change["bench_in"]]), change
        started = {r["player"] for r in out["optimal_lineup"]}
        assert {c["bench_in"] for c in out["suggested_changes"]} == started & set(positions)

    @pytest.mark.asyncio
    async def test_each_swap_names_the_starter_it_displaces(self, offline_optimizer):
        """Sorting newcomers against the cheapest starters paired the bench RB
        with the WR (+17) and dropped the RB swap as a "loss" (-10)."""
        out = await lo.analyze_full_lineup({
            "RB": [_e("Starting RB", "RB", 14.0)],
            "WR": [_e("Starting WR", "WR", 3.0)],
            "BENCH": [_e("Bench RB", "RB", 20.0), _e("Bench WR", "WR", 6.0)],
        })
        pairs = {(c["bench_in"], c["bench_out"], c["gain"]) for c in out["suggested_changes"]}
        assert pairs == {("Bench RB", "Starting RB", 6.0), ("Bench WR", "Starting WR", 3.0)}

    @pytest.mark.asyncio
    async def test_a_sub_threshold_swap_is_listed_as_marginal(self, offline_optimizer):
        out = await lo.analyze_full_lineup({
            "WR": [_e("WR1", "WR", 15.0)],
            "FLEX": [_e("Flex Starter", "WR", 8.7)],
            "BENCH": [_e("Bench RB", "RB", 9.5)],
        })
        assert out["suggested_changes"] == []
        [marginal] = out["marginal_changes"]
        assert (marginal["bench_in"], marginal["bench_out"]) == ("Bench RB", "Flex Starter")
        assert marginal["gain"] == pytest.approx(0.8)
        assert "marginal" in out["message"]
        assert out["lineup_efficiency_pct"] < 100

    @pytest.mark.asyncio
    async def test_a_cosmetic_seat_shuffle_is_not_a_move(self, offline_optimizer):
        out = await lo.analyze_full_lineup({
            "WR": [_e("Weak", "WR", 2.0), _e("Okay", "WR", 3.0)],
            "BENCH": [_e("Good", "WR", 15.0)],
        })
        [swap] = out["suggested_changes"]
        assert swap["bench_out"] == "Weak" and swap["moves"] == []


# --------------------------------------------------------------------------
# get_transactions: the Wednesday waiver run sits in the previous leg
# --------------------------------------------------------------------------

def _tx(tid, leg, when):
    return {"transaction_id": tid, "leg": leg, "type": "waiver", "status": "complete",
            "status_updated": when, "adds": {"p" + tid: 1}, "drops": None}


@pytest.fixture
def legs(monkeypatch):
    data = {5: [], 4: [_tx("a", 4, 300), _tx("b", 4, 200)], 3: [_tx("c", 3, 100)]}
    calls = []

    async def _fetch(league_id, week, auto_inferred):
        calls.append(week)
        return {"success": True, "transactions": [dict(t) for t in data[week]],
                "week": week, "count": len(data[week]), "auto_week_inferred": auto_inferred}

    async def _state():
        return {"success": True, "nfl_state": {"week": 5, "display_week": 4, "season": "2026"}}

    monkeypatch.setattr(sleeper_transactions, "_fetch_week", _fetch)
    monkeypatch.setattr(sleeper_transactions, "get_nfl_state", _state)
    return data, calls


class TestTransactionLegs:
    @pytest.mark.asyncio
    async def test_current_week_includes_the_previous_leg(self, legs):
        out = await sleeper_transactions.get_transactions("L", week=5, include_previous_leg=None)
        assert out["success"] is True
        assert [t["transaction_id"] for t in out["transactions"]] == ["a", "b"]
        assert out["legs"] == [5, 4] and out["count"] == 2 and "leg 4" in out["leg_note"]

    @pytest.mark.asyncio
    async def test_no_week_means_current_and_previous(self, legs):
        out = await sleeper_transactions.get_transactions("L", include_previous_leg=None)
        assert sorted(legs[1]) == [4, 5] and out["count"] == 2

    @pytest.mark.asyncio
    async def test_a_past_week_is_read_alone(self, legs):
        out = await sleeper_transactions.get_transactions("L", week=3, include_previous_leg=None)
        assert legs[1] == [3] and out["count"] == 1 and "legs" not in out

    @pytest.mark.asyncio
    async def test_default_reads_one_leg_for_multi_week_callers(self, legs):
        out = await sleeper_transactions.get_transactions("L", week=5)
        assert legs[1] == [5] and out["count"] == 0

    @pytest.mark.asyncio
    async def test_the_mcp_tool_merges_without_a_week(self, legs):
        from nfl_mcp import tool_registry
        out = await tool_registry.get_transactions("123456789")
        assert out["legs"] == [5, 4] and out["count"] == 2

    def test_rows_are_deduped_and_a_failed_leg_is_reported(self):
        current = {"success": False, "error": "timeout", "transactions": [_tx("a", 4, 3)]}
        previous = {"success": True, "transactions": [_tx("a", 4, 3), _tx("b", 4, 2)]}
        out = sleeper_transactions._merge_legs(current, previous, 5)
        assert [t["transaction_id"] for t in out["transactions"]] == ["a", "b"]
        assert out["success"] is True and out["current_leg_error"] == "timeout"


# --------------------------------------------------------------------------
# refresh_data and the enrichment gate
# --------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, ages=None):
        self.ages = ages or {}
        self.calls = []

    def get_data_freshness(self):
        return {feed: {"updated_at": None, "age_hours": age} for feed, age in
                {"injuries": None, "athletes": None, "practice_status": None, **self.ages}.items()}

    def upsert_injuries(self, rows, prune_missing=False, complete_teams=None):
        self.calls.append(("injuries", len(rows), prune_missing, set(complete_teams or ())))
        return len(rows)

    def upsert_practice_status(self, rows):
        self.calls.append(("practice", len(rows)))
        return len(rows)


@pytest.fixture
def enrich_off(monkeypatch):
    monkeypatch.setattr(sleeper_enrichment, "ADVANCED_ENRICH_ENABLED", False)
    monkeypatch.delenv("NFL_MCP_ADVANCED_ENRICH", raising=False)

    async def _state(db=None):
        return {"season": 2026, "week": 5, "source": "nfl_state"}
    monkeypatch.setattr("nfl_mcp.week_context.current_season_week", _state)
    monkeypatch.setattr(data_refresh, "_jobs", {})
    monkeypatch.setattr(data_refresh, "_tasks", {})
    monkeypatch.setattr(data_refresh, "_scope_owner", {})


@pytest.fixture
def crawl(monkeypatch):
    rows = [{"player_id": "1", "player_name": "A", "team_id": "KC", "injury_status": "Out"}]

    async def _crawl(teams=None, db=None):
        return rows, {"KC"}
    monkeypatch.setattr("nfl_mcp.injury_service.crawl_injury_reports", _crawl)
    return rows


class TestRefreshData:
    @pytest.mark.asyncio
    async def test_the_enrichment_gate_is_bypassed_by_force_only(self, enrich_off, crawl):
        assert await sleeper_enrichment._fetch_injuries() == []
        assert await sleeper_enrichment._fetch_injuries(force=True) == crawl

    @pytest.mark.asyncio
    async def test_injuries_refresh_like_the_prefetch(self, enrich_off, crawl):
        db = _FakeDB()
        out = await data_refresh.refresh_data(["injuries"], db=db)
        assert out["success"] is True and out["status"] == "done"
        scope = out["scopes"]["injuries"]
        assert scope["status"] == "ok" and scope["fetched"] == 1 and scope["written"] == 1
        assert "duration_s" in scope and "freshness" in out
        # Pruned only for the completely crawled teams, as the prefetch does.
        assert db.calls == [("injuries", 1, True, {"KC"})]

    @pytest.mark.asyncio
    async def test_practice_refresh_bypasses_the_gate(self, enrich_off, monkeypatch):
        async def _reports(season, week, db=None):
            return [{"player_name": "A", "team": "KC", "date": "2026-10-07", "status": "DNP"}]
        monkeypatch.setattr("nfl_mcp.practice_reports.fetch_practice_reports", _reports)
        monkeypatch.setattr("nfl_mcp.response_validation.validate_response_and_log",
                            lambda *a, **k: True)
        db = _FakeDB()
        out = await data_refresh.refresh_data(["practice"], db=db)
        assert out["scopes"]["practice"]["written"] == 1 and db.calls == [("practice", 1)]

    @pytest.mark.asyncio
    async def test_a_fresh_feed_is_skipped_unless_forced(self, enrich_off, crawl):
        db = _FakeDB(ages={"injuries": 0.05})
        out = await data_refresh.refresh_data(["injuries"], db=db)
        assert out["scopes"]["injuries"]["status"] == "skipped_fresh" and db.calls == []
        out = await data_refresh.refresh_data(["injuries"], force=True, db=db)
        assert out["scopes"]["injuries"]["status"] == "ok" and len(db.calls) == 1

    @pytest.mark.asyncio
    async def test_unknown_scope_is_rejected(self, enrich_off):
        out = await data_refresh.refresh_data(["injury"], db=_FakeDB())
        assert out["success"] is False and "injuries" in out["error"]

    @pytest.mark.asyncio
    async def test_a_failing_scope_does_not_sink_the_others(self, enrich_off, crawl, monkeypatch):
        async def _boom(*a, **k):
            raise RuntimeError("nfl.com down")
        monkeypatch.setattr(data_refresh, "_REFRESHERS",
                            {**data_refresh._REFRESHERS, "practice": _boom})
        out = await data_refresh.refresh_data(["injuries", "practice"], db=_FakeDB())
        assert out["scopes"]["injuries"]["status"] == "ok"
        assert out["scopes"]["practice"] == {"status": "error", "error": "nfl.com down",
                                             "duration_s": out["scopes"]["practice"]["duration_s"]}
        assert out["success"] is True

    @pytest.mark.asyncio
    async def test_background_job_can_be_polled(self, enrich_off, monkeypatch):
        gate = asyncio.Event()

        async def _slow(db, season, week):
            await gate.wait()
            return {"fetched": 3, "written": 3}
        monkeypatch.setattr(data_refresh, "_REFRESHERS",
                            {**data_refresh._REFRESHERS, "injuries": _slow})
        db = _FakeDB()
        started = await data_refresh.refresh_data(["injuries"], background=True, db=db)
        assert started["status"] == "running" and started["job_id"]
        again = await data_refresh.refresh_data(["injuries"], db=db)
        assert again["scopes"]["injuries"]["status"] == "already_running"
        gate.set()
        await data_refresh._tasks[started["job_id"]]
        polled = await data_refresh.refresh_data(job_id=started["job_id"])
        assert polled["status"] == "done" and polled["scopes"]["injuries"]["written"] == 3

    def test_registered_in_every_profile(self):
        from nfl_mcp import tool_registry
        for profile in tool_registry.TOOL_PROFILES:
            assert "refresh_data" in {t.__name__ for t in tool_registry.get_all_tools(profile)}

    def test_athletes_overdue_reads_the_wall_clock_age(self, monkeypatch):
        from nfl_mcp import server
        monkeypatch.setattr(server, "PREFETCH_ATHLETES_INTERVAL_SECONDS", 86400)
        assert server._athletes_overdue(_FakeDB(ages={"athletes": 43.0})) is True
        assert server._athletes_overdue(_FakeDB(ages={"athletes": 2.0})) is False
        assert server._athletes_overdue(_FakeDB()) is False


class TestInjuryCrawlCost:
    def test_espn_core_has_its_own_faster_limiter(self, monkeypatch):
        monkeypatch.delenv("NFL_MCP_ESPN_CORE_RATE_LIMIT", raising=False)
        monkeypatch.setattr(config, "_rate_limiters", {})
        assert config.rate_limiter_name_for_host("sports.core.api.espn.com") == "espn_core"
        assert config.rate_limiter_name_for_host("site.api.espn.com") == "espn"
        assert config.get_rate_limiter("espn_core").rate * 60 >= 600

    def test_stored_names_seed_the_athlete_cache(self, tmp_path: Path, monkeypatch):
        from nfl_mcp.database import NFLDatabase
        from nfl_mcp.injury_service import InjuryAggregator

        db = NFLDatabase(str(tmp_path / "t.db"))
        db.upsert_injuries([
            {"player_id": "4362759", "player_name": "Nick Bolton", "team_id": "KC",
             "injury_status": "Active"},
            {"player_id": "999", "player_name": "Unknown", "team_id": "KC",
             "injury_status": "Out"},
        ])
        assert db.get_injury_player_names() == {"4362759": "Nick Bolton"}
        monkeypatch.setattr(InjuryAggregator, "_athlete_name_cache", {})
        InjuryAggregator(db=db)._seed_names_from_db()
        assert InjuryAggregator._athlete_name_cache == {"4362759": "Nick Bolton"}

    @pytest.mark.asyncio
    async def test_a_cached_name_skips_the_athlete_request(self, monkeypatch):
        from nfl_mcp.injury_service import InjuryAggregator

        requested = []

        class _Resp:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def json(self):
                return self._data

        class _Client:
            async def get(self, url, **kw):
                requested.append(url)
                return _Resp({"status": "Out", "athlete": {
                    "$ref": "https://sports.core.api.espn.com/v2/sports/football/leagues/"
                            "nfl/seasons/2026/athletes/4362759"}})

        monkeypatch.setattr(InjuryAggregator, "_athlete_name_cache", {"4362759": "Nick Bolton"})
        agg = InjuryAggregator(http_client=_Client())
        report = await agg._fetch_espn_injury_detail(
            "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/2026/"
            "athletes/4362759/injuries/1", {})
        assert report.player_name == "Nick Bolton" and len(requested) == 1


# --------------------------------------------------------------------------
# analyze_trade: positional need from lineup impact
# --------------------------------------------------------------------------

WEEKS = list(range(6, 18))


def _ros(pid, position, per_week):
    weekly = dict.fromkeys(WEEKS, per_week)
    return {"player_id": pid, "player": pid, "position": position,
            "total_points": per_week * len(WEEKS), "ros_points": per_week * len(WEEKS),
            "playoff_points": 0.0, "weekly_points": weekly, "bye_weeks": [], "injury_weeks": []}


class TestTradeNeedFromLineup:
    @pytest.mark.asyncio
    async def test_a_qb_no_better_than_the_incumbent_is_no_need(self, monkeypatch):
        """Team 2 starts Lawrence; receiving Daniels (no better) for a starting
        RB lowers its lineup — that is no QB need, and no fit bonus."""
        from nfl_mcp import ros

        by_id = {
            "daniels": _ros("daniels", "QB", 18.0), "t1_qb2": _ros("t1_qb2", "QB", 17.0),
            "t1_rb": _ros("t1_rb", "RB", 9.0), "t1_wr": _ros("t1_wr", "WR", 12.0),
            "lawrence": _ros("lawrence", "QB", 18.5), "t2_rb": _ros("t2_rb", "RB", 14.0),
            "t2_rb2": _ros("t2_rb2", "RB", 6.0), "t2_wr": _ros("t2_wr", "WR", 13.0),
        }

        async def _ros_for_ids(ids, **kw):
            return ({i: by_id[i] for i in ids if i in by_id},
                    {"windows": {"regular": WEEKS, "playoff": []}, "schedule_unknown_weeks": []})

        async def _state(db=None):
            return {"season": 2026, "week": 6, "source": "nfl_state"}

        monkeypatch.setattr(ros, "ros_for_ids", _ros_for_ids)
        monkeypatch.setattr("nfl_mcp.week_context.current_season_week", _state)
        league = {"roster_positions": ["QB", "RB", "WR", "FLEX", "BN", "BN"], "settings": {}}
        team1 = {"players": ["daniels", "t1_qb2", "t1_rb", "t1_wr"]}
        team2 = {"players": ["lawrence", "t2_rb", "t2_rb2", "t2_wr"]}
        t1_gives = [{"player_id": "daniels", "position": "QB", "calculated_value": 6000,
                     "value_source": "fantasycalc"}]
        t2_gives = [{"player_id": "t2_rb", "position": "RB", "calculated_value": 5000,
                     "value_source": "fantasycalc"}]

        block = await _ros_deltas(league, team1, team2, t1_gives, t2_gives, db=object())

        assert block["team2"]["ros_points_delta"] < 0
        assert block["team2"]["positional_needs"]["QB"] == 0
        assert t1_gives[0]["lineup_fit"] == 0.0  # Daniels would sit behind Lawrence
        # Team 1 (before the trade) has an empty FLEX: every RB point is new.
        assert block["team1"]["positional_needs"]["RB"] == 10
        assert block["team1"]["positional_needs"]["QB"] == 0

        # A count-based score would call that a QB need (one QB on the roster).
        count_needs = TradeAnalyzer()._calculate_positional_needs(
            {"players_enriched": [{"position": "QB"}, {"position": "RB"}]}, league["roster_positions"])
        assert count_needs["QB"] >= 5

        _, _, details = TradeAnalyzer()._evaluate_trade_fairness(
            t1_gives, t2_gives, block["team1"]["positional_needs"],
            block["team2"]["positional_needs"],
            block["team1"]["ros_points_delta"], block["team2"]["ros_points_delta"])
        assert details["team2_need_bonus"] == 0

    def test_a_receiver_whose_lineup_drops_gets_no_bonus(self):
        p = {"position": "QB", "calculated_value": 5000, "value_source": "fantasycalc"}
        q = {"position": "RB", "calculated_value": 5000, "value_source": "fantasycalc"}
        needs = {"QB": 10, "RB": 10}
        _, _, with_ros = TradeAnalyzer()._evaluate_trade_fairness(
            [dict(p)], [dict(q)], needs, needs, 20.0, -15.0)
        assert with_ros["team2_need_bonus"] == 0 and with_ros["team1_need_bonus"] > 0
        _, _, without = TradeAnalyzer()._evaluate_trade_fairness([dict(p)], [dict(q)], needs, needs)
        assert without["team2_need_bonus"] > 0  # no ROS: the count-based fallback
