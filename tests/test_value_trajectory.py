"""Value trajectory (sell-high / buy-low) and package trades.

The live session that motivated this: Jaylen Warren's value peaked only
because Rico Dowdle was out, and the trade tools called him a hold; counter
offers like "Warren for Flowers" or a 3-for-2 had to be evaluated by hand.
"""
from unittest.mock import patch

import pytest

from nfl_mcp import ros, value_trajectory
from nfl_mcp.trade_finder_tools import (
    MAX_STARTERS_PER_PACKAGE,
    _free_slots,
    _timing,
    find_trade_targets,
    package_shapes,
    packages,
    roster_after,
)
from nfl_mcp.value_trajectory import annotate, assess, market_gap, our_rank
from tests.test_ros_projections import SETTINGS, FakeDB, _player, _stub_projection

WEEKS = list(range(5, 18))


def _entry(name="Jaylen Warren", position="RB", team="PIT", per_game=11.8, **extra):
    return {"player": name, "player_id": name, "position": position, "team": team,
            "per_game": per_game, "weekly_points": dict.fromkeys(WEEKS, per_game),
            "expected_absence_games": 0, **extra}


def _warren(**extra):
    return _entry(per_game_until_return=13.6,
                  returning_teammates=[{"name": "Rico Dowdle", "expected_return_week": 6,
                                        "games_until_return": 1, "status": "Questionable"}],
                  deflated_volume={"carries": {"trailing": 15.1, "with_teammate": 10.7}},
                  role_trend="role_up",
                  role_flags=["carries share 48%→82% (weeks 3-4)"], **extra)


class TestReturningTeammate:
    def test_warren_like_backup_is_a_sell_high(self):
        t = assess(_warren(), week=5)
        assert t["trajectory"] == "falling" and t["signal"] == "sell_high"
        assert t["change_week"] == 6
        assert t["expected_value_change"]["per_game"] == pytest.approx(-1.8)
        # 1.8 a game over the 12 weeks from his return on.
        assert t["expected_value_change"]["ros_points"] == pytest.approx(-21.6)
        why = t["reasons"][0]
        assert "Rico Dowdle (PIT, Questionable) due back week 6" in why
        assert "13.6 pts/game came without him" in why and "carries 15.1→10.7/game" in why

    def test_the_role_he_grew_into_while_the_starter_was_out_is_not_rising(self):
        t = assess(_warren(), week=5)
        assert not any(s["kind"] == "role_up" for s in t["signals"])
        assert any(r.startswith("role grew while Rico Dowdle was out") for r in t["reasons"])

    def test_a_return_past_the_horizon_does_not_move_him_yet(self):
        far = _warren()
        far["returning_teammates"][0]["games_until_return"] = (
            value_trajectory.TRAJECTORY_HORIZON_GAMES + 1)
        t = assess(far, week=5)
        assert t["signal"] == "hold"
        assert not any(s["kind"] == "returning_teammate" for s in t["signals"])

    def test_inherited_volume_ending_is_falling(self):
        e = _entry(name="Fill In", per_game=9.0, inherited_per_game=3.0, inherited_games=1,
                   inherited_from=["Hurt Starter"])
        t = assess(e, week=5)
        assert t["signal"] == "sell_high"
        assert t["expected_value_change"]["pct"] == pytest.approx(-25.0)
        assert "Hurt Starter" in t["reasons"][0] and "week 6" in t["reasons"][0]


class TestRoleAndInjury:
    def test_a_rising_role_is_flagged_but_alone_is_not_a_call(self):
        e = _entry(name="Jeremiyah Love", team="ARI", role_trend="role_up",
                   role_flags=["carries share 43%→66% (weeks 3-4)"])
        t = assess(e, week=5)
        assert t["signal"] == "hold"
        assert t["signals"] == [{"kind": "role_up", "change": 0.06}]
        assert t["reasons"] == ["role rising: carries share 43%→66% (weeks 3-4)"]

    def test_a_rising_role_the_market_has_not_seen_is_a_buy_low(self):
        e = _entry(name="Jeremiyah Love", team="ARI", role_trend="role_up",
                   role_flags=["carries share 43%→66% (weeks 3-4)"], market_position_rank=30)
        t = assess(e, week=5, rank=14)
        assert t["trajectory"] == "rising" and t["signal"] == "buy_low"

    def test_a_one_week_role_change_is_flagged_but_not_a_call(self):
        e = _entry(role_trend="role_down", role_flags=["snap share 94%→20% (week 4)"])
        t = assess(e, week=5)
        assert t["signal"] == "hold"
        assert t["signals"] == [{"kind": "role_down", "change": -0.03}]

    def test_back_from_a_multi_week_absence_is_rising(self):
        e = _entry(name="Jaxson Dart", position="QB", team="NYG", injury_status="IR",
                   expected_absence_games=3, injury_weeks=[5, 6, 7])
        t = assess(e, week=5)
        assert t["signal"] == "buy_low" and t["change_week"] == 8
        assert "back from IR in ~3 game(s) (week 8)" in t["reasons"][0]

    def test_season_ending_is_never_timed(self):
        t = assess(_warren(expected_absence_games=ros.SEASON_ENDING_WEEKS), week=5)
        assert t["signal"] == "hold" and t["signals"] == []


