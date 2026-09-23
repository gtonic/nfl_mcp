"""Finding a trade, not grading one.

A proposal is only real if the other manager also comes out ahead, so every
candidate swap is scored by recomputing both teams' best legal starting lineup.
"""
import tempfile
from pathlib import Path

import pytest

from nfl_mcp import trade_finder_tools
from nfl_mcp.database import NFLDatabase
from nfl_mcp.roster_needs import (
    lineup_slots,
    replacement_levels,
    slot_counts,
    starting_lineup_total,
    surplus_players,
)
from nfl_mcp.trade_finder_tools import find_trade_targets, swap_gain

LEAGUE = "L1"
ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN", "BN", "BN"]


def _p(name, position, points):
    return {"player_id": name, "name": name, "position": position,
            "team": "BUF", "projected_points": points}


class TestSharedRosterMaths:
    def test_lineup_slots_are_whole_and_renamed(self):
        slots = lineup_slots(["QB", "RB", "RB", "FLEX", "SUPER_FLEX", "DEF", "BN", "IR"])
        assert slots == {"QB": 1, "RB": 2, "FLEX": 1, "SUPERFLEX": 1, "DST": 1}

    def test_slot_counts_spread_the_flex(self):
        counts = slot_counts(ROSTER_POSITIONS)
        assert counts["RB"] == pytest.approx(2 + 1 / 3)
        assert counts["QB"] == 1

    def test_lineup_total_fills_flex_with_the_leftover_best(self):
        players = [_p("QB1", "QB", 20), _p("RB1", "RB", 15), _p("RB2", "RB", 10),
                   _p("WR1", "WR", 14), _p("WR2", "WR", 12), _p("TE1", "TE", 8),
                   _p("RB3", "RB", 11), _p("WR3", "WR", 4)]
        # QB 20 + RB 15/10 + WR 14/12 + TE 8 + FLEX (best leftover = RB3 at 11)
        assert starting_lineup_total(players, lineup_slots(ROSTER_POSITIONS)) == 90

    def test_surplus_is_what_does_not_start(self):
        players = [_p("RB1", "RB", 15), _p("RB2", "RB", 12), _p("RB3", "RB", 11),
                   _p("RB4", "RB", 3)]
        names = {p["name"] for p in surplus_players(players, slot_counts(ROSTER_POSITIONS))}
        # 2 RB slots + a third of the flex rounds to 2 starters.
        assert names == {"RB3", "RB4"}

    def test_replacement_level_is_the_weakest_starter(self):
        players = [_p("WR1", "WR", 18), _p("WR2", "WR", 11), _p("WR3", "WR", 4)]
        assert replacement_levels(players, {"WR": 2})["WR"] == 11


class TestSwapGain:
    def _roster(self):
        return [_p("QB1", "QB", 20), _p("RB1", "RB", 15), _p("RB2", "RB", 6),
                _p("WR1", "WR", 14), _p("WR2", "WR", 13), _p("WR3", "WR", 12),
                _p("TE1", "TE", 8)]

    def test_upgrading_a_starter_is_a_gain(self):
        roster = self._roster()
        give = next(p for p in roster if p["name"] == "RB2")
        gain = swap_gain(roster, lineup_slots(ROSTER_POSITIONS), give, _p("NewRB", "RB", 16))
        assert gain > 0

    def test_trading_surplus_you_never_start_costs_nothing(self):
        """WR3 loses the flex to nobody here, so moving him is free upside."""
        roster = self._roster()
        give = next(p for p in roster if p["name"] == "WR3")
        gain = swap_gain(roster, lineup_slots(ROSTER_POSITIONS), give, _p("NewRB", "RB", 20))
        assert gain > 0

    def test_downgrading_is_negative(self):
        roster = self._roster()
        give = next(p for p in roster if p["name"] == "RB1")
        gain = swap_gain(roster, lineup_slots(ROSTER_POSITIONS), give, _p("Scrub", "RB", 1))
        assert gain < 0


def _athlete(pid, name, position, team="BUF"):
    return pid, {"player_id": pid, "full_name": name, "position": position,
                 "team": team, "status": "Active"}


