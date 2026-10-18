"""Late-game risk planner: when a questionable starter has to be decided.

Synthetic week (2026 week 6 shape): TNF Thu 00:15 UTC, Sunday early 17:00,
late 20:25, SNF Mon 00:20, MNF Tue 00:15 (02:15 Vienna). "Now" is Friday.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nfl_mcp import late_risk as lr

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)          # Fri 22:00 Vienna
TNF = datetime(2026, 10, 23, 0, 15, tzinfo=UTC)          # next Thu night (for the TNF case)
EARLY = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)        # Sun 19:00 Vienna
LATE = datetime(2026, 10, 18, 20, 25, tzinfo=UTC)        # Sun 22:25
SNF = datetime(2026, 10, 19, 0, 20, tzinfo=UTC)          # Mon 02:20
MNF = datetime(2026, 10, 20, 0, 15, tzinfo=UTC)          # Tue 02:15


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def player(name, pos, kick, pts, slot=None, status=None, practice=None, pattern=None,
           locked=False, pid=None, **extra):
    row = {"name": name, "player_id": pid or name.lower().replace(" ", "_"), "position": pos,
           "team": "XX", "projected_points": pts, "kickoff": iso(kick) if kick else None,
           "locked": locked, "injury_status": status, "practice_status": practice,
           "practice_pattern": pattern, "news_flags": [], "gameday_status": None, **extra}
    if slot:
        row["slot"] = slot
    return row


def evans(**kw):
    base = {"status": "Questionable", "practice": "DNP", "pattern": "DNP-DNP"}
    base.update(kw)
    return player("Mike Evans", "WR", MNF, 7.9, slot="WR", **base)


def flex(name="Romeo Doubs", kick=EARLY, pts=7.1):
    return player(name, "WR", kick, pts, slot="FLEX")


class TestPlayProbability:
    @pytest.mark.parametrize("status,practice,pattern,expected", [
        ("Questionable", "DNP", "DNP-DNP", 0.56),
        ("Questionable", "DNP", "DNP", 0.72),
        ("Questionable", "LP", "DNP-LP", 0.66),
        ("Questionable", "FP", "LP-FP", 0.86),
        ("Questionable", None, None, 0.68),
        ("Doubtful", None, None, 0.02),
        ("Out", None, None, 0.0),
        (None, None, None, 1.0),
    ])
    def test_designation_and_practice(self, status, practice, pattern, expected):
        p, _ = lr.play_probability({"injury_status": status, "practice_status": practice,
                                    "practice_pattern": pattern})
        assert p == expected

    def test_published_gameday_list_settles_it(self):
        assert lr.play_probability({"injury_status": "Questionable",
                                    "gameday_status": "inactive"})[0] == 0.0
        assert lr.play_probability({"injury_status": "Questionable",
                                    "gameday_status": "active"})[0] == 1.0

    def test_news_caps_and_floors(self):
        p, basis = lr.play_probability({"injury_status": "Questionable", "practice_status": "FP",
                                        "news_flags": [{"flag": "unlikely_to_play"}]})
        assert p == 0.25 and "unlikely to play" in basis
        p, _ = lr.play_probability({"injury_status": "Questionable", "practice_status": "DNP",
                                    "practice_pattern": "DNP-DNP",
                                    "news_flags": [{"flag": "expected_to_play"}]})
        assert p == 0.85


class TestRiskReasons:
    def test_healthy_and_limited_is_no_risk(self):
        assert lr.risk_reasons({"injury_status": None, "practice_status": "LP"}) == []
        assert lr.risk_reasons({"injury_status": None}) == []

    def test_dnp_without_designation_news_and_tags(self):
        assert lr.risk_reasons({"injury_status": None, "practice_status": "DNP"})
        assert lr.risk_reasons({"injury_status": None,
                                "news_flags": [{"flag": "game_time_decision"}]})
        assert "designation Questionable" in lr.risk_reasons({"injury_status": "Questionable"})

    def test_confirmed_active_is_settled(self):
        assert lr.risk_reasons({"injury_status": "Questionable", "gameday_status": "active"}) == []

    def test_if_active_points_lifts_the_discount(self):
        # DNP-DNP: 0.54 before inactives, 0.82 once confirmed active.
        assert lr.if_active_points({"projected_points": 7.9, "injury_status": "Questionable",
                                    "practice_status": "DNP", "practice_pattern": "DNP-DNP"}) == 12.0
        assert lr.if_active_points({"projected_points": 0.0, "injury_status": "Out"}) is None


class TestMondayNightStarter:
    """Evans: MNF, every replacement plays Sunday -> decide before Sunday."""

    def test_better_sunday_replacement_means_swap_before_its_kickoff(self):
        starters = [evans(), flex()]
        bench = [player("Bench WR", "WR", EARLY, 10.5), player("Late WR", "WR", LATE, 9.0)]
        out = lr.plan_contingencies(starters, bench, now=NOW)
        plan = out["at_risk"][0]
        assert plan["recommendation"] == "swap_now"
        assert plan["options"]["swap"]["player"] == "Bench WR"
        assert plan["decision_deadline"] == iso(EARLY)
        assert plan["decision_deadline_local"].startswith("2026-10-18T19:00")
        assert plan["inactives_expected_local"].startswith("2026-10-20T00:45")
        assert plan["options"]["wait"] is None
        assert all(not r["unlocked_at_his_inactives"] for r in plan["replacements"])
        assert "after every replacement has locked" in plan["summary"]
        assert out["earliest_deadline"] == iso(EARLY)

    def test_close_call_keeps_him_but_names_the_deadline(self):
        out = lr.plan_contingencies([evans(), flex()], [player("Bench WR", "WR", LATE, 8.4)], now=NOW)
        plan = out["at_risk"][0]
        assert plan["recommendation"] == "keep"
        assert plan.get("close_call") is True
        assert plan["decision_deadline"] == iso(LATE)
        assert "before his inactives" in plan["summary"]

    def test_best_band_deadline_is_the_earliest_lock(self):
        bench = [player("Late WR", "WR", LATE, 10.0), player("Early WR", "WR", EARLY, 9.6)]
        plan = lr.plan_contingencies([evans(), flex()], bench, now=NOW)["at_risk"][0]
        assert plan["options"]["swap"]["player"] == "Late WR"
        # Early WR is within BEST_BAND of the best and locks first.
        assert plan["decision_deadline"] == iso(EARLY)

    def test_same_game_bench_player_is_a_fallback(self):
        bench = [player("Same Game WR", "WR", MNF, 6.0), player("Sunday WR", "WR", EARLY, 7.0)]
        plan = lr.plan_contingencies([evans(), flex()], bench, now=NOW)["at_risk"][0]
        assert plan["recommendation"] == "wait"
        assert plan["options"]["wait"]["fallback"] == "Same Game WR"
        assert plan["options"]["wait"]["check_at_local"].startswith("2026-10-20T00:50")
        # 7.9 + (1 - 0.56) * 6.0
        assert plan["options"]["wait"]["expected_points"] == pytest.approx(10.5, abs=0.1)


class TestRearrange:
    """Ropeway, week 6: Evans (WR slot, MNF, Q) and DeVonta Smith (FLEX,
    Sunday). The only player unlocked at Evans' inactives is Rachaad White,
    a free-agent back in the same Monday game -- he cannot take a WR slot."""

    def lineup(self):
        return [evans(), player("DeVonta Smith", "WR", EARLY, 9.0, slot="FLEX")]

    def test_swap_with_the_flex_receiver_keeps_the_monday_back(self):
        fa = [player("Rachaad White", "RB", MNF, 6.0)]
        bench = [player("Sunday WR", "WR", EARLY, 5.0)]
        plan = lr.plan_contingencies(self.lineup(), bench, fa, open_spots=1, now=NOW)["at_risk"][0]
        assert plan["recommendation"] == "wait"
        wait = plan["options"]["wait"]
        assert wait["fallback"] == "Rachaad White" and wait["requires_rearrange"] is True
        # 7.9 + (1 - 0.56) * 6.0
        assert wait["expected_points"] == pytest.approx(10.5, abs=0.1)
        step = plan["rearrange"]
        assert step["with"] == "DeVonta Smith" and step["unlocks_fallback"] == "Rachaad White"
        assert [(m["player"], m["to_slot"]) for m in step["moves"]] == [
            ("Mike Evans", "FLEX"), ("DeVonta Smith", "WR")]
        # Smith's Sunday kickoff locks the swap: the first deadline.
        assert step["act_by"] == iso(EARLY)
        assert plan["decision_deadline"] == iso(EARLY)
        assert plan["summary"].startswith("move Mike Evans to FLEX and DeVonta Smith to WR before Sun")
        assert "Rachaad White" in plan["summary"]

    def test_already_in_flex_needs_no_rearrange(self):
        """The briefing's seating (Evans in FLEX) finds White directly."""
        fa = [player("Rachaad White", "RB", MNF, 6.0)]
        starters = [player("Mike Evans", "WR", MNF, 7.9, slot="FLEX", status="Questionable",
                           practice="DNP", pattern="DNP-DNP"),
                    player("DeVonta Smith", "WR", EARLY, 9.0, slot="WR")]
        plan = lr.plan_contingencies(starters, [], fa, open_spots=1, now=NOW)["at_risk"][0]
        assert plan["recommendation"] == "wait"
        assert "rearrange" not in plan and "requires_rearrange" not in plan["options"]["wait"]
        assert plan["decision_deadline"] == iso(MNF)

    def test_no_rearrange_once_the_flex_starter_is_locked(self):
        fa = [player("Rachaad White", "RB", MNF, 6.0)]
        later = EARLY + timedelta(minutes=10)
        starters = [evans(), player("DeVonta Smith", "WR", EARLY, 9.0, slot="FLEX", locked=True)]
        plan = lr.plan_contingencies(starters, [], fa, open_spots=1, now=later)["at_risk"][0]
        assert plan["options"]["wait"] is None and "rearrange" not in plan

    def test_a_same_slot_fallback_is_not_displaced_by_a_weaker_rearrange(self):
        fa = [player("Rachaad White", "RB", MNF, 4.0)]
        bench = [player("Monday WR", "WR", MNF, 6.0)]
        plan = lr.plan_contingencies(self.lineup(), bench, fa, open_spots=1, now=NOW)["at_risk"][0]
        assert plan["options"]["wait"]["fallback"] == "Monday WR"
        assert "rearrange" not in plan

    def test_rearrange_needs_eligible_slots(self):
        # A TE in the TE slot cannot trade seats with a WR in WR.
        starters = [evans(), player("Some TE", "TE", EARLY, 6.0, slot="TE")]
        fa = [player("Rachaad White", "RB", MNF, 6.0)]
        plan = lr.plan_contingencies(starters, [], fa, open_spots=1, now=NOW)["at_risk"][0]
        assert plan["options"]["wait"] is None and "rearrange" not in plan