class TestMarketGap:
    def test_rank_against_the_pool(self):
        pool = [_entry(name=f"RB{i}", per_game=20.0 - i) for i in range(20)]
        ranked = value_trajectory._rank_pool(pool)
        assert our_rank(_entry(per_game=16.5), ranked) == 5   # 20, 19, 18, 17 ahead
        assert our_rank(pool[0], ranked) == 1
        # Too few at a position to rank against.
        assert value_trajectory._rank_pool(pool[:5]) == {}

    def test_a_gap_alone_is_reported_but_never_a_call(self):
        cheap = _entry(market_position_rank=28)
        gap = market_gap(cheap, 13)
        assert gap["read"] == "market_lower" and gap["gap"] == 15
        t = assess(cheap, week=5, rank=13)
        assert t["signal"] == "hold" and t["signals"][0]["kind"] == "market_gap"
        rich = _entry(market_position_rank=13)
        assert assess(rich, week=5, rank=32)["signal"] == "hold"
        # With a teammate back it corroborates the hard signal.
        assert assess(_warren(market_position_rank=8), week=5, rank=19)[
            "expected_value_change"]["pct"] < assess(_warren(), week=5)[
            "expected_value_change"]["pct"]
        assert market_gap(_entry(market_position_rank=17), 19)["read"] == "in_line"
        assert assess(_entry(market_position_rank=17), week=5, rank=19)["signal"] == "hold"

    def test_a_rising_role_is_not_called_overvalued_by_a_lagging_rate(self):
        e = _entry(market_position_rank=13, role_trend="role_up",
                   role_flags=["carries share 43%→66% (weeks 3-4)"])
        t = assess(e, week=5, rank=20)
        assert t["signal"] == "hold"            # the role alone, not cancelled
        assert t["expected_value_change"]["pct"] == pytest.approx(6.0)
        assert not any(s["kind"] == "market_gap" for s in t["signals"])

    def test_agreeing_soft_signals_count_once_plus_a_bonus(self):
        e = _entry(market_position_rank=13, role_trend="role_down",
                   role_flags=["target share 36%→16% (weeks 3-4)"])
        t = assess(e, week=5, rank=33)
        role, gap = (next(s["change"] for s in t["signals"] if s["kind"] == k)
                     for k in ("role_down", "market_gap"))
        expected = min(role, gap) - value_trajectory.SOFT_AGREEMENT_BONUS
        assert t["expected_value_change"]["pct"] == pytest.approx(100 * expected, abs=0.1)

    def test_annotate_attaches_and_returns_by_id(self):
        pool = [_entry(name=f"RB{i}", per_game=20.0 - i, market_position_rank=i + 1)
                for i in range(20)]
        out = annotate([pool[3]], week=5, pool=pool)
        assert pool[3]["value_trajectory"]["market_gap"]["our_rank"] == 4
        assert out["RB3"]["signal"] == "hold"


class TestRosCarriesTheInputs:
    @pytest.mark.asyncio
    async def test_returning_teammate_reaches_the_trajectory(self, monkeypatch):
        bd = {"base_ppg": 14.0, "base_source": "opportunity", "usage_games": 6,
              "position_rank": 17, "usage_mult": 1.0, "deflated_base_ppg": 8.0,
              "deflated_games": 2,
              "deflated_volume": {"carries": {"trailing": 15.0, "with_teammate": 9.0}},
              "returning_teammates": [{"name": "Lead Back", "games_until_return": 1,
                                       "expected_return_week": 4, "status": "Questionable"}]}
        _stub_projection(monkeypatch, {"Backup Back": 14.0}, bd)
        out = await ros.ros_projections([_player("Backup Back", team="PIT", position="RB")],
                                        season=2026, week=3, settings=SETTINGS, db=FakeDB())
        entry = out["players"][0]
        assert entry["market_position_rank"] == 17
        assert entry["deflated_volume"]["carries"]["with_teammate"] == 9.0
        assert entry["returning_teammates"][0]["games_until_return"] == 1
        t = assess(entry, week=3)
        assert t["signal"] == "sell_high"
        assert "Lead Back (PIT, Questionable) due back week 4" in t["reasons"][0]


