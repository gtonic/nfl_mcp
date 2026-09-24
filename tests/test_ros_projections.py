"""Rest-of-season projections and the tools that now use them.

Offline: the schedule is a fake database, projections are stubbed, and the
defense rankings / schedule fetch are kept off the network by conftest.
"""
import contextlib
from datetime import date
from unittest.mock import patch

import pytest

from nfl_mcp import playoff_tools as pt
from nfl_mcp import projections, ros
from nfl_mcp.trade_analyzer_tools import TradeAnalyzer

TEAMS = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
         "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO",
         "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS"]


class FakeDB:
    """Just enough of NFLDatabase for the schedule lookups."""

    def __init__(self, byes: dict[int, set[str]] | None = None, weeks=range(1, 19)):
        self.byes = byes or {}
        self.weeks = set(weeks)

    def get_week_opponents(self, season, week):
        if week not in self.weeks:
            return {}
        playing = [t for t in TEAMS if t not in self.byes.get(week, set())]
        out = {}
        for a, b in zip(playing[::2], playing[1::2], strict=False):
            out[a], out[b] = b, a
        return out

    def get_week_kickoffs(self, season, week):
        return {}


def _stub_projection(monkeypatch, points: dict[str, float], breakdown: dict | None = None):
    calls = []

    async def _project(players, **kwargs):
        calls.append((kwargs.get("week"), [p["name"] for p in players]))
        out = []
        for p in players:
            bye = p.get("opponent") == "BYE"
            out.append({
                "player": p["name"], "team": p["team"], "position": p["position"],
                "opponent": p["opponent"], "on_bye": bye,
                "projected_points": 0.0 if bye else points.get(p["name"], 5.0),
                "breakdown": ({"base_ppg": 0.0, "base_source": "bye"} if bye
                              else dict(breakdown or {})),
            })
        return {"projections": out}

    monkeypatch.setattr(projections, "project_players", _project)
    return calls


SETTINGS = {"playoff_week_start": 15, "playoff_teams": 6, "playoff_round_type": 0}


def _player(name, team="BUF", position="WR", status=None, description=None):
    return {"name": name, "team": team, "position": position, "player_id": name,
            "injury": {"status": status, "description": description}}


class TestCalendar:
    def test_playoff_window_from_bracket_size(self):
        assert ros.playoff_window(SETTINGS) == (15, 17)
        assert ros.playoff_window({**SETTINGS, "playoff_round_type": 1}) == (15, 18)
        assert ros.playoff_window({"playoff_week_start": 15, "playoff_teams": 4}) == (15, 16)
        assert ros.playoff_window({}) == (15, 17)

    def test_windows_split_regular_season_and_playoffs(self):
        w = ros.season_windows(SETTINGS, 3)
        assert w["regular"] == list(range(3, 15))
        assert w["playoff"] == [15, 16, 17]
        late = ros.season_windows(SETTINGS, 16)
        assert late["regular"] == [] and late["playoff"] == [16, 17]

    @pytest.mark.parametrize("settings,week,passed,urgent", [
        ({"trade_deadline": 11}, 3, False, False),
        ({"trade_deadline": 11}, 10, False, True),
        ({"trade_deadline": 11}, 11, False, True),
        ({"trade_deadline": 11}, 12, True, False),
        ({"trade_deadline": 0}, 12, False, False),
        ({}, 12, False, False),
    ])
    def test_trade_deadline(self, settings, week, passed, urgent):
        status = ros.trade_deadline_status(settings, week)
        assert status["passed"] is passed
        assert status["urgent"] is urgent


class TestExpectedAbsence:
    def test_healthy_and_questionable_cost_no_future_weeks(self):
        assert ros.expected_absence(None)[0] == 0
        assert ros.expected_absence("Questionable")[0] == 0
        assert ros.expected_absence("Doubtful")[0] == 0

    def test_out_is_one_week_ir_is_four(self):
        assert ros.expected_absence("Out")[0] == 1
        assert ros.expected_absence("IR")[0] == ros.IR_MIN_WEEKS
        assert ros.expected_absence("PUP")[0] == ros.IR_MIN_WEEKS

    def test_report_text_extends_the_window(self):
        assert ros.expected_absence("Out", "expected to miss 2-3 weeks")[0] == 3
        assert ros.expected_absence("IR", "suffered a season-ending ACL tear")[0] == \
            ros.SEASON_ENDING_WEEKS
        # A shorter stated window never beats the IR minimum.
        assert ros.expected_absence("IR", "out 2 weeks")[0] == ros.IR_MIN_WEEKS
        assert ros.expected_absence("Sus", "serving a 6-game suspension")[0] == 6

    def test_return_date_wins(self):
        weeks, _ = ros.expected_absence("IR", return_date="2026-10-14",
                                        today=date(2026, 9, 23))
        assert weeks == 3