class TestSundayStarters:
    def test_early_starter_with_a_late_bench_option_waits(self):
        s = player("Zay Flowers", "WR", EARLY, 9.0, slot="WR", status="Questionable",
                   practice="LP", pattern="LP")
        bench = [player("Ladd McConkey", "WR", LATE, 5.7)]
        plan = lr.plan_contingencies([s], bench, now=NOW)["at_risk"][0]
        assert plan["recommendation"] == "wait"
        assert plan["options"]["wait"]["check_at_local"].startswith("2026-10-18T17:35")
        assert plan["decision_deadline"] == iso(LATE)
        assert "17:30" in plan["summary"]

    def test_snf_starter_only_same_game_players_are_fallbacks(self):
        s = player("SNF WR", "WR", SNF, 10.0, slot="WR", status="Questionable", practice="LP")
        bench = [player("Early WR", "WR", EARLY, 8.0), player("Late WR", "WR", LATE, 7.0),
                 player("Monday WR", "WR", MNF, 4.0)]
        plan = lr.plan_contingencies([s], bench, now=NOW)["at_risk"][0]
        fallbacks = [r["player"] for r in plan["replacements"] if r["unlocked_at_his_inactives"]]
        assert fallbacks == ["Monday WR"]
        assert plan["recommendation"] == "wait"  # 10 + 0.34 * 4 beats 8.0

    def test_tnf_starter_is_decided_at_his_own_inactives(self):
        now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
        sunday = TNF + timedelta(days=3)
        s = player("TNF RB", "RB", TNF, 12.0, slot="RB", status="Questionable", practice="DNP",
                   pattern="DNP-DNP")
        plan = lr.plan_contingencies([s], [player("Sunday RB", "RB", sunday, 9.0)], now=now)["at_risk"][0]
        assert plan["recommendation"] == "wait"
        assert plan["options"]["wait"]["check_at"] == iso(TNF - timedelta(minutes=85))