class TestTiming:
    def test_notes_and_score(self):
        sell = {"name": "Warren", "value_trajectory": assess(_warren(), week=5)}
        buy = {"name": "Love", "value_trajectory": assess(_entry(
            role_trend="role_up", role_flags=["carries share 43%→66% (weeks 3-4)"],
            market_position_rank=30), week=5, rank=14)}
        notes = value_trajectory.side_notes([sell], [buy])
        assert notes[0].startswith("You are selling high on Warren (Rico Dowdle")
        assert notes[1].startswith("You are buying low on Love")
        assert value_trajectory.timing_score([sell], [buy]) == 2
        # The other side of the same trade is badly timed.
        assert value_trajectory.timing_score([buy], [sell]) == -2
        timing = _timing([sell], [buy], 13)
        assert timing["bonus"] == pytest.approx(2 * 0.25 * 13, abs=0.1)


class TestPackagePieces:
    def test_shapes(self):
        assert package_shapes(1) == []
        assert package_shapes(2) == [(1, 2), (2, 1), (2, 2)]
        assert set(package_shapes(3)) == {(1, 2), (2, 1), (2, 2), (2, 3), (3, 2)}

    def test_at_most_one_starter_per_package(self):
        surplus = [{"name": f"S{i}"} for i in range(3)]
        starters = [{"name": f"T{i}"} for i in range(3)]
        pkgs = packages(surplus, starters, 2)
        starter_ids = {id(p) for p in starters}
        assert all(sum(id(p) in starter_ids for p in pkg) <= MAX_STARTERS_PER_PACKAGE
                   for pkg in pkgs)
        # C(3,2) surplus pairs + 3x3 surplus-starter pairs.
        assert len(pkgs) == 3 + 9

    def test_a_full_roster_receiving_more_drops_its_least_valuable_active_player(self):
        roster = [{"name": "A", "total_points": 100}, {"name": "B", "total_points": 5},
                  {"name": "Hurt", "total_points": 1, "on_reserve": True},
                  {"name": "C", "total_points": 50}]
        give = (roster[0],)
        get = ({"name": "X", "total_points": 60}, {"name": "Y", "total_points": 2})
        after, drops = roster_after(roster, give, get, free_slots=0)
        assert [p["name"] for p in drops] == ["B"]          # not IR, not received
        assert sorted(p["name"] for p in after) == ["C", "Hurt", "X", "Y"]
        _, none = roster_after(roster, give, get, free_slots=1)
        assert none == []

    def test_free_slots_ignore_ir_and_taxi(self):
        positions = ["QB", "RB", "WR", "BN", "BN", "IR"]
        roster = {"players": ["a", "b", "c", "d"], "reserve": ["d"]}
        assert _free_slots(roster, positions) == 2


# ---------------------------------------------------------------------------
# End to end: a 2-for-1 the one-for-one search cannot express
# ---------------------------------------------------------------------------

ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN", "BN", "BN"]
POINTS = {
    "My QB": 20.0, "My RB1": 15.0, "My Weak RB": 3.0, "My Spare RB": 2.0,
    "My WR1": 16.0, "My WR2": 14.0, "My WR3": 13.0, "My Spare WR": 12.5,
    "My TE": 9.0, "My Spare TE": 8.0,
    "Their QB": 19.0, "Their QB2": 10.0, "Their RB1": 17.0, "Their RB2": 16.0,
    "Their RB3": 15.0, "Their Spare RB": 14.0,
    "Their WR1": 15.0, "Their Weak WR": 2.0, "Their Worst WR": 0.5, "Their Weak TE": 1.0,
}
MINE = ["My QB", "My RB1", "My Weak RB", "My Spare RB", "My WR1", "My WR2", "My WR3",
        "My Spare WR", "My TE", "My Spare TE"]
