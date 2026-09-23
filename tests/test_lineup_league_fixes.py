"""Lineup grading, start/sit, locks, and league-level fixes (bye cost, matchups,
replacement levels, playoff simulation, league changes, trades)."""
import contextlib
import random
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import league_changes_tools, lineup_tools, nfl_tools, roster_context, sleeper_tools
from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp import playoff_tools as pt
from nfl_mcp.bye_week_tools import _week_row
from nfl_mcp.roster_needs import replacement_levels, slot_counts, surplus_players
from nfl_mcp.week_context import PLAYING, bye_check
from nfl_mcp.win_probability import optimize_win_probability
from tests.test_league_changes import db, league  # noqa: F401
from tests.test_start_sit_ranks_by_points import _analyzer

pytestmark = pytest.mark.usefixtures("offline_sources")  # no ambient network reads


def _optimizer(monkeypatch):
    monkeypatch.setattr(lo, "get_lineup_optimizer",
                        lambda: lo.LineupOptimizer(db=None, auto_project=False,
                                                   defense_analyzer=_analyzer()))


def _p(name, pos, pts, **extra):
    return {"name": name, "team": "KC", "opponent": "OPP", "position": pos,
            "projection": {"projected_points": pts}, **extra}


class TestEmptySlotsAreSeats:
    @pytest.mark.asyncio
    async def test_an_empty_flex_is_filled_and_costs_the_grade(self, monkeypatch):
        _optimizer(monkeypatch)
        lineup = {"RB": [_p("Starter RB", "RB", 15.0)],
                  "BENCH": [_p("Bench RB", "RB", 12.0)]}
        out = await lo.analyze_full_lineup(lineup, empty_slots=["FLEX"])
        assert out["lineup_grade"] != "A"
        assert out["optimal_projected"] == 27.0
        fill = out["suggested_changes"][0]
        assert fill["action"] == "fill" and fill["slot"] == "FLEX"
        assert fill["bench_in"] == "Bench RB"
        assert "Fill empty FLEX with Bench RB" in fill["reason"]

    @pytest.mark.asyncio
    async def test_a_fill_and_a_swap_together(self, monkeypatch):
        _optimizer(monkeypatch)
        lineup = {"WR": [_p("Weak WR", "WR", 3.0)],
                  "BENCH": [_p("Strong WR", "WR", 19.0), _p("Bench RB", "RB", 10.0)]}
        out = await lo.analyze_full_lineup(lineup, empty_slots=["FLEX"])
        actions = {(c["action"], c["bench_in"]) for c in out["suggested_changes"]}
        assert out["optimal_projected"] == 29.0
        assert ("fill", "Bench RB") in actions or ("fill", "Strong WR") in actions
        assert any(a == "swap" for a, _ in actions)

    @pytest.mark.asyncio
    async def test_analyze_lineup_keeps_slots_aligned_past_an_empty_one(self, monkeypatch):
        players = [
            {"player_id": "q", "name": "QB A", "position": "QB", "team": "KC", "opponent": "LV"},
            {"player_id": "w", "name": "WR A", "position": "WR", "team": "KC", "opponent": "LV"},
            {"player_id": "b", "name": "RB B", "position": "RB", "team": "KC", "opponent": "LV"},
        ]
        ctx = {"league": {"roster_positions": ["QB", "RB", "WR", "BN"]}, "roster": {},
               "roster_id": 1, "season": 2026, "week": 3, "players": players,
               "starters": ["q", "w"], "starters_raw": ["q", "0", "w"], "error": None}
        monkeypatch.setattr(roster_context, "load_roster_players", AsyncMock(return_value=ctx))
        monkeypatch.setattr(sleeper_tools, "get_matchups", AsyncMock(return_value={"matchups": []}))
        captured = {}

        async def _full(**kwargs):
            captured.update(kwargs)
            return {"success": True}
        monkeypatch.setattr(lo, "analyze_full_lineup", _full)
        out = await lineup_tools.analyze_lineup(league_id="L", roster_id=1, db=None)
        built = captured["lineup"]
        assert [p["name"] for p in built["WR"]] == ["WR A"]
        assert "RB" not in built
        assert captured["empty_slots"] == ["RB"] and out["empty_slots"] == ["RB"]