class TestRosProjections:
    async def _run(self, monkeypatch, players, db=None, week=3, points=None, **kw):
        _stub_projection(monkeypatch, points or {}, kw.pop("breakdown", None))
        return await ros.ros_projections(
            players, season=2026, week=week, settings=SETTINGS, db=db or FakeDB(),
            include_weekly=True, **kw)

    @pytest.mark.asyncio
    async def test_bye_week_is_zero(self, monkeypatch):
        out = await self._run(monkeypatch, [_player("Receiver")], db=FakeDB({7: {"BUF"}}),
                              points={"Receiver": 10.0})
        p = out["players"][0]
        assert p["bye_weeks"] == [7]
        assert p["weekly_points"][7] == 0.0
        # 12 regular weeks (3-14), one of them a bye.
        assert p["ros_points"] == pytest.approx(10.0 * 11)
        assert p["playoff_points"] == pytest.approx(30.0)
        assert p["weeks_counted"] == 14

    @pytest.mark.asyncio
    async def test_bye_this_week_is_priced_from_next_week(self, monkeypatch):
        """A player on bye right now is still worth his other weeks."""
        out = await self._run(monkeypatch, [_player("Receiver")], db=FakeDB({3: {"BUF"}}),
                              points={"Receiver": 10.0})
        p = out["players"][0]
        assert p["this_week_points"] == 0.0
        assert p["weekly_points"][4] == pytest.approx(10.0)
        assert p["ros_points"] == pytest.approx(10.0 * 11)

    @pytest.mark.asyncio
    async def test_ir_window_is_zero_then_full(self, monkeypatch):
        out = await self._run(monkeypatch, [_player("Hurt", status="IR")],
                              points={"Hurt": 0.0},
                              breakdown={"base_ppg": 12.0, "base_source": "rank_bucket"})
        p = out["players"][0]
        assert p["injury_weeks"] == [3, 4, 5, 6]
        assert all(p["weekly_points"][w] == 0.0 for w in (3, 4, 5, 6))
        assert p["weekly_points"][7] == pytest.approx(12.0)

    @pytest.mark.asyncio
    async def test_season_ending_is_zero_throughout(self, monkeypatch):
        out = await self._run(
            monkeypatch, [_player("Gone", status="IR", description="season-ending surgery")],
            breakdown={"base_ppg": 15.0, "base_source": "rank_bucket"})
        p = out["players"][0]
        assert p["total_points"] == 0.0 and p["weeks_counted"] == 0

    @pytest.mark.asyncio
    async def test_small_samples_regress_toward_the_prior(self, monkeypatch):
        # Two games at a 30-point pace for a WR the market ranks 40th.
        out = await self._run(
            monkeypatch, [_player("Hot Start")], points={"Hot Start": 30.0},
            breakdown={"base_ppg": 30.0, "base_source": "opportunity", "usage_games": 2,
                       "position_rank": 40, "usage_mult": 1.0})
        p = out["players"][0]
        prior = projections.base_ppg("WR", 40, 1.0)
        assert p["prior_weight"] == pytest.approx(0.5)
        assert p["per_game"] == pytest.approx(0.5 * 30.0 + 0.5 * prior, abs=0.01)
        # This week keeps the weekly projection; later weeks use the rate.
        assert p["weekly_points"][3] == 30.0
        assert p["weekly_points"][4] == pytest.approx(p["per_game"])

    @pytest.mark.asyncio
    async def test_a_player_at_his_rank_level_is_not_pulled_down(self, monkeypatch):
        # A WR12 whose two-game opportunity base is what a WR12 actually scores
        # per game: later weeks keep that rate instead of sliding toward the
        # rank bucket — the systematic under-projection of starters.
        level = projections.base_ppg("WR", 12, 1.0)
        out = await self._run(
            monkeypatch, [_player("Steady")], points={"Steady": level},
            breakdown={"base_ppg": level, "base_source": "opportunity", "usage_games": 2,
                       "position_rank": 12, "usage_mult": 1.0})
        p = out["players"][0]
        assert p["per_game"] == pytest.approx(level, abs=0.01)
        assert p["weekly_points"][4] == pytest.approx(p["this_week_points"], abs=0.1)

    def test_regression_weight_falls_with_games(self):
        assert ros.regressed_rate(20.0, 10.0, 2) == pytest.approx(15.0)
        assert ros.regressed_rate(20.0, 10.0, 6) == pytest.approx(17.5)
        assert ros.regressed_rate(20.0, 10.0, 0) == pytest.approx(10.0)
        assert ros.regressed_rate(20.0, 10.0, 2, prior_games=0) == pytest.approx(20.0)

    def test_prior_is_the_weekly_bucket_unscaled(self):
        # ROS and the weekly engine share one scale: no ROS-only rescaling.
        assert not hasattr(ros, "PRIOR_SCALE")
        proj = {"breakdown": {"base_ppg": 20.0, "base_source": "opportunity",
                              "usage_games": 2, "position_rank": 30}}
        from nfl_mcp.scoring import ScoringModel
        rate, _, _ = ros._per_game(proj, "TE", ScoringModel.preset(1.0))
        assert rate == pytest.approx(0.5 * 20.0 + 0.5 * projections.base_ppg("TE", 30, 1.0))

    @pytest.mark.asyncio
    async def test_matchup_moves_later_weeks(self, monkeypatch):
        class Analyzer:
            def get_matchup_difficulty(self, position, opponent, rankings):
                return {"matchup_tier": "smash" if opponent == "ATL" else "neutral"}

        async def _rankings():
            return {"RB": [{"team": "ATL"}]}

        monkeypatch.setattr(ros, "_defense_rankings", _rankings)
        import nfl_mcp.matchup_tools as mt
        monkeypatch.setattr(mt, "get_defense_analyzer", lambda: Analyzer())
        out = await self._run(monkeypatch, [_player("Runner", team="ARI", position="RB")],
                              points={"Runner": 10.0},
                              breakdown={"base_ppg": 10.0, "base_source": "rank_bucket"})
        weekly = {w["week"]: w for w in out["players"][0]["weekly"]}
        # FakeDB pairs ARI with ATL every week.
        assert weekly[4]["opponent"] == "ATL"
        assert weekly[4]["points"] == pytest.approx(10.0 * projections.matchup_multiplier("RB", "smash"))

    @pytest.mark.asyncio
    async def test_unknown_schedule_weeks_are_reported(self, monkeypatch):
        out = await self._run(monkeypatch, [_player("Receiver")],
                              db=FakeDB(weeks=range(1, 10)), points={"Receiver": 10.0})
        assert out["schedule_unknown_weeks"] == list(range(10, 18))
        assert out["players"][0]["total_points"] == pytest.approx(10.0 * 15)

    @pytest.mark.asyncio
    async def test_missing_schedule_week_is_fetched_and_cached(self, monkeypatch):
        fetched = []

        async def _fetch(season, week):
            fetched.append(week)
            return [{"season": season, "week": week, "team": a, "opponent": b, "is_home": 1}
                    for a, b in zip(TEAMS[::2], TEAMS[1::2], strict=True)]

        monkeypatch.setattr(ros, "_fetch_week_schedule", _fetch)
        out = await self._run(monkeypatch, [_player("Receiver")],
                              db=FakeDB(weeks=range(1, 10)), points={"Receiver": 10.0})
        assert fetched == list(range(10, 18))
        assert out["schedule_unknown_weeks"] == []


