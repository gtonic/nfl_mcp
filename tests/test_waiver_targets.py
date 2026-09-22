"""The waiver question: who is worth claiming, for one specific roster."""
import tempfile
from pathlib import Path

import pytest

from nfl_mcp import waiver_target_tools
from nfl_mcp.database import NFLDatabase
from nfl_mcp.roster_needs import replacement_levels, slot_counts
from nfl_mcp.waiver_target_tools import get_waiver_targets

LEAGUE = "L1"


def _athlete(pid, name, position, team, status="Active"):
    return pid, {
        "player_id": pid, "full_name": name, "position": position,
        "team": team, "status": status,
    }


@pytest.fixture
def db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        database.upsert_athletes(dict([
            # Mine
            _athlete("m1", "My Starter WR", "WR", "BUF"),
            _athlete("m2", "My Second WR", "WR", "KC"),
            _athlete("m3", "My Weak RB", "RB", "NYJ"),
            # Rostered by an opponent — must never be offered
            _athlete("o1", "Their Stud RB", "RB", "SF"),
            # Free agents
            _athlete("f1", "Free Good RB", "RB", "DET"),
            _athlete("f2", "Free Weak WR", "WR", "CAR"),
            _athlete("f3", "Practice Squad Guy", "WR", "CAR", status="Practice Squad"),
            _athlete("f4", "Bye Week Guy", "RB", "LAR"),
        ]))
        database.upsert_schedule_games([
            {"season": 2026, "week": 3, "team": t, "opponent": o, "is_home": home}
            for t, o, home in (
                ("BUF", "KC", 1), ("KC", "BUF", 0),
                ("NYJ", "SF", 1), ("SF", "NYJ", 0),
                ("DET", "CAR", 1), ("CAR", "DET", 0),
            )
        ])  # LAR deliberately absent: a bye week
        monkeypatch.setattr(waiver_target_tools, "NFLDatabase", lambda *a, **k: database)
        yield database


def _stub_sleeper(monkeypatch, points, roster_positions=None, mine=None):
    """Sleeper + projection stubs; `points` maps player name -> projection.

    No FLEX by default: the fixture roster is three players deep, and an empty
    FLEX seat makes *any* free agent a genuine lineup upgrade.
    """
    from nfl_mcp import sleeper_tools

    async def _state():
        return {"nfl_state": {"week": 3, "season": 2026}}

    async def _league(_):
        return {"league": {
            "name": "Test", "total_rosters": 12,
            "scoring_settings": {"rec": 0.5},
            "roster_positions": roster_positions or ["QB", "RB", "RB", "WR", "WR", "BN", "BN"],
            "settings": {"waiver_type": 0},
        }}

    async def _rosters(_):
        return {"rosters": [
            {"roster_id": 7, "owner_id": "me", "players": mine or ["m1", "m2", "m3"]},
            {"roster_id": 2, "owner_id": "them", "players": ["o1"]},
        ]}

    async def _trending(**kwargs):
        return {"trending_players": [{"player_id": "f2", "count": 900}]}

    monkeypatch.setattr(sleeper_tools, "get_nfl_state", _state)
    monkeypatch.setattr(sleeper_tools, "get_league", _league)
    monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
    monkeypatch.setattr(sleeper_tools, "get_trending_players", _trending)

    from nfl_mcp import projections

    async def _project(players, **kwargs):
        return {"projections": [
            {"player": p["name"], "position": p["position"], "team": p["team"],
             "opponent": p["opponent"], "projected_points": points.get(p["name"], 5.0),
             "floor": 1.0, "ceiling": 20.0, "confidence": 70,
             "breakdown": {"base_source": "opportunity"}}
            for p in players
        ]}

    monkeypatch.setattr(projections, "project_players", _project)


class TestSlotCounts:
    def test_flex_is_spread_over_the_positions_that_can_fill_it(self):
        counts = slot_counts(["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN", "IR"])
        assert counts["QB"] == 1
        assert counts["RB"] == pytest.approx(2 + 1 / 3)
        assert "BN" not in counts and "IR" not in counts


class TestReplacementLevels:
    def test_level_is_the_weakest_starter_not_the_best_player(self):
        mine = [
            {"position": "WR", "projected_points": 18.0},
            {"position": "WR", "projected_points": 11.0},
            {"position": "WR", "projected_points": 4.0},
        ]
        # Two WR slots: the bar is the WR2, not the WR1 and not the WR3.
        assert replacement_levels(mine, {"WR": 2})["WR"] == 11.0

    def test_an_unfilled_slot_makes_anything_an_upgrade(self):
        mine = [{"position": "RB", "projected_points": 9.0}]
        assert replacement_levels(mine, {"RB": 2})["RB"] == 0.0