class TestEdges:
    def test_officially_inactive_swaps_to_any_unlocked_option(self):
        s = evans(gameday_status="inactive")
        out = lr.plan_contingencies([s, flex()], [player("Monday WR", "WR", MNF, 3.0)],
                                    now=MNF - timedelta(minutes=60))
        plan = out["at_risk"][0]
        assert plan["recommendation"] == "swap_now"
        assert plan["play_probability"] == 0.0

    def test_locked_starter_and_locked_replacements_are_skipped(self):
        out = lr.plan_contingencies([evans(locked=True)], [player("B", "WR", EARLY, 9)], now=NOW)
        assert out["at_risk"] == []
        later = EARLY + timedelta(minutes=10)
        plan = lr.plan_contingencies([evans()], [player("Locked WR", "WR", EARLY, 20.0, locked=True)],
                                     now=later)["at_risk"][0]
        assert plan["recommendation"] == "no_alternative"

    def test_healthy_lineup_has_nothing_to_plan(self):
        out = lr.plan_contingencies([player("A", "WR", EARLY, 10, slot="WR")], [], now=NOW)
        assert out["at_risk"] == [] and "No starter" in out["summary"]

    def test_slot_eligibility_and_flex_chain(self):
        # Evans in WR; the bench has only an RB: the FLEX WR moves into the WR
        # slot and the RB takes the FLEX.
        bench = [player("Bench RB", "RB", EARLY, 11.0), player("Bench QB", "QB", EARLY, 20.0)]
        plan = lr.plan_contingencies([evans(), flex()], bench, now=NOW)["at_risk"][0]
        assert [r["player"] for r in plan["replacements"]] == ["Bench RB"]
        row = plan["replacements"][0]
        assert row["via"] == "Romeo Doubs"
        assert [m["to_slot"] for m in row["moves"]] == ["WR", "FLEX", "BENCH"]

    def test_free_agent_on_a_full_roster_names_the_drop(self):
        bench = [player("Keeper", "WR", EARLY, 6.0, value=4000),
                 player("Hurt WR1", "WR", EARLY, 0.5, status="Questionable", value=6000),
                 player("Cut Me", "TE", LATE, 3.0, value=200)]
        fa = [player("FA WR", "WR", LATE, 11.0)]
        plan = lr.plan_contingencies([evans(), flex()], bench, fa, open_spots=0, now=NOW)["at_risk"][0]
        row = next(r for r in plan["replacements"] if r["player"] == "FA WR")
        assert row["source"] == "free_agent" and row["drop"] == "Cut Me"
        assert "drop_note" in plan

    def test_a_bench_option_beats_an_add_within_add_cost(self):
        bench = [player("Bench WR", "WR", EARLY, 9.0), player("Cut Me", "TE", LATE, 1.0)]
        fa = [player("FA WR", "WR", EARLY, 9.3)]
        plan = lr.plan_contingencies([evans(), flex()], bench, fa, open_spots=0, now=NOW)["at_risk"][0]
        assert plan["options"]["swap"]["player"] == "Bench WR"

    def test_no_droppable_player_means_no_free_agent(self):
        fa = [player("FA WR", "WR", LATE, 11.0)]
        plan = lr.plan_contingencies([evans()], [], fa, open_spots=0, now=NOW)["at_risk"][0]
        assert plan["replacements"] == []

    def test_open_roster_spots(self):
        league = {"roster_positions": ["QB", "WR", "BN", "BN", "IR"]}
        assert lr.open_roster_spots(league, {"players": ["1", "2", "3"], "reserve": ["3"]}) == 2
        assert lr.open_roster_spots(league, {"players": ["1", "2", "3", "4"]}) == 0

    def test_needed_positions_cover_flex(self):
        assert lr.needed_positions([player("A", "RB", EARLY, 5, slot="FLEX", status="Doubtful")]) \
            == ["RB", "TE", "WR"]
        assert lr.needed_positions([player("A", "RB", EARLY, 5, slot="RB")]) == []