class TestTradeFairnessPadding:
    def _p(self, value, position="WR", source="fantasycalc", **kw):
        return {"calculated_value": value, "position": position, "value_source": source, **kw}

    def test_padding_with_unpriced_k_and_def_does_not_raise_fairness(self):
        analyzer = TradeAnalyzer()
        needs = {}
        _, base, _ = analyzer._evaluate_trade_fairness(
            [self._p(6000)], [self._p(4000)], needs, needs)
        _, padded, _ = analyzer._evaluate_trade_fairness(
            [self._p(6000)],
            [self._p(4000), self._p(150, "K", "estimated"), self._p(150, "DEF", "estimated")],
            needs, needs)
        assert padded <= base

    def test_a_stud_for_two_lesser_players_favours_the_stud_side(self):
        analyzer = TradeAnalyzer()
        rec, _, details = analyzer._evaluate_trade_fairness(
            [self._p(6000)], [self._p(3000), self._p(3000)], {}, {})
        # Team 2 receives the stud; team 1 receives two bench pieces.
        assert details["team2_receives_adjusted_value"] > details["team1_receives_adjusted_value"]
        assert details["team1_depth_discount"] > 0
        assert rec != "fair"

    def test_depth_that_starts_counts_in_full(self):
        analyzer = TradeAnalyzer()
        _, score, details = analyzer._evaluate_trade_fairness(
            [self._p(6000)],
            [self._p(3000, starts_for_receiver=True), self._p(3000, starts_for_receiver=True)],
            {}, {})
        assert details["team1_depth_discount"] == 0
        assert score >= 90