class TestWaiverTargets:
    @pytest.mark.asyncio
    async def test_ranks_free_agents_by_upgrade_over_your_own_starter(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {
            "My Starter WR": 14.0, "My Second WR": 9.0, "My Weak RB": 3.0,
            "Free Good RB": 12.0, "Free Weak WR": 2.0,
        })
        out = await get_waiver_targets(LEAGUE, roster_id=7)

        assert out["success"] is True
        names = [t["name"] for t in out["targets"]]
        assert names[0] == "Free Good RB"
        assert out["targets"][0]["verdict"] == "upgrade"
        # Two RB slots but only one RB on the roster: the second seat is empty,
        # so the bar is zero and the whole projection is the upgrade.
        assert out["replacement_levels"]["RB"] == 0.0
        assert out["targets"][0]["upgrade_points"] == pytest.approx(12.0, abs=0.1)

        # Both WR slots *are* filled, so there the bar is the weaker starter —
        # and a 2.0-point free agent is nowhere near it, so he is not offered.
        assert out["replacement_levels"]["WR"] == 9.0
        assert "Free Weak WR" not in names

    @pytest.mark.asyncio
    async def test_never_offers_a_player_someone_already_rosters(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {"Their Stud RB": 25.0})
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        assert "Their Stud RB" not in [t["name"] for t in out["targets"]]

    @pytest.mark.asyncio
    async def test_skips_inactive_players_and_byes(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {"Practice Squad Guy": 20.0, "Bye Week Guy": 20.0})
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        offered = [t["name"] for t in out["targets"]]
        assert "Practice Squad Guy" not in offered   # not a roster option
        assert "Bye Week Guy" not in offered         # LAR has no game this week

    @pytest.mark.asyncio
    async def test_a_marginal_player_the_league_is_adding_is_speculative(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {
            "My Starter WR": 14.0, "My Second WR": 9.0, "My Weak RB": 3.0,
            "Free Weak WR": 9.2,   # inside the noise band vs the WR2
        })
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        weak = next(t for t in out["targets"] if t["name"] == "Free Weak WR")
        assert weak["verdict"] == "speculative"
        assert weak["trending_adds"] == 900

    @pytest.mark.asyncio
    async def test_hype_alone_does_not_make_a_worse_player_a_claim(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {
            "My Starter WR": 14.0, "My Second WR": 9.0, "My Weak RB": 3.0,
            "Free Weak WR": 2.0,   # far below the WR2 he would replace
        })
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        assert "Free Weak WR" not in [t["name"] for t in out["targets"]]

    @pytest.mark.asyncio
    async def test_says_so_plainly_when_nothing_is_worth_claiming(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {
            "My Starter WR": 14.0, "My Second WR": 9.0, "My Weak RB": 30.0,
            "Free Good RB": 1.0, "Free Weak WR": 1.0,
        })
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        assert out["targets"] == []
        assert "Nothing on waivers" in out["message"]

    @pytest.mark.asyncio
    async def test_reports_the_leagues_own_scoring_and_waiver_type(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {})
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        assert out["league"]["ppr"] == 0.5
        assert out["waiver_type"] == "priority"

    @pytest.mark.asyncio
    async def test_drop_candidates_are_your_weakest_first(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {
            "My Starter WR": 14.0, "My Second WR": 9.0, "My Weak RB": 3.0,
        })
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        assert out["drop_candidates"][0]["name"] == "My Weak RB"

    @pytest.mark.asyncio
    async def test_never_offers_a_position_the_league_does_not_start(self, db, monkeypatch):
        """A league with no K slot must not be told to claim a kicker.

        With no kicker on the roster the replacement level is zero, so every
        kicker scored as a large upgrade and swamped the real targets.
        """
        from nfl_mcp import sleeper_tools

        _stub_sleeper(monkeypatch, {"Free Kicker": 9.5, "My Weak RB": 3.0})
        db.upsert_athletes(dict([_athlete("f9", "Free Kicker", "K", "DET")]))

        out = await get_waiver_targets(LEAGUE, roster_id=7)
        assert "K" not in out["positions_considered"]
        assert "Free Kicker" not in [t["name"] for t in out["targets"]]

        # Explicitly asking for one is still honoured.
        assert sleeper_tools  # (stubs applied above)
        out = await get_waiver_targets(LEAGUE, roster_id=7, positions=["K"])
        assert out["positions_considered"] == ["K"]

    @pytest.mark.asyncio
    async def test_flags_defenses_and_kickers_as_unranked_without_vegas(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {"Free Kicker": 9.5})
        db.upsert_athletes(dict([_athlete("f9", "Free Kicker", "K", "DET")]))

        out = await get_waiver_targets(LEAGUE, roster_id=7, positions=["K"])
        assert out["vegas_active"] is False
        assert out["warnings"]
        # Constant projections must not be dressed up as a ranking.
        assert out["targets"] == []

    @pytest.mark.asyncio
    async def test_unknown_roster_is_an_error_not_an_empty_answer(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {})
        out = await get_waiver_targets(LEAGUE, roster_id=999)
        assert out["success"] is False
        assert "No roster found" in out["error"]

    @pytest.mark.asyncio
    async def test_says_so_when_the_schedule_cache_is_cold(self, db, monkeypatch):
        _stub_sleeper(monkeypatch, {})
        out = await get_waiver_targets(LEAGUE, roster_id=7, week=17)
        assert out["success"] is False
        assert "schedule" in out["error"]