THEIRS = ["Their QB", "Their QB2", "Their RB1", "Their RB2", "Their RB3", "Their Spare RB",
          "Their WR1", "Their Weak WR", "Their Worst WR", "Their Weak TE"]


def _pos(name):
    for tag, pos in (("QB", "QB"), ("RB", "RB"), ("WR", "WR"), ("TE", "TE")):
        if tag in name:
            return pos
    raise ValueError(name)


@pytest.fixture
def league(monkeypatch, tmp_path):
    from nfl_mcp import projections, sleeper_tools, trade_finder_tools
    from nfl_mcp.database import NFLDatabase

    db = NFLDatabase(str(tmp_path / "t.db"))
    db.upsert_athletes({
        n: {"player_id": n, "full_name": n, "position": _pos(n), "team": "BUF",
            "status": "Active"} for n in MINE + THEIRS})
    db.upsert_schedule_games([
        {"season": 2026, "week": 3, "team": "BUF", "opponent": "KC", "is_home": 1},
        {"season": 2026, "week": 3, "team": "KC", "opponent": "BUF", "is_home": 0},
    ])
    monkeypatch.setattr(trade_finder_tools, "get_shared_db", lambda *a, **k: db)

    async def _state():
        return {"nfl_state": {"week": 3, "season": 2026}}

    async def _league(_):
        return {"league": {"name": "Test", "total_rosters": 2, "scoring_settings": {"rec": 0.5},
                           "roster_positions": ROSTER_POSITIONS, "settings": SETTINGS}}

    async def _rosters(_):
        return {"rosters": [{"roster_id": 7, "owner_id": "me", "players": list(MINE)},
                            {"roster_id": 2, "owner_id": "them", "players": list(THEIRS)}]}

    async def _users(_):
        return {"users": [{"user_id": "them", "display_name": "Rival"}]}

    async def _project(players, **kwargs):
        return {"projections": [
            {"player": p["name"], "position": p["position"], "team": p["team"],
             "opponent": p.get("opponent"), "projected_points": POINTS[p["name"]],
             "breakdown": {}} for p in players]}

    monkeypatch.setattr(sleeper_tools, "get_nfl_state", _state)
    monkeypatch.setattr(sleeper_tools, "get_league", _league)
    monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
    monkeypatch.setattr(sleeper_tools, "get_league_users", _users)
    monkeypatch.setattr(projections, "project_players", _project)
    return db


def _scored(names, weeks):
    return [{"player_id": n, "name": n, "player": n, "position": _pos(n),
             "total_points": POINTS[n] * len(weeks), "projected_points": POINTS[n] * len(weeks),
             "weekly_points": dict.fromkeys(weeks, POINTS[n]), "on_reserve": False}
            for n in names]


def _search(max_size=2):
    from nfl_mcp.roster_needs import lineup_slots
    from nfl_mcp.trade_finder_tools import _search_packages

    weeks = list(range(3, 18))
    slots = lineup_slots(ROSTER_POSITIONS)
    scored = {7: _scored(MINE, weeks), 2: _scored(THEIRS, weeks)}
    rosters = [{"roster_id": 7, "players": list(MINE)}, {"roster_id": 2, "players": list(THEIRS)}]
    return _search_packages(
        rosters=rosters, roster_id=7, scored_by_roster=scored, whole_slots=slots, weeks=weeks,
        wanted={"QB", "RB", "WR", "TE"}, bar=0.5 * len(weeks), names={2: "Rival"},
        roster_positions=ROSTER_POSITIONS, max_package_size=max_size,
        base=lambda rid: ros.weekly_lineup_total(scored[rid], slots, weeks))