class TestCallerProjectionRespectsOut:
    @pytest.mark.asyncio
    async def test_an_out_player_scores_nothing_and_loses_the_slot(self, monkeypatch):
        _optimizer(monkeypatch)
        out = await lo.compare_players_for_slot([
            _p("Hurt Star", "WR", 25.0, injury={"status": "Out"}),
            _p("Healthy", "WR", 9.0)], slot="WR")
        assert out["winner"]["player"] == "Healthy"
        hurt = next(c for c in out["comparison"] if c["player"] == "Hurt Star")
        assert hurt["projected_points"] == 0.0


class TestCompareUnresolved:
    @pytest.mark.asyncio
    async def test_unresolved_names_are_reported_not_crashed_on(self, monkeypatch):
        from nfl_mcp import tool_registry

        def _input(_db, p, season, week):
            name = p if isinstance(p, str) else p.get("name")
            known = {"A": ("WR", "KC"), "B": ("WR", "BUF")}.get(name)
            return {"name": name, "position": known[0] if known else None,
                    "team": known[1] if known else None, "player_id": "", "opponent": "OPP",
                    "projection": {"projected_points": 10.0}}
        monkeypatch.setattr(lineup_tools, "player_input", _input)
        monkeypatch.setattr(tool_registry, "get_db", lambda: None)
        _optimizer(monkeypatch)
        out = await tool_registry.compare_players_for_slot(
            players=["A", "B", "Nobody Known"], slot="WR", season=2026, week=3)
        assert out["success"] is True and out["unresolved"] == ["Nobody Known"]
        only = await tool_registry.compare_players_for_slot(
            players=["A", "Nobody Known"], slot="WR", season=2026, week=3)
        assert only["success"] is False and only["unresolved"] == ["Nobody Known"]


class TestLockedPlayerWithoutASlot:
    def test_no_eligible_slot_removes_nothing(self):
        mine = [{"name": "QB1", "position": "QB", "projected_points": 20.0},
                {"name": "RB1", "position": "RB", "projected_points": 12.0}]
        locked = [{"name": "Edge", "position": "DL", "slot": "DL",
                   "projected_points": 8.0, "sd": 0.0}]
        out = optimize_win_probability(mine, [], slots={"QB": 1, "RB": 1},
                                       locked_players=locked)
        names = {e["player"] for e in out["recommended_lineup"]}
        assert {"QB1", "RB1", "Edge"} <= names


class TestByeCostIgnoresInjuredWeeks:
    def test_a_player_out_injured_is_not_a_bye_cost(self):
        injured = {"player": "Hurt", "position": "WR", "team": "KC", "player_id": "h",
                   "weekly_points": {5: 0.0}, "bye_weeks": [5], "injury_weeks": [5],
                   "per_game": 15.0}
        other = {"player": "Other", "position": "WR", "team": "BUF", "player_id": "o",
                 "weekly_points": {5: 8.0}, "bye_weeks": [7], "per_game": 8.0}
        row = _week_row([injured, other], ["WR"], 5, {"h"})
        assert row["bye_cost"] == 0.0


class TestByeCheckPrefersSchedule:
    def test_conflicting_opponent_is_overridden_and_reported(self):
        out = bye_check("KC", "LV", {"KC": "DEN", "DEN": "KC"}, 3)
        assert out["status"] == PLAYING and out["opponent"] == "DEN"
        assert out["source"] == "schedule" and "LV" in out["reason"]

    def test_an_agreeing_alias_is_not_a_conflict(self):
        out = bye_check("HOU", "JAC", {"JAX": "HOU", "HOU": "JAX"}, 3)
        assert out["opponent"] == "JAX" and out["reason"] is None


class TestUnitMatchupNormalizesTeams:
    @pytest.mark.asyncio
    async def test_alias_opponent_is_found(self, monkeypatch):
        from nfl_mcp import streaming_tools
        rankings = {"JAX": {"rank": 30, "games": 8, "real_points_avg": 15.0}}
        monkeypatch.setattr(streaming_tools, "_resolve_offense",
                            AsyncMock(return_value=(rankings, 2026, False)))
        from nfl_mcp.scoring import resolve_scoring
        out = await streaming_tools.unit_matchup("DEF", "KC", "jac", 2026, resolve_scoring("ppr"))
        assert out is not None and out["offense_rank"] == 30