class TestBuildBlock:
    async def test_free_agents_timeout_degrades_to_bench(self, monkeypatch):
        async def slow(*_a, **_k):
            import asyncio
            await asyncio.sleep(1)
            return []
        monkeypatch.setattr(lr, "free_agent_options", slow)
        out = await lr.build_block([evans()], [player("B", "WR", LATE, 9.5)],
                                   league={"roster_positions": ["WR", "BN"]}, rosters=[],
                                   mine={"players": ["a"]}, db=MagicMock(), season=2026, week=6,
                                   now=NOW, budget=0.05)
        assert out["free_agents_checked"] is False
        assert "bench players only" in out["free_agents_note"]
        assert out["at_risk"][0]["recommendation"] == "swap_now"

    async def test_no_risk_reads_no_free_agents(self, monkeypatch):
        fa = AsyncMock(return_value=[])
        monkeypatch.setattr(lr, "free_agent_options", fa)
        out = await lr.build_block([player("A", "WR", EARLY, 10, slot="WR")], [],
                                   league={}, rosters=[], mine={}, db=None, season=2026, week=6, now=NOW)
        fa.assert_not_called()
        assert out["at_risk"] == []


class TestFreeAgentOptions:
    async def test_instant_adds_only_projected_in_league_scoring(self):
        db = MagicMock()
        db.get_week_opponents = MagicMock(return_value={"SF": "WSH", "LV": "BUF", "WSH": "SF",
                                                         "BUF": "LV"})
        db.get_athletes_by_positions = MagicMock(return_value=[
            {"id": "1", "full_name": "Free One", "position": "WR", "team_id": "LV", "status": "Active",
             "raw": {"depth_chart_order": 1}},
            {"id": "2", "full_name": "Taken Two", "position": "WR", "team_id": "SF", "status": "Active",
             "raw": {"depth_chart_order": 1}},
        ])
        db.get_all_current_injuries = MagicMock(return_value=[])
        db.get_week_game_states = MagicMock(return_value={
            "LV": {"kickoff": iso(LATE)}, "SF": {"kickoff": iso(MNF)}})
        projections = {"projections": [{"player": "Free One", "team": "LV", "position": "WR",
                                        "projected_points": 7.6}]}
        with patch("nfl_mcp.projections.project_players", AsyncMock(return_value=projections)) as proj, \
             patch("nfl_mcp.sleeper_tools.get_transactions", AsyncMock(return_value={"transactions": []})):
            out = await lr.free_agent_options({"league_id": "L", "settings": {}}, [{"players": ["2"]}],
                                              db, 2026, 6, ["WR"], now=NOW)
        assert [r["name"] for r in out] == ["Free One"]
        assert out[0]["kickoff"] == iso(LATE) and out[0]["projected_points"] == 7.6
        assert [i["name"] for i in proj.call_args.args[0]] == ["Free One"]