class TestPackageSearch:
    def test_a_two_for_one_costs_the_full_roster_its_worst_player(self):
        result = _search()
        deal = next(p for p in result["proposals"] if p["shape"] == "2-for-1"
                    and {x["name"] for x in p["you_give"]} == {"My Spare WR", "My Spare TE"}
                    and [x["name"] for x in p["you_get"]] == ["Their Spare RB"])
        assert [d["name"] for d in deal["their_drops"]] == ["Their Worst WR"]
        assert deal["your_drops"] == []
        # Both pieces matter to them: WR2 2 -> 12.5 and TE 1 -> 8 a week.
        assert deal["their_gain"] == pytest.approx((10.5 + 7.0) * 15, abs=0.5)
        assert deal["your_gain"] == pytest.approx((14.0 - 3.0) * 15, abs=0.5)

    def test_every_package_is_roster_legal_and_both_sides_gain(self):
        result = _search(3)
        assert result["proposals"] and not result["screen_capped"]
        capacity = len(ROSTER_POSITIONS)
        for p in result["proposals"]:
            n_give, n_get = len(p["you_give"]), len(p["you_get"])
            assert len(MINE) - n_give + n_get - len(p["your_drops"]) <= capacity
            assert len(THEIRS) - n_get + n_give - len(p["their_drops"]) <= capacity
            assert p["your_gain"] >= 7.5 and p["their_gain"] >= 7.5

    def test_padding_never_rides_along(self):
        result = _search()
        assert result["skipped_padding"] > 0
        for p in result["proposals"]:
            # My 2-point back helps nobody: any deal with him is a smaller
            # deal plus padding.
            assert "My Spare RB" not in {x["name"] for x in p["you_give"]}

    @pytest.mark.asyncio
    async def test_the_tool_lists_the_best_package_per_partner(self, league):
        out = await find_trade_targets("L", roster_id=7)
        assert out["success"] is True and out["max_package_size"] == 2
        best = out["package_proposals"][0]
        assert best["shape"] in ("1-for-2", "2-for-1", "2-for-2")
        assert best["your_gain"] >= out["minimum_gain"]
        assert best["their_gain"] >= out["minimum_gain"]
        search = out["package_search"]
        assert search["shapes"] == ["1-for-2", "2-for-1", "2-for-2"]
        assert search["screened"] > 0 and not search["screen_capped"]
        assert best["timing"]["score"] == 0 and best["rank_score"] == best["your_gain"]
        assert "search_seconds" in out["timing"]

    @pytest.mark.asyncio
    async def test_size_one_is_the_old_search(self, league):
        out = await find_trade_targets("L", roster_id=7, max_package_size=1)
        assert out["package_proposals"] == [] and out["package_search"] is None
        assert out["proposals"]

    @pytest.mark.asyncio
    async def test_nothing_when_they_cannot_gain(self, league, monkeypatch):
        for name in THEIRS:
            monkeypatch.setitem(POINTS, name, 30.0)
        out = await find_trade_targets("L", roster_id=7)
        assert out["package_proposals"] == [] and out["proposals"] == []


# ---------------------------------------------------------------------------
# analyze_trade: per-player trajectory and side notes
# ---------------------------------------------------------------------------

def _ros_entry(pid, position, per_week, **extra):
    weekly = dict.fromkeys(WEEKS, per_week)
    return {"player_id": pid, "player": pid, "position": position, "team": "PIT",
            "per_game": per_week, "total_points": per_week * len(WEEKS),
            "ros_points": per_week * len(WEEKS), "playoff_points": 0.0,
            "weekly_points": weekly, "bye_weeks": [], "injury_weeks": [],
            "expected_absence_games": 0, **extra}


