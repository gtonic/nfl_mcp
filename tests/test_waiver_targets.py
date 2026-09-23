"""The waiver question: who is worth claiming, for one specific roster."""
import tempfile
from pathlib import Path

import pytest

from nfl_mcp import waiver_target_tools
from nfl_mcp.database import NFLDatabase
from nfl_mcp.roster_needs import replacement_levels, slot_counts
from nfl_mcp.waiver_target_tools import get_waiver_targets

LEAGUE = "L1"


def _athlete(pid, name, position, team, status="Active", injury_status=None):
    return pid, {
        "player_id": pid, "full_name": name, "position": position,
        "team": team, "status": status, "injury_status": injury_status,
    }


class _FakeValues:
    """Market values by player name; None when a test does not care."""

    def __init__(self, by_name):
        self.by_name = by_name

    async def get_values(self, *a, **k):
        return {"list": [{"value": v} for v in self.by_name.values()]}

    def lookup(self, idx, player_id=None, name=None, position=None):
        value = self.by_name.get(name)
        return {"value": value} if value is not None else None


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


def _stub_sleeper(monkeypatch, points, roster_positions=None, mine=None,
                  values=None, starters=None):
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
            {"roster_id": 7, "owner_id": "me", "players": mine or ["m1", "m2", "m3"],
             "starters": starters or []},
            {"roster_id": 2, "owner_id": "them", "players": ["o1"]},
        ]}

    async def _trending(**kwargs):
        return {"trending_players": [{"player_id": "f2", "count": 900}]}

    monkeypatch.setattr(sleeper_tools, "get_nfl_state", _state)
    monkeypatch.setattr(sleeper_tools, "get_league", _league)
    monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
    monkeypatch.setattr(sleeper_tools, "get_trending_players", _trending)

    async def _transactions(_league, week=None):
        return {"transactions": []}

    monkeypatch.setattr(sleeper_tools, "get_transactions", _transactions)
    monkeypatch.setattr(waiver_target_tools, "get_values_service",
                        lambda db=None: _FakeValues(values or {}))

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
    async def test_a_full_starting_lineup_offers_no_drops(self, db, monkeypatch):
        # All three start (RB/WR/WR): the old list was simply the five lowest
        # projections, starters included.
        _stub_sleeper(monkeypatch, {
            "My Starter WR": 14.0, "My Second WR": 9.0, "My Weak RB": 3.0,
        })
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        assert out["drop_candidates"] == []

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