class TestLineupGain:
    """The Dalton Schultz case: a TE who beats your TE1 but not your flex.

    Two FLEX seats spread over RB/WR/TE rounded the roster's second TE into a
    starter, so the free agent was scored against a player who never starts
    (+8.7 instead of +5.7), while `recommend_faab_bid` ignored FLEX and called
    the same player depth.
    """

    @pytest.mark.asyncio
    async def test_upgrade_is_the_change_in_the_best_lineup(self, db, monkeypatch):
        db.upsert_athletes(dict([
            _athlete("t1", "My TE1", "TE", "BUF"),
            _athlete("t2", "My TE2", "TE", "KC"),
            _athlete("r2", "My RB2", "RB", "NYJ"),
            _athlete("x1", "My Flex WR", "WR", "BUF"),
            _athlete("x2", "My Flex RB", "RB", "KC"),
            _athlete("f5", "Free TE", "TE", "DET"),
        ]))
        _stub_sleeper(
            monkeypatch,
            {"My Starter WR": 14.0, "My Second WR": 10.3, "My Weak RB": 10.1,
             "My RB2": 10.2, "My Flex WR": 9.9, "My Flex RB": 9.8,
             "My TE1": 7.4, "My TE2": 4.4, "Free TE": 13.1},
            roster_positions=["RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "BN"],
            mine=["m1", "m2", "m3", "r2", "x1", "x2", "t1", "t2"],
        )
        out = await get_waiver_targets(LEAGUE, roster_id=7, positions=["TE"])
        free_te = next(t for t in out["targets"] if t["name"] == "Free TE")
        # Takes the TE slot from the 7.4, who cannot beat either flex (9.9/9.8);
        # the 4.4 never started, so he is not the bar.
        assert free_te["upgrade_points"] == pytest.approx(13.1 - 7.4, abs=0.1)
        assert out["horizon"] == "this_week"


class TestLineupBars:
    def _roster(self):
        return [
            {"position": "WR", "projected_points": 14.0},
            {"position": "WR", "projected_points": 10.3},
            {"position": "RB", "projected_points": 10.2},
            {"position": "RB", "projected_points": 10.1},
            {"position": "WR", "projected_points": 9.9},
            {"position": "RB", "projected_points": 9.8},
            {"position": "TE", "projected_points": 7.4},
            {"position": "TE", "projected_points": 4.4},
        ]

    def test_the_bar_is_the_weakest_player_who_actually_starts(self):
        from nfl_mcp.roster_needs import lineup_bars
        bars = lineup_bars(self._roster(), {"RB": 2, "WR": 2, "TE": 1, "FLEX": 2})
        # TE1 at 7.4 is the weakest TE-eligible starter; the 4.4 never starts.
        assert bars["TE"] == 7.4
        assert bars["WR"] == bars["RB"] == 9.8
        assert "FLEX" not in bars

    def test_an_empty_eligible_slot_is_a_zero_bar(self):
        from nfl_mcp.roster_needs import lineup_bars
        bars = lineup_bars([{"position": "RB", "projected_points": 9.0}],
                           {"QB": 1, "RB": 2})
        assert bars == {"QB": 0.0, "RB": 0.0}
