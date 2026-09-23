"""Playoff odds: team strength blends points so far with the roster's projection."""

import contextlib
from unittest.mock import patch

import pytest

from nfl_mcp import playoff_tools as pt


def _round_robin(n: int) -> list[list[tuple[int, int]]]:
    """Circle-method rounds for roster ids 1..n."""
    ids = list(range(1, n + 1))
    rounds = []
    for _ in range(n - 1):
        rounds.append([(ids[i], ids[n - 1 - i]) for i in range(n // 2)])
        ids = [ids[0], ids[-1], *ids[1:-1]]
    return rounds


ROUNDS = _round_robin(12)


def _pairs(week):
    return ROUNDS[(week - 1) % len(ROUNDS)]


# Week 3 of a 12-team league, two games played. Roster 7 is 0-2 on 88 a game
# (bad lineups); everyone else scored ~110.
ACTUAL = {rid: 110.0 + (rid % 3) for rid in range(1, 13)}
ACTUAL[7] = 88.3


def _record(rid):
    wins = 0 if rid == 7 else (2 if rid in (1, 2, 3, 4, 5) else 1)
    return wins, 2 - wins


@contextlib.contextmanager
def _league(projected: dict[int, float] | None):
    async def L(_):
        return {"success": True, "league": {
            "season": "2026", "roster_positions": ["QB", "RB", "WR", "BN"],
            "settings": {"playoff_teams": 6, "playoff_week_start": 15}}}

    async def R(_):
        rosters = []
        for rid in range(1, 13):
            w, lo = _record(rid)
            rosters.append({"roster_id": rid, "owner_id": f"u{rid}", "players": [f"p{rid}"],
                            "settings": {"wins": w, "losses": lo, "ties": 0,
                                         "fpts": int(ACTUAL[rid] * 2),
                                         "fpts_decimal": round(ACTUAL[rid] * 200) % 100}})
        return {"success": True, "rosters": rosters}

    async def U(_):
        return {"success": True, "users": []}

    async def M(_, week):
        ms = []
        for mid, (a, b) in enumerate(_pairs(week), 1):
            for rid in (a, b):
                ms.append({"roster_id": rid, "matchup_id": mid,
                           "points": ACTUAL[rid] + (20 if week == 1 else -20) if week < 3 else 0})
        return {"success": True, "matchups": ms}

    async def S():
        return {"success": True, "nfl_state": {"week": 3, "season": "2026"}}

    async def P(league, league_id, rosters, season, week, db):
        return {"ppg": dict(projected or {}), "weeks": list(range(4, 15))}

    with patch.object(pt, "get_league", L), patch.object(pt, "get_rosters", R), \
         patch.object(pt, "get_league_users", U), patch.object(pt, "get_matchups", M), \
         patch.object(pt, "get_nfl_state", S), patch.object(pt, "_projected_strength", P):
        yield


class TestBlend:
    def test_projection_leads_early_and_fades(self):
        mean, w = pt.blend_strength(88.3, 2, 118.0)
        assert w == pytest.approx(0.2)
        assert mean == pytest.approx(0.2 * 88.3 + 0.8 * 118.0)
        _, mid = pt.blend_strength(100.0, 8, 118.0)
        assert mid == 0.5
        _, late = pt.blend_strength(100.0, 12, 118.0)
        assert late == pytest.approx(0.6)

    def test_missing_pieces(self):
        assert pt.blend_strength(100.0, 2, None) == (100.0, 1.0)
        assert pt.blend_strength(None, 0, 115.0) == (115.0, 0.0)


class TestOddsWithProjection:
    @pytest.mark.asyncio
    async def test_a_0_2_team_with_an_average_roster_is_alive(self):
        # Everyone projects about the same; roster 7 slightly above average.
        projected = dict.fromkeys(range(1, 13), 115.0)
        projected[7] = 118.0
        with _league(projected):
            res = await pt.get_playoff_odds("L", num_sims=4000, seed=3, my_roster_id=7, db=object())
        me = next(o for o in res["odds"] if o["roster_id"] == 7)
        assert res["strength_source"] == "blended"
        assert me["actual_ppg"] == pytest.approx(88.3, abs=0.1)
        assert me["projected_ppg"] == 118.0
        assert me["actual_weight"] == pytest.approx(0.2, abs=0.01)
        assert 15.0 <= me["playoff_pct"] <= 45.0
        assert res["this_week_swing"]["if_win_pct"] > me["playoff_pct"]

    @pytest.mark.asyncio
    async def test_without_projections_it_is_actuals_alone(self):
        with _league({}):
            res = await pt.get_playoff_odds("L", num_sims=4000, seed=3, db=object())
        me = next(o for o in res["odds"] if o["roster_id"] == 7)
        assert res["strength_source"] == "actual"
        assert me["projected_ppg"] is None
        assert me["mean_ppg"] == pytest.approx(88.3, abs=0.1)
        assert me["playoff_pct"] < 5.0


class TestProjectedStrength:
    @pytest.mark.asyncio
    async def test_weekly_best_lineup_covers_a_bye_and_is_cached(self, monkeypatch):
        from nfl_mcp import ros

        calls = []

        async def _ros(ids, *, league, season, week, db, include_weekly=False):
            calls.append(ids)
            weeks = list(range(week, 15))
            qb = {"player_id": "qb", "position": "QB",
                  "weekly_points": {w: (0.0 if w == 6 else 20.0) for w in weeks}}
            qb2 = {"player_id": "qb2", "position": "QB",
                   "weekly_points": dict.fromkeys(weeks, 12.0)}
            return ({"qb": qb, "qb2": qb2},
                    {"windows": {"regular": weeks, "playoff": [15, 16, 17]}})

        monkeypatch.setattr(ros, "ros_for_ids", _ros)
        pt._projection_cache.clear()
        league = {"roster_positions": ["QB", "BN"]}
        rosters = [{"roster_id": 1, "players": ["qb", "qb2"]}]
        out = await pt._projected_strength(league, "L", rosters, 2026, 3, db=object())
        # Weeks 4-14 (the current week is left out): 20 a week, 12 on the bye.
        assert out["weeks"] == list(range(4, 15))
        assert out["ppg"][1] == pytest.approx((20.0 * 10 + 12.0) / 11)
        await pt._projected_strength(league, "L", rosters, 2026, 3, db=object())
        assert len(calls) == 1  # cached per league/week/rosters

    def test_unnamed_team_defense_gets_a_ros_input(self):
        from nfl_mcp.ros import ros_input
        # The athlete cache stores Sleeper DEFs with no name.
        row = {"id": "KC", "full_name": None, "team_id": "KC", "position": "DEF"}
        out = ros_input(row, {})
        assert out["name"] == "KC" and out["position"] == "DEF"

    @pytest.mark.asyncio
    async def test_no_db_no_projection(self):
        out = await pt._projected_strength({}, "L", [], 2026, 3, db=None)
        assert out == {"ppg": {}, "weeks": []}