class TestWiring:
    async def test_briefing_block_uses_the_recommended_lineup(self, monkeypatch):
        from nfl_mcp import briefing_tools
        monkeypatch.setattr(lr, "_now", lambda: NOW)
        monkeypatch.setattr(lr, "free_agent_options", AsyncMock(return_value=[]))
        games = {"SF": {"kickoff": iso(MNF)}, "BAL": {"kickoff": iso(EARLY)}}
        my_all = [{"name": "Mike Evans", "team": "SF", "position": "WR", "player_id": "2216",
                   "projected_points": 7.9},
                  {"name": "Bench Guy", "team": "BAL", "position": "WR", "player_id": "9",
                   "projected_points": 10.0}]
        proj = {"projections": [
            {"player": "Mike Evans", "team": "SF", "position": "WR", "projected_points": 7.9,
             "injury_status": "Questionable", "practice_status": "DNP", "practice_pattern": "DNP-DNP"},
            {"player": "Bench Guy", "team": "BAL", "position": "WR", "projected_points": 10.0}]}
        out = await briefing_tools._late_game_risk(
            [{"slot": "WR", "player": "Mike Evans"}], my_all, proj,
            [{"name": "Mike Evans", "team": "SF", "player_id": "2216"}], games,
            league={"roster_positions": ["WR", "BN"]}, rosters=[], mine={"players": ["2216", "9"]},
            db=MagicMock(), season=2026, week=6)
        plan = out["at_risk"][0]
        assert plan["player"] == "Mike Evans" and plan["recommendation"] == "swap_now"
        assert plan["decision_deadline_local"].startswith("2026-10-18T19:00")

    def test_from_analysis_shape(self):
        row = lr.from_analysis({"player_name": "A", "player_id": "1", "position": "WR", "team": "SF",
                                "projected_points": 5, "injury_status": "Questionable",
                                "practice_pattern": "DNP", "kickoff": iso(MNF), "locked": False}, "WR")
        assert row["name"] == "A" and row["slot"] == "WR" and row["kickoff"] == iso(MNF)