class TestDropCandidates:
    """The Mike Evans case: a FLEX starter worth 2249 listed as a drop.

    Drops were the five lowest projections this week, starters included, with
    no regard for rest-of-season value, and any claim could be paired with them.
    """

    SLOTS = ["RB", "WR", "WR", "BN", "BN", "BN"]
    ROSTER = ["m1", "m2", "m3", "b1", "b2", "b3"]
    POINTS = {"My Starter WR": 14.0, "My Second WR": 9.0, "My Weak RB": 8.0,
              "Bench Stud WR": 4.0, "Bench Scrub RB": 6.0, "Hurt Bench RB": 0.0,
              "Free Good RB": 12.0, "Free Weak WR": 9.2}
    VALUES = {"My Starter WR": 6000, "My Second WR": 2249, "My Weak RB": 1500,
              "Bench Stud WR": 5000, "Bench Scrub RB": 100, "Hurt Bench RB": 4000,
              "Free Good RB": 900, "Free Weak WR": 50}

    @pytest.fixture(autouse=True)
    def _bench(self, db):
        db.upsert_athletes(dict([
            _athlete("b1", "Bench Stud WR", "WR", "KC"),
            _athlete("b2", "Bench Scrub RB", "RB", "NYJ"),
            _athlete("b3", "Hurt Bench RB", "RB", "SF", injury_status="Out"),
        ]))

    async def _run(self, monkeypatch, **kw):
        args = {"roster_positions": self.SLOTS, "mine": self.ROSTER,
                "values": self.VALUES}
        args.update(kw)
        _stub_sleeper(monkeypatch, {**self.POINTS, **args.pop("points", {})}, **args)
        return await get_waiver_targets(LEAGUE, roster_id=7)

    @pytest.mark.asyncio
    async def test_only_bench_players_ranked_by_value_then_projection(self, db, monkeypatch):
        out = await self._run(monkeypatch)
        names = [d["name"] for d in out["drop_candidates"]]
        # Starters never; the Out player never (a zero says nothing about his
        # season); the low-value scrub before the valuable stash, even though
        # the stash projects fewer points this week.
        assert names == ["Bench Scrub RB", "Bench Stud WR"]
        assert out["drop_candidates"][0]["value"] == 100
        assert [p["name"] for p in out["injured_not_dropped"]] == ["Hurt Bench RB"]

    @pytest.mark.asyncio
    async def test_a_set_starter_is_never_a_drop(self, db, monkeypatch):
        # He projects below the bench stash, so the optimizer would sit him —
        # but the manager starts him, and that is not a drop recommendation.
        out = await self._run(monkeypatch, points={"My Second WR": 3.0},
                              starters=["m1", "m2", "m3"])
        assert "My Second WR" not in [d["name"] for d in out["drop_candidates"]]

    @pytest.mark.asyncio
    async def test_a_claim_is_paired_only_with_a_player_worth_less(self, db, monkeypatch):
        # Market-value pairing (no rest-of-season projection available).
        from nfl_mcp import waiver_target_tools

        async def _no_ros(*_a, **_k):
            return False
        monkeypatch.setattr(waiver_target_tools, "_attach_ros", _no_ros)
        out = await self._run(monkeypatch)
        assert out["drop_ranking"] == "market_value"
        by_name = {t["name"]: t for t in out["targets"]}
        # 900 beats the 100 scrub: a real pairing.
        assert by_name["Free Good RB"]["drop"]["name"] == "Bench Scrub RB"
        # 50 beats nobody on the bench: no drop, and says why.
        weak = by_name["Free Weak WR"]
        assert weak["drop"] is None
        assert "Nobody on your bench" in weak["drop_note"]

    @pytest.mark.asyncio
    async def test_an_open_roster_spot_needs_no_drop(self, db, monkeypatch):
        out = await self._run(monkeypatch, roster_positions=[*self.SLOTS, "BN"])
        assert out["open_roster_spots"] == 1
        assert out["targets"]
        assert all(t["drop"] is None for t in out["targets"])

    @pytest.mark.asyncio
    async def test_rest_of_season_points_are_the_main_drop_term(self, db, monkeypatch):
        """A stash whose season is effectively over goes before a scrub who
        will keep scoring, whatever the market still says about the stash."""
        from nfl_mcp import ros
        totals = {"m1": 200.0, "m2": 150.0, "m3": 120.0, "b1": 0.0, "b2": 150.0,
                  "b3": 60.0, "f1": 170.0, "f2": 20.0}

        async def _ros(ids, **_k):
            return {i: {"total_points": totals.get(i, 0.0)} for i in ids}, {}
        monkeypatch.setattr(ros, "ros_for_ids", _ros)
        out = await self._run(monkeypatch)
        assert out["drop_ranking"] == "ros"
        names = [d["name"] for d in out["drop_candidates"]]
        assert names[0] == "Bench Stud WR"

    @pytest.mark.asyncio
    async def test_rolling_waivers_say_a_claim_costs_priority(self, db, monkeypatch):
        out = await self._run(monkeypatch)
        assert "back of the order" in out["message"]


class TestKickoffsAndPriority:
    """Locks and rolling-waiver advice, with kickoffs in the schedule."""

    @pytest.mark.asyncio
    async def test_started_games_and_priority_strategy(self, db, monkeypatch):
        from datetime import UTC, datetime

        # DET/CAR kicked off (Thursday), BUF/KC and NYJ/SF on Sunday.
        db.upsert_schedule_games([
            {"season": 2026, "week": 3, "team": t, "opponent": o, "is_home": h, "kickoff": k}
            for t, o, h, k in (
                ("DET", "CAR", 1, "2026-09-25T00:15Z"), ("CAR", "DET", 0, "2026-09-25T00:15Z"),
                ("BUF", "KC", 1, "2026-09-27T17:00Z"), ("KC", "BUF", 0, "2026-09-27T17:00Z"),
                ("NYJ", "SF", 1, "2026-09-27T17:00Z"), ("SF", "NYJ", 0, "2026-09-27T17:00Z"),
            )
        ])
        db.upsert_athletes(dict([_athlete("f5", "Sunday RB", "RB", "SF")]))
        _stub_sleeper(monkeypatch, {
            "My Starter WR": 14.0, "My Second WR": 9.0, "My Weak RB": 3.0,
            "Free Good RB": 12.0, "Sunday RB": 10.0,
        })
        monkeypatch.setattr(waiver_target_tools, "_now",
                            lambda: datetime(2026, 9, 25, 2, tzinfo=UTC))
        out = await get_waiver_targets(LEAGUE, roster_id=7)

        names = [t["name"] for t in out["targets"]]
        # Free Good RB (DET) is already playing: not a target this week.
        assert "Free Good RB" not in names
        assert [t["name"] for t in out["too_late_for_this_week"]] == ["Free Good RB"]
        sunday = next(t for t in out["targets"] if t["name"] == "Sunday RB")
        assert sunday["locked"] is False
        assert sunday["kickoff_local"] == "2026-09-27T19:00:00+02:00"
        # Free agent after Wednesday's run: add him, no priority needed.
        assert sunday["waiver_strategy"]["recommendation"] == "add_now"
        assert out["waiver_priority"]["rolling"] is True