class TestAnalyzeTradeTiming:
    @pytest.mark.asyncio
    async def test_selling_high_and_the_market_note_agree_with_the_verdict(self, monkeypatch):
        from nfl_mcp.trade_analyzer_tools import analyze_trade

        by_id = {
            "warren": _ros_entry("warren", "RB", 11.8, per_game_until_return=13.6,
                                 returning_teammates=[{"name": "Rico Dowdle",
                                                       "expected_return_week": 6,
                                                       "games_until_return": 1}]),
            "flowers": _ros_entry("flowers", "WR", 15.0),
            "t1_rb2": _ros_entry("t1_rb2", "RB", 2.0), "t1_wr": _ros_entry("t1_wr", "WR", 3.0),
            "t2_rb": _ros_entry("t2_rb", "RB", 3.0), "t2_wr": _ros_entry("t2_wr", "WR", 14.0),
            "t2_wr2": _ros_entry("t2_wr2", "WR", 13.5),
        }
        seen = {}

        async def _ros_for_ids(ids, **kw):
            seen["ids"] = list(ids)
            return ({i: by_id[i] for i in ids if i in by_id},
                    {"windows": {"regular": WEEKS, "playoff": []}, "schedule_unknown_weeks": []})

        async def _state(db=None):
            return {"season": 2026, "week": 5, "source": "nfl_state"}

        class _Values:
            async def get_values(self, *a, **k):
                return {}

            def lookup(self, index, player_id=None, name=None, position=None):
                # The market loves Flowers: lopsided on value.
                return {"value": 9000 if player_id == "flowers" else 3000,
                        "overall_rank": 5, "position_rank": 2}

        rosters = [
            {"roster_id": 1, "players": ["warren", "t1_rb2", "t1_wr"], "starters_enriched": [],
             "players_enriched": [{"player_id": "warren", "full_name": "Jaylen Warren",
                                   "position": "RB"}]},
            {"roster_id": 2, "players": ["flowers", "t2_rb", "t2_wr", "t2_wr2"],
             "starters_enriched": [],
             "players_enriched": [{"player_id": "flowers", "full_name": "Zay Flowers",
                                   "position": "WR"}]},
            {"roster_id": 3, "players": ["elsewhere"], "players_enriched": []},
        ]

        async def _rosters(league_id):
            return {"success": True, "rosters": rosters}

        async def _trending(*a):
            return {"success": True, "trending_players": []}

        async def _league(league_id):
            return {"success": True, "league": {
                "scoring_settings": {"rec": 1}, "total_rosters": 3, "settings": {"type": 0},
                "roster_positions": ["RB", "WR", "FLEX", "BN"]}}

        monkeypatch.setattr(ros, "ros_for_ids", _ros_for_ids)
        monkeypatch.setattr("nfl_mcp.week_context.current_season_week", _state)
        with patch("nfl_mcp.trade_analyzer_tools.get_rosters", side_effect=_rosters), \
             patch("nfl_mcp.trade_analyzer_tools.get_league", side_effect=_league), \
             patch("nfl_mcp.trade_analyzer_tools.get_trending_players", side_effect=_trending), \
             patch("nfl_mcp.trade_analyzer_tools.get_values_service", return_value=_Values()):
            result = await analyze_trade("L", 1, 2, ["warren"], ["flowers"], nfl_db=object())

        assert result["success"] is True
        # The whole league is the ranking pool.
        assert "elsewhere" in seen["ids"]
        warren = result["team1_analysis"]["gives"][0]
        assert warren["value_trajectory"]["signal"] == "sell_high"
        assert result["team1_analysis"]["timing_notes"][0].startswith(
            "You are selling high on Jaylen Warren (Rico Dowdle")
        assert result["team2_analysis"]["timing_notes"][0].startswith(
            "You are buying high on Jaylen Warren")
        # Both lineups improve while market value is lopsided: no "unfair".
        assert result["recommendation"] == "both_lineups_improve"
        assert result["market_fairness"] == "unfair"
        assert "unfair" not in result["verdict"]
        assert "expect a counter-offer" in result["verdict"]
        assert not any("appears significantly lopsided" in w for w in result["warnings"])
        assert any("Market values are lopsided" in w for w in result["warnings"])


class TestRosToolSurfacesTrajectory:
    @pytest.mark.asyncio
    async def test_value_trajectory_in_get_ros_projections(self, monkeypatch):
        from nfl_mcp import sleeper_tools

        seen = {}
        by_id = {"warren": {**_warren(), "player_id": "warren", "total_points": 150.0},
                 "other": _entry(name="other", per_game=9.0, total_points=110.0)}
        by_id["other"]["player_id"] = "other"

        async def _league(_):
            return {"league": {"name": "T", "settings": SETTINGS}}

        async def _rosters(_):
            return {"rosters": [{"roster_id": 7, "players": ["warren"]},
                                {"roster_id": 1, "players": ["other"]}]}

        async def _ros_for_ids(ids, **kw):
            seen["ids"] = list(ids)
            return ({i: dict(by_id[i]) for i in ids if i in by_id},
                    {"windows": {"regular": WEEKS, "playoff": []},
                     "schedule_unknown_weeks": [], "matchups_active": False,
                     "scoring_used": {}})

        monkeypatch.setattr(sleeper_tools, "get_league", _league)
        monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
        monkeypatch.setattr(ros, "ros_for_ids", _ros_for_ids)
        out = await ros.get_ros_projections("L", roster_id=7, season=2026, week=5, db=object())
        assert set(seen["ids"]) == {"warren", "other"}   # the league pool
        [warren] = out["players"]                         # only what was asked for
        assert warren["value_trajectory"]["signal"] == "sell_high"
        assert "weekly_points" not in warren

        out = await ros.get_ros_projections("L", roster_id=7, season=2026, week=5, db=object(),
                                            include_trajectory=False)
        assert "value_trajectory" not in out["players"][0]