@pytest.fixture
def db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        # Both rosters carry more players than starting slots — otherwise
        # everyone starts, the total is just the sum, and no swap can help.
        database.upsert_athletes(dict([
            # Mine: deep at WR, a hole at RB
            _athlete("m_qb", "My QB", "QB"),
            _athlete("m_rb1", "My RB1", "RB"),
            _athlete("m_rb2", "My Weak RB", "RB"),
            _athlete("m_wr1", "My WR1", "WR"),
            _athlete("m_wr2", "My WR2", "WR"),
            _athlete("m_wr3", "My WR3", "WR"),
            _athlete("m_wr4", "My Spare WR", "WR"),
            _athlete("m_te", "My TE", "TE"),
            # Theirs: deep at RB, a hole at WR — the mirror image
            _athlete("t_qb", "Their QB", "QB"),
            _athlete("t_rb1", "Their RB1", "RB"),
            _athlete("t_rb2", "Their RB2", "RB"),
            _athlete("t_rb3", "Their RB3", "RB"),
            _athlete("t_rb4", "Their Spare RB", "RB"),
            _athlete("t_wr1", "Their WR1", "WR"),
            _athlete("t_wr2", "Their Weak WR", "WR"),
            _athlete("t_wr3", "Their Worse WR", "WR"),
            _athlete("t_te", "Their TE", "TE"),
        ]))
        database.upsert_schedule_games([
            {"season": 2026, "week": 3, "team": "BUF", "opponent": "KC", "is_home": 1},
            {"season": 2026, "week": 3, "team": "KC", "opponent": "BUF", "is_home": 0},
        ])
        monkeypatch.setattr(trade_finder_tools, "NFLDatabase", lambda *a, **k: database)
        yield database


POINTS = {
    "My QB": 20.0, "My RB1": 15.0, "My Weak RB": 3.0,
    "My WR1": 16.0, "My WR2": 14.0, "My WR3": 13.0, "My Spare WR": 13.0,
    "My TE": 9.0,
    "Their QB": 19.0, "Their RB1": 17.0, "Their RB2": 14.0, "Their RB3": 13.0,
    "Their Spare RB": 12.0,
    "Their WR1": 15.0, "Their Weak WR": 2.0, "Their Worse WR": 1.0, "Their TE": 8.0,
}


def _stub(monkeypatch, points=None):
    from nfl_mcp import projections, sleeper_tools
    points = POINTS if points is None else points

    async def _state():
        return {"nfl_state": {"week": 3, "season": 2026}}

    async def _league(_):
        return {"league": {"name": "Test", "total_rosters": 2,
                           "scoring_settings": {"rec": 0.5},
                           "roster_positions": ROSTER_POSITIONS,
                           "settings": {}}}

    async def _rosters(_):
        return {"rosters": [
            {"roster_id": 7, "owner_id": "me",
             "players": ["m_qb", "m_rb1", "m_rb2", "m_wr1", "m_wr2", "m_wr3",
                         "m_wr4", "m_te"]},
            {"roster_id": 2, "owner_id": "them",
             "players": ["t_qb", "t_rb1", "t_rb2", "t_rb3", "t_rb4", "t_wr1",
                         "t_wr2", "t_wr3", "t_te"]},
        ]}

    async def _users(_):
        return {"users": [{"user_id": "them", "display_name": "Rival"},
                          {"user_id": "me", "display_name": "Me"}]}

    async def _project(players, **kwargs):
        return {"projections": [
            {"player": p["name"], "position": p["position"], "team": p["team"],
             "opponent": p["opponent"], "projected_points": points.get(p["name"], 5.0),
             "floor": 1.0, "ceiling": 20.0}
            for p in players
        ]}

    monkeypatch.setattr(sleeper_tools, "get_nfl_state", _state)
    monkeypatch.setattr(sleeper_tools, "get_league", _league)
    monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
    monkeypatch.setattr(sleeper_tools, "get_league_users", _users)
    monkeypatch.setattr(projections, "project_players", _project)