def _mock_matchups_with_rematch(week):
    # Week 13: 4 v 5, week 14: 4 v 5 again (a rematch), plus the others.
    pairs = {13: [(1, 6), (2, 3), (4, 5)], 14: [(4, 5), (1, 2), (3, 6)]}.get(week, [])
    ms = []
    for mid, (a, b) in enumerate(pairs, 1):
        ms += [{"roster_id": a, "matchup_id": mid}, {"roster_id": b, "matchup_id": mid}]
    return {"success": True, "matchups": ms, "week": week}


class TestPlayoffSwingRematch:
    @pytest.mark.asyncio
    async def test_a_later_rematch_stays_in_the_simulation(self):
        from tests.test_playoff_tools import _mock_league, _mock_rosters, _mock_users

        async def L(_):
            return _mock_league()

        async def R(_):
            return _mock_rosters()

        async def U(_):
            return _mock_users()

        async def S():
            return {"success": True, "nfl_state": {"week": 13}}

        async def M(_, w):
            return _mock_matchups_with_rematch(w)

        seen: list[list[tuple[int, int]]] = []
        real = pt._simulate

        forced_seen: list = []

        def _spy(teams, schedule, playoff_teams, num_sims, sd, rng, **kw):
            seen.append(list(schedule))
            forced_seen.append(kw.get("forced"))
            return real(teams, schedule, playoff_teams, num_sims, sd, rng, **kw)

        with contextlib.ExitStack() as stack:
            for name, fn in (("get_league", L), ("get_rosters", R), ("get_league_users", U),
                             ("get_nfl_state", S), ("get_matchups", M), ("_simulate", _spy)):
                stack.enter_context(patch.object(pt, name, fn))
            res = await pt.get_playoff_odds("123", num_sims=200, seed=1, my_roster_id=4)

        assert res["this_week_swing"]["opponent_roster_id"] == 5
        full, win_sched = seen[0], seen[1]
        # This week's game stays in the schedule with its winner pinned (so
        # both teams keep their median-game draw); nothing else is decided.
        assert win_sched == full
        (idx, winner), = forced_seen[1].items()
        assert winner == 4 and set(full[idx]) == {4, 5}
        # The week-14 rematch is still to be played, unpinned.
        assert full.count((4, 5)) + full.count((5, 4)) >= 2


class TestTradeFinderRos:
    @pytest.mark.asyncio
    async def test_deadline_passed_returns_no_proposals(self, monkeypatch):
        from nfl_mcp import sleeper_tools, trade_finder_tools

        async def _league(_):
            return {"league": {"name": "T", "settings": {"trade_deadline": 11},
                               "roster_positions": ["QB"], "scoring_settings": {}}}

        async def _rosters(_):
            return {"rosters": [{"roster_id": 1, "owner_id": "me", "players": []}]}

        monkeypatch.setattr(sleeper_tools, "get_league", _league)
        monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
        monkeypatch.setattr(trade_finder_tools, "get_shared_db", lambda *a, **k: FakeDB())
        out = await trade_finder_tools.find_trade_targets("L", roster_id=1, week=12,
                                                           season=2026)
        assert out["success"] is True
        assert out["proposals"] == []
        assert out["trade_deadline"]["passed"] is True
        assert "passed" in out["message"]