class TestVegasSeasonDefault:
    def test_january_uses_last_season(self, monkeypatch):
        from nfl_mcp import vegas_tools, week_context
        seen = []

        class _DB:
            def get_kickoff_week_index(self, season):
                seen.append(season)
                return {}
        monkeypatch.setattr(vegas_tools, "get_shared_db", lambda *a, **k: _DB())
        monkeypatch.setattr(week_context, "infer_from_calendar", lambda now=None: (2025, 18))
        vegas_tools._build_week_index(None)
        assert seen == [2025]


class TestReplacementLevels:
    def test_two_flex_does_not_make_a_te2_a_starter(self):
        slots = slot_counts(["QB", "RB", "WR", "TE", "FLEX", "FLEX", "BN"])
        tes = [{"position": "TE", "projected_points": 12.0},
               {"position": "TE", "projected_points": 4.0}]
        assert replacement_levels(tes, slots)["TE"] == 12.0
        assert surplus_players(tes, slots)[0]["projected_points"] == 4.0

    def test_no_k_slot_means_no_k_bar(self):
        slots = slot_counts(["QB", "RB", "WR", "BN"])
        ks = [{"position": "K", "projected_points": 9.0}]
        assert "K" not in replacement_levels(ks, slots)
        assert surplus_players(ks, slots) == []


class TestRosterMatchupsBye:
    @pytest.mark.asyncio
    async def test_bye_is_a_bye_row_not_a_neutral_16(self, monkeypatch):
        from nfl_mcp import matchup_tools
        analyzer = _analyzer()
        monkeypatch.setattr(matchup_tools, "get_defense_analyzer", lambda: analyzer)
        out = await matchup_tools.analyze_roster_matchups(
            [{"name": "X", "position": "WR", "opponent": "BYE"}])
        row = out["analysis"][0]
        assert row["matchup_tier"] == "bye" and row["on_bye"] is True
        assert row["rank"] is None


class TestWaiverReEntryDays:
    def test_days_between_reads_millisecond_timestamps(self):
        from nfl_mcp.waiver_tools import WaiverAnalyzer
        day = 86_400_000
        out = WaiverAnalyzer()._track_re_entries([
            {"transaction_id": "d", "created": 1_700_000_000_000, "drops": {"p": 1}},
            {"transaction_id": "a", "created": 1_700_000_000_000 + 2 * day, "adds": {"p": 2}},
        ])
        assert out["p"]["re_entries"][0]["days_between"] == pytest.approx(2.0)


class TestLeagueChanges:
    @pytest.mark.asyncio
    async def test_failed_transactions_hold_the_last_check(self, league, db, monkeypatch):  # noqa: F811
        monkeypatch.setattr(sleeper_tools, "get_transactions",
                            AsyncMock(side_effect=OSError("down")))
        out = await league_changes_tools.get_league_changes("L", roster_id=1)
        assert out["marked_seen"] is False and out["not_marked_because"]
        assert db.get_league_last_check("L", 1) is None

    @pytest.mark.asyncio
    async def test_failed_news_holds_the_last_check(self, league, db, monkeypatch):  # noqa: F811
        monkeypatch.setattr(nfl_tools, "get_nfl_news", AsyncMock(side_effect=OSError("down")))
        out = await league_changes_tools.get_league_changes("L", roster_id=1)
        assert out["marked_seen"] is False
        assert db.get_league_last_check("L", 1) is None

    @pytest.mark.asyncio
    async def test_a_null_matchup_has_no_opponent(self, league, monkeypatch):  # noqa: F811
        monkeypatch.setattr(sleeper_tools, "get_matchups", AsyncMock(return_value={"matchups": [
            {"roster_id": 1, "matchup_id": None, "starters": ["rb1"]},
            {"roster_id": 2, "matchup_id": None, "starters": ["oqb"]},
        ]}))
        out = await league_changes_tools.get_league_changes("L", roster_id=1, mark_seen=False)
        assert out["opponent_roster_id"] is None