class TestFindTradeTargets:
    @pytest.mark.asyncio
    async def test_finds_the_mirror_image_trade(self, db, monkeypatch):
        """I am deep at WR and thin at RB; they are the exact opposite."""
        _stub(monkeypatch)
        out = await find_trade_targets(LEAGUE, roster_id=7)

        assert out["success"] is True
        assert out["proposals"]
        best = out["proposals"][0]
        assert best["you_give"]["position"] == "WR"
        assert best["you_get"]["position"] == "RB"
        assert best["your_gain"] > 0 and best["their_gain"] > 0
        assert best["partner"] == "Rival"

    @pytest.mark.asyncio
    async def test_never_proposes_a_trade_the_partner_loses(self, db, monkeypatch):
        """Everyone on my roster is worse than everyone on theirs."""
        lopsided = dict.fromkeys(POINTS, 20.0)
        for name in POINTS:
            if name.startswith("My"):
                lopsided[name] = 2.0
        _stub(monkeypatch, lopsided)

        out = await find_trade_targets(LEAGUE, roster_id=7)
        assert out["proposals"] == []
        assert "No one-for-one trade" in out["message"]

    @pytest.mark.asyncio
    async def test_both_gains_are_reported_not_just_mine(self, db, monkeypatch):
        _stub(monkeypatch)
        out = await find_trade_targets(LEAGUE, roster_id=7)
        for proposal in out["proposals"]:
            assert proposal["their_gain"] >= trade_finder_tools.MEANINGFUL_GAIN
            assert proposal["mutual_gain"] == pytest.approx(
                proposal["your_gain"] + proposal["their_gain"]
            )

    @pytest.mark.asyncio
    async def test_position_filter_restricts_what_you_receive(self, db, monkeypatch):
        _stub(monkeypatch)
        out = await find_trade_targets(LEAGUE, roster_id=7, positions=["TE"])
        assert all(p["you_get"]["position"] == "TE" for p in out["proposals"])

    @pytest.mark.asyncio
    async def test_one_proposal_per_partner(self, db, monkeypatch):
        _stub(monkeypatch)
        out = await find_trade_targets(LEAGUE, roster_id=7)
        partners = [p["partner_roster_id"] for p in out["proposals"]]
        assert len(partners) == len(set(partners))

    @pytest.mark.asyncio
    async def test_says_the_gains_are_weekly_not_season_long(self, db, monkeypatch):
        _stub(monkeypatch)
        out = await find_trade_targets(LEAGUE, roster_id=7)
        assert any("rest-of-season" in c for c in out["caveats"])

    @pytest.mark.asyncio
    async def test_unknown_roster_is_an_error(self, db, monkeypatch):
        _stub(monkeypatch)
        out = await find_trade_targets(LEAGUE, roster_id=999)
        assert out["success"] is False
        assert "No roster found" in out["error"]

    @pytest.mark.asyncio
    async def test_cold_schedule_is_reported_plainly(self, db, monkeypatch):
        _stub(monkeypatch)
        out = await find_trade_targets(LEAGUE, roster_id=7, week=17)
        assert out["success"] is False
        assert "schedule" in out["error"]


class TestRestOfSeasonHorizon:
    @pytest.mark.asyncio
    async def test_candidates_considered_counts_every_swap_scored(self, db, monkeypatch):
        _stub(monkeypatch)
        out = await find_trade_targets(LEAGUE, roster_id=7)
        # 8 of mine x 9 of theirs, whether or not the swap survived.
        assert out["candidates_considered"] == 72
        assert out["proposals_found"] >= len(out["proposals"])

    @pytest.mark.asyncio
    async def test_a_player_on_bye_is_still_tradeable(self, db, monkeypatch):
        from nfl_mcp import ros
        _stub(monkeypatch)
        real = ros.ros_for_ids

        async def _with_bye(ids, **kw):
            by_id, meta = await real(ids, **kw)
            rb1 = by_id["t_rb1"]
            rb1["weekly_points"][3] = 0.0
            rb1["bye_weeks"] = [3]
            rb1["total_points"] = round(sum(rb1["weekly_points"].values()), 1)
            return by_id, meta

        monkeypatch.setattr(ros, "ros_for_ids", _with_bye)
        out = await find_trade_targets(LEAGUE, roster_id=7)
        assert out["horizon"] == "ros"
        best = out["proposals"][0]
        assert best["you_get"]["name"] == "Their RB1"
        assert best["you_get"]["bye_weeks"] == [3]

    @pytest.mark.asyncio
    async def test_week_horizon_is_still_available(self, db, monkeypatch):
        _stub(monkeypatch)
        out = await find_trade_targets(LEAGUE, roster_id=7, horizon="week")
        assert out["horizon"] == "week"
        assert out["proposals"]