class TestNewsMovesExpectedPoints:
    """Evans, week 6: Q + DNP-DNP-DNP but "still expected to play" -- the
    projection (practice discount only) and P(plays) (news floor 0.85) must
    not disagree in the keep/swap call."""

    def test_expected_to_play_reprices_keep(self):
        e = evans(news_flags=[{"flag": "expected_to_play"}])
        plan = lr.plan_contingencies([e, flex()], [player("Bench RB", "WR", LATE, 9.5)], now=NOW)["at_risk"][0]
        active = plan["if_active_points"]
        assert plan["play_probability"] == 0.85
        assert plan["expected_points"] == round(0.85 * active, 1)
        assert plan["expected_points"] > 7.9
        assert plan["options"]["keep"]["expected_points"] == plan["expected_points"]
        assert "news moved" in plan["expected_points_basis"]
        assert plan["recommendation"] == "keep"

    def test_unlikely_to_play_lowers_keep(self):
        e = evans(news_flags=[{"flag": "unlikely_to_play"}])
        plan = lr.plan_contingencies([e, flex()], [player("Bench WR", "WR", LATE, 6.0)], now=NOW)["at_risk"][0]
        assert plan["expected_points"] == round(0.25 * plan["if_active_points"], 1)
        assert plan["recommendation"] == "swap_now"

    def test_no_news_keeps_the_projection(self):
        plan = lr.plan_contingencies([evans(), flex()], [player("Bench WR", "WR", LATE, 6.0)], now=NOW)["at_risk"][0]
        assert plan["expected_points"] == 7.9
        assert plan["expected_points_basis"].startswith("projection")

    async def test_build_block_fills_missing_news_flags(self, monkeypatch):
        """`analyze_lineup` rows carry no news flags: the block reads them."""
        from nfl_mcp import news_signals
        monkeypatch.setattr(news_signals, "index_for", lambda db, rows: {"k": []})
        monkeypatch.setattr(news_signals, "signals_for", lambda index, name, team: (
            [{"flag": "expected_to_play", "weight": 1.0}] if name == "Mike Evans" else []))
        db = MagicMock()
        db.get_all_current_injuries = MagicMock(return_value=[])
        out = await lr.build_block([evans(), flex()], [player("Bench WR", "WR", LATE, 9.5)],
                                   league=None, rosters=None, mine=None, db=db, season=2026,
                                   week=6, now=NOW)
        plan = out["at_risk"][0]
        assert plan["play_probability"] == 0.85
        assert "news moved" in plan["expected_points_basis"]