class TestTradeGivesMustBeOnTheRoster:
    @pytest.mark.asyncio
    async def test_a_player_not_on_the_giving_roster_is_rejected(self):
        from nfl_mcp.trade_analyzer_tools import analyze_trade

        async def _rosters(_):
            return {"success": True, "rosters": [
                {"roster_id": 1, "players": ["a1"], "players_enriched": [
                    {"player_id": "a1", "full_name": "A", "position": "RB"}]},
                {"roster_id": 2, "players": ["b1"], "players_enriched": [
                    {"player_id": "b1", "full_name": "B", "position": "WR"}]},
            ]}
        with patch("nfl_mcp.trade_analyzer_tools.get_rosters", side_effect=_rosters):
            out = await analyze_trade("L", 1, 2, ["b1"], ["b1"])
        assert out["success"] is False
        assert out["not_on_roster"] == {"team1_gives": ["b1"]}


def _pt_patches(rosters, matchups, week=13, league_settings=None):
    settings = {"playoff_teams": 2, "playoff_week_start": 15, **(league_settings or {})}

    async def L(_):
        return {"success": True, "league": {"settings": settings}}

    async def R(_):
        return {"success": True, "rosters": rosters}

    async def U(_):
        return {"success": True, "users": []}

    async def S():
        return {"success": True, "nfl_state": {"week": week}}

    async def M(_, w):
        return matchups(w)
    stack = contextlib.ExitStack()
    for name, fn in (("get_league", L), ("get_rosters", R), ("get_league_users", U),
                     ("get_nfl_state", S), ("get_matchups", M)):
        stack.enter_context(patch.object(pt, name, fn))
    return stack


def _roster(rid, wins, losses, ppg=100):
    return {"roster_id": rid, "owner_id": f"u{rid}",
            "settings": {"wins": wins, "losses": losses, "ties": 0, "fpts": ppg * (wins + losses)}}


def _pairs(pairs):
    ms = []
    for mid, (a, b) in enumerate(pairs, 1):
        ms += [{"roster_id": a, "matchup_id": mid}, {"roster_id": b, "matchup_id": mid}]
    return {"success": True, "matchups": ms}


class TestPlayoffOdds:
    def test_median_game_adds_a_win_to_the_top_half(self):
        teams = [{"roster_id": i, "wins": 0, "points": 0, "mean": m}
                 for i, m in ((1, 200), (2, 150), (3, 50), (4, 10))]
        # 1v2 and 3v4: 2 loses its game but beats the median, 3 wins its game
        # but does not. Without the median game 3 (1-0) beats 2 (0-1) to the
        # second seed; with it both have a win and 2's points decide.
        out = pt._simulate(teams, [(1, 2), (3, 4)], 2, 200, 1.0, random.Random(1),
                           median_weeks=[5, 5])
        assert out[2]["playoff_pct"] == 100.0 and out[3]["playoff_pct"] == 0.0
        plain = pt._simulate(teams, [(1, 2), (3, 4)], 2, 200, 1.0, random.Random(1))
        assert plain[3]["playoff_pct"] == 100.0

    @pytest.mark.asyncio
    async def test_failed_weeks_are_reported(self):
        rosters = [_roster(i, 6, 6) for i in range(1, 5)]

        def matchups(w):
            if w == 14:
                return {"success": False, "error": "down", "matchups": []}
            return _pairs([(1, 2), (3, 4)])
        with _pt_patches(rosters, matchups):
            out = await pt.get_playoff_odds("L", num_sims=200, seed=1)
        assert out["failed_weeks"] == [14]
        assert "could not be loaded" in out["message"]

    @pytest.mark.asyncio
    async def test_a_finished_week_is_not_simulated_again(self):
        # NFL state still says week 13, but every team has played 13 games.
        rosters = [_roster(i, 7, 6) for i in range(1, 5)]
        with _pt_patches(rosters, lambda w: _pairs([(1, 2), (3, 4)])):
            out = await pt.get_playoff_odds("L", num_sims=200, seed=1)
        assert out["current_week"] == 14 and out["games_remaining"] == 2

    @pytest.mark.asyncio
    async def test_median_league_counts_two_games_a_week(self):
        # 12 weeks in a median league = 24 games; week 13 is still to play.
        rosters = [_roster(i, 12, 12) for i in range(1, 5)]
        with _pt_patches(rosters, lambda w: _pairs([(1, 2), (3, 4)]),
                         league_settings={"league_average_match": 1}):
            out = await pt.get_playoff_odds("L", num_sims=200, seed=1)
        assert out["current_week"] == 13 and out["median_game"] is True
