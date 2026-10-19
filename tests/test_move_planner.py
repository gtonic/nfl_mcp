"""plan_roster_moves: a multi-step roster plan checked before it is entered.

Live failures behind it: a claim entered without a drop on a full roster
("Unfortunately, your roster will have too many players after this
transaction"), and a claim the assistant assumed was in that never was.
Everything here runs on a synthetic league: a 9-player active roster (full),
one IR slot that only takes IR/PUP, rolling waivers on Wednesday.
"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import move_planner, tool_registry
from nfl_mcp.move_planner import (
    PlanContext,
    diff_planned_vs_happened,
    failed_claims,
    normalize_move,
    parse_goals,
    parse_when,
    simulate,
)

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)  # Friday of week 6, after TNF

LEAGUE = {
    "name": "Synthetic",
    "total_rosters": 10,
    "roster_positions": ["QB", "RB", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"],
    "settings": {"reserve_slots": 1, "waiver_type": 0, "waiver_day_of_week": 2,
                 "waiver_clear_days": 2, "daily_waivers_hour": 0, "playoff_week_start": 15},
}

TEAMS = ["CIN", "CAR", "SEA", "KC", "NE", "PHI", "LV", "IND", "LAC", "DEN", "ATL", "MIA"]


def _games():
    games = {}
    for w in range(5, 18):
        day = 11 + 7 * (w - 5)  # Sundays: Oct 11, 18, 25, ...
        week = {}
        for team in TEAMS:
            if w == 7 and team in ("CIN", "LAC"):
                continue  # bye
            month, d = (10, day) if day <= 31 else (11, day - 31) if day <= 61 else (12, day - 61)
            kick = f"2026-{month:02d}-{d:02d}T17:00Z"
            if w == 6 and team in ("SEA", "DEN"):
                kick = "2026-10-16T00:15Z"  # Thursday night: already started
            week[team] = {"kickoff": kick}
        games[w] = week
    return games


def _entry(pid, pos, team, rate, byes=(), out=()):
    weekly = {w: (0.0 if w in byes or w in out else rate) for w in range(6, 18)}
    return {"player_id": pid, "player": pid.upper(), "position": pos, "team": team,
            "per_game": rate, "weekly_points": weekly, "bye_weeks": list(byes),
            "injury_weeks": list(out), "injury_window": None, "ros_points": rate * 10}


PLAYERS = {
    # pid: (name, position, team, rate, byes, sleeper status)
    "q1": ("Joe Burrow", "QB", "CIN", 20, (7,), None),
    "q2": ("Bryce Young", "QB", "CAR", 14, (), None),
    "r1": ("Emanuel Wilson", "RB", "SEA", 9, (), None),
    "r2": ("Kenneth Walker", "RB", "KC", 15, (), None),
    "w1": ("Romeo Doubs", "WR", "NE", 8, (), None),
    "w2": ("DeVonta Smith", "WR", "PHI", 11, (), "Out"),
    "t1": ("Brock Bowers", "TE", "LV", 13, (), None),
    "k1": ("Spencer Shrader", "K", "IND", 8, (), None),
    "KC": ("KC", "DEF", "KC", 7, (), None),
    "ir1": ("A.J. Brown", "WR", "NE", 12, (), "IR"),
    "fa1": ("Keaton Mitchell", "RB", "LAC", 10, (7,), None),
    "fa2": ("Jadarian Price", "RB", "DEN", 9, (), None),
    "o1": ("Jeremiyah Love", "RB", "ATL", 16, (), None),
}


def _ctx(roster_players=None, reserve=("ir1",), statuses=None, now=NOW, week=None, starters=None):
    statuses = statuses or {}
    info, ros = {}, {}
    for pid, (name, pos, team, rate, byes, status) in PLAYERS.items():
        info[pid] = {"name": name, "position": pos, "team": team, "status": statuses.get(pid, status)}
        ros[pid] = {**_entry(pid, pos, team, rate, byes), "player": name}
    players = list(roster_players or ["q1", "q2", "r1", "r2", "w1", "w2", "t1", "k1", "KC"])
    mine = {"roster_id": 1, "owner_id": "u1", "players": players + list(reserve),
            "reserve": list(reserve), "taxi": [],
            # QB, RB, WR, TE, FLEX, K, DEF — Wilson (Thursday, locked) at RB.
            "starters": list(starters or ["q2", "r1", "w1", "t1", "r2", "k1", "KC"]),
            "settings": {"waiver_position": 10}}
    other = {"roster_id": 2, "owner_id": "u2", "players": ["o1"], "reserve": [], "taxi": []}
    return PlanContext(league=LEAGUE, roster=mine, rosters=[mine, other], info=info, ros=ros,
                       games=_games(), now=now, current_week=6, week=week, season=2026,
                       owners={"u2": "Rival"})


def _moves(*raw):
    out = []
    for r in raw:
        move, err = normalize_move(r)
        assert err is None, err
        out.append(move)
    return out


def _step(plan, n):
    return next(s for s in plan["steps"] if s["step"] == n)


class TestRosterSize:
    def test_claim_without_drop_on_a_full_roster_is_invalid(self):
        plan = simulate(_ctx(), _moves({"action": "claim", "player": "Jadarian Price"}))
        step = _step(plan, 1)
        assert step["valid"] is False
        assert "too many players" in step["reason"]
        assert "drop" in step["fix"].lower()
        # It takes effect at the waiver run, not now.
        assert step["week"] == 7 and step["effective_at_local"].startswith("2026-10-21T09:00")

    def test_claim_with_a_drop_is_valid_and_lands_at_the_run(self):
        plan = simulate(_ctx(), _moves({"action": "claim", "player": "Jadarian Price", "drop": "Bryce Young"}))
        step = _step(plan, 1)
        assert step["valid"] is True, step.get("reason")
        assert step["kind"] == "waiver_claim"
        item = plan["checklist"][0]
        assert "before the waiver run" in item["do"] and "10th of 10" in item["do"]
        assert "rolling waivers" in item["do"]
        assert any("claims are invisible until processed" in v for v in plan["verify_after"])

    def test_free_add_with_a_drop_frees_nothing_extra(self):
        plan = simulate(_ctx(), _moves("add Keaton Mitchell drop Romeo Doubs"))
        assert _step(plan, 1)["valid"] is True
        assert plan["roster"]["counts"].startswith("9/9")


class TestIR:
    def test_ir_move_of_a_status_the_league_does_not_allow_is_invalid(self):
        # Out is not IR-eligible here (only IR / PUP).
        plan = simulate(_ctx(reserve=()), _moves({"action": "ir_move", "player": "DeVonta Smith"}))
        step = _step(plan, 1)
        assert step["valid"] is False
        assert "Out" in step["reason"] and "IR, PUP" in step["reason"]

    def test_ir_move_with_the_slot_taken_is_invalid(self):
        plan = simulate(_ctx(statuses={"w2": "IR"}), _moves({"action": "ir_move", "player": "DeVonta Smith"}))
        assert "IR slot(s) are taken" in _step(plan, 1)["reason"]

    def test_ir_move_opens_the_spot_a_claim_needs(self):
        plan = simulate(_ctx(reserve=(), statuses={"w2": "IR"}), _moves(
            {"action": "ir_move", "player": "DeVonta Smith"},
            {"action": "claim", "player": "Jadarian Price"}))
        assert plan["valid"] is True, plan["steps"]

    def test_ineligible_player_in_ir_blocks_adds(self):
        plan = simulate(_ctx(statuses={"ir1": None}),
                        _moves("add Keaton Mitchell drop Romeo Doubs"))
        step = _step(plan, 1)
        assert step["valid"] is False and "sits in IR" in step["reason"]
        assert "Activate A.J. Brown" in step["fix"]

    def test_activate_on_a_full_roster_needs_a_drop(self):
        plan = simulate(_ctx(), _moves({"action": "activate", "player": "A.J. Brown"}))
        assert "free roster spot" in _step(plan, 1)["reason"]
        plan = simulate(_ctx(), _moves({"action": "activate", "player": "A.J. Brown",
                                        "drop": "Bryce Young"}))
        assert _step(plan, 1)["valid"] is True


class TestLocksAndWaivers:
    def test_drop_of_a_player_whose_game_started_is_invalid(self):
        plan = simulate(_ctx(), _moves({"action": "drop", "player": "Emanuel Wilson"}))
        step = _step(plan, 1)
        assert step["valid"] is False
        assert "kicked off" in step["reason"] and "cannot be dropped" in step["reason"]

    def test_the_same_drop_after_the_week_rolls_over_is_valid(self):
        plan = simulate(_ctx(), _moves({"action": "drop", "player": "Emanuel Wilson",
                                        "when": "after week 6"}))
        step = _step(plan, 1)
        assert step["valid"] is True and step["week"] == 7

    def test_drop_at_waivers_is_evaluated_after_the_rollover(self):
        # Live 2026-10-19 (Monday): "drop KC at waivers" resolved to the daily
        # run before the Wednesday rollover and was reported locked.
        monday = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)
        plan = simulate(_ctx(now=monday, week=7), _moves({"action": "drop", "player": "KC",
                                                          "when": "waivers"}))
        step = _step(plan, 1)
        assert step["valid"] is True, step.get("reason")
        assert step["week"] == 7 and step["effective_at_local"].startswith("2026-10-21T09:00")
        assert plan["checklist"][0]["do"].startswith(
            "Drop KC after the week rolls over (~Wed 21 Oct 09:00)")

    def test_drop_at_waivers_of_an_unlocked_player_keeps_the_next_run(self):
        plan = simulate(_ctx(), _moves({"action": "drop", "player": "Romeo Doubs",
                                        "when": "waivers"}))
        step = _step(plan, 1)
        assert step["valid"] is True and step["week"] == 6
        assert "rolls over" not in plan["checklist"][0]["do"]

    def test_claim_after_a_lock_is_in_time_for_next_weeks_kickoff(self):
        # Price's week-6 game locked him until Wednesday's run; the claim lands
        # then, days before his week-7 kickoff -- no "only helps from next week".
        monday = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)
        for when in (None, "waivers", "now"):
            move = {"action": "claim", "player": "Jadarian Price", "drop": "Romeo Doubs"}
            plan = simulate(_ctx(now=monday, week=7), _moves({**move, "when": when} if when else move))
            step = _step(plan, 1)
            assert step["valid"] is True and step["kind"] == "waiver_claim"
            assert step["waiver_timing"]["in_time_for_kickoff"] is True
            assert not any("only helps from next week" in n for n in step["notes"])

    def test_claim_landing_after_this_weeks_kickoff_still_says_so(self):
        # Dropped Saturday: clears Monday, after his Sunday game, same week.
        saturday = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
        ws = _ctx(now=saturday).waiver_state("fa1", saturday, saturday)
        assert ws["on_waivers"] is True and ws["in_time_for_kickoff"] is False

    def test_add_of_a_player_on_waivers_is_invalid_and_points_to_a_claim(self):
        plan = simulate(_ctx(), _moves("add Jadarian Price drop Romeo Doubs"))
        step = _step(plan, 1)
        assert step["valid"] is False and "on waivers" in step["reason"]
        assert "claim" in step["fix"].lower()

    def test_claim_of_a_free_agent_is_treated_as_an_add(self):
        plan = simulate(_ctx(), _moves({"action": "claim", "player": "Keaton Mitchell",
                                        "drop": "Romeo Doubs"}))
        step = _step(plan, 1)
        assert step["valid"] is True and step["kind"] == "instant_add"
        assert any("free agent" in n for n in step["notes"])

    def test_rostered_elsewhere_is_invalid(self):
        plan = simulate(_ctx(), _moves({"action": "claim", "player": "Jeremiyah Love",
                                        "drop": "Romeo Doubs"}))
        assert "rostered by roster 2 (Rival)" in _step(plan, 1)["reason"]

    def test_lineup_move_of_a_locked_player_is_invalid(self):
        plan = simulate(_ctx(), _moves({"action": "bench", "player": "Emanuel Wilson"}))
        assert "locked" in _step(plan, 1)["reason"]

    def test_start_in_a_slot_held_by_a_locked_player_is_invalid(self):
        ctx = _ctx(starters=["q2", "r2", "w1", "t1", "r1", "k1", "KC"])  # Wilson at FLEX
        plan = simulate(ctx, _moves("add Keaton Mitchell drop Romeo Doubs",
                                    "start Keaton Mitchell at FLEX"))
        step = _step(plan, 2)
        assert step["valid"] is False and "Emanuel Wilson" in step["reason"]

    def test_a_start_that_depends_on_an_invalid_add_says_so(self):
        plan = simulate(_ctx(), _moves("add Keaton Mitchell drop Tyreek Hill",
                                       "start Keaton Mitchell at FLEX"))
        assert _step(plan, 1)["valid"] is False
        second = _step(plan, 2)
        assert second["depends_on"] == 1 and "depends on step 1" in second["reason"]

    def test_forced_start_is_seated_in_the_lineup(self):
        plan = simulate(_ctx(), _moves("add Keaton Mitchell drop Romeo Doubs",
                                       "start Keaton Mitchell at FLEX"))
        week6 = next(w for w in plan["weeks"] if w["week"] == 6)
        flex = next(r for r in week6["lineup"] if r["slot"] == "FLEX")
        assert flex["player"] == "Keaton Mitchell" and flex["fixed"] == "forced"
        # The locked Thursday starter keeps his slot.
        assert any(r["player"] == "Emanuel Wilson" and r.get("fixed") == "locked" for r in week6["lineup"])
        # Checklist: by when, with the kickoff as deadline.
        assert plan["checklist"][0]["deadline"].startswith("2026-10-18")


class TestByesAndBackups:
    def test_bye_crunch_warning_when_the_plan_empties_a_slot(self):
        # Drop the only K for an RB: week 6 and 7 have nobody to kick.
        plan = simulate(_ctx(), _moves("add Keaton Mitchell drop Spencer Shrader"))
        assert any("empty starting slot" in w and "K" in w for w in plan["warnings"])
        week6 = next(w for w in plan["weeks"] if w["week"] == 6)
        assert [h["slot"] for h in week6["empty_slots"]] == ["K"]

    def test_lost_backup_qb_names_the_bye_week(self):
        plan = simulate(_ctx(), _moves("add Keaton Mitchell drop Bryce Young"))
        lost = plan["lost_backups"]
        assert len(lost) == 1 and lost[0]["position"] == "QB"
        assert lost[0]["uncovered_weeks"] == [7]
        assert "Joe Burrow" in lost[0]["message"] and "week 7" in lost[0]["message"]
        assert any(w.startswith("Lost backup at QB") for w in plan["warnings"])

    def test_dropping_a_backup_after_his_bye_has_passed_costs_no_week(self):
        plan = simulate(_ctx(), _moves({"action": "claim", "player": "Jadarian Price",
                                        "drop": "Bryce Young", "when": "after week 7"}))
        lost = plan["lost_backups"]
        assert lost and lost[0]["uncovered_weeks"] == []
        assert "injury" in lost[0]["message"]


class TestParsing:
    def test_string_moves(self):
        move, _ = normalize_move("add Keaton Mitchell drop Tyreek Hill")
        assert move["action"] == "add" and move["player"] == "Keaton Mitchell"
        assert move["drop"] == "Tyreek Hill"
        move, _ = normalize_move("start Mitchell at FLEX")
        assert move["slot"] == "FLEX"
        assert normalize_move({"action": "trade", "player": "X"})[1].startswith("unknown action")

    def test_when(self):
        ctx = _ctx()
        assert parse_when(ctx, None)[0] == NOW
        t, err = parse_when(ctx, "after week 6")
        assert err is None and t == datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
        t, _ = parse_when(ctx, "before Wed 09:00")
        assert t.isoformat().startswith("2026-10-21T06:59")
        assert parse_when(ctx, "whenever")[1]

    def test_goals(self):
        goals = parse_goals(["fill week-7 FLEX hole", "DEF for bye", "TE backup"], 6)
        assert [(g["position"], g["week"], g["kind"]) for g in goals] == [
            ("FLEX", 7, "fill"), ("DEF", 6, "fill"), ("TE", 6, "backup")]


class TestFailedClaimsAndVerify:
    TXS = [
        {"type": "waiver", "status": "failed", "transaction_id": "t1", "roster_ids": [1],
         "adds": {"fa2": 1}, "drops": None, "status_updated": int(NOW.timestamp() * 1000) - 3600_000,
         "metadata": {"notes": "Unfortunately, your roster will have too many players after this transaction."}},
        {"type": "waiver", "status": "failed", "transaction_id": "t2", "roster_ids": [1],
         "adds": {"o1": 1}, "drops": None, "status_updated": int(NOW.timestamp() * 1000) - 7200_000,
         "metadata": {"notes": "This player was claimed by another owner."}},
        {"type": "free_agent", "status": "complete", "transaction_id": "t3", "roster_ids": [1],
         "adds": {"fa1": 1}, "drops": {"w1": 1}, "status_updated": int(NOW.timestamp() * 1000)},
    ]

    def test_failed_claims_carry_sleepers_note(self):
        rows = failed_claims(self.TXS, {"fa2": "Jadarian Price"})
        assert [r["reason_code"] for r in rows] == ["roster_full", "claimed_by_other"]
        assert rows[0]["wanted"] == [{"player_id": "fa2", "name": "Jadarian Price"}]
        assert "too many players" in rows[0]["note"]

    def test_planned_vs_happened(self):
        ctx = _ctx()
        roster_now = {**ctx.roster, "players": [*ctx.roster["players"], "fa1"]}
        roster_now["players"].remove("w1")
        moves = _moves("add Keaton Mitchell drop Romeo Doubs",
                       {"action": "claim", "player": "Jadarian Price"},
                       {"action": "claim", "player": "Jeremiyah Love"},
                       {"action": "drop", "player": "Kenneth Walker"})
        diff = diff_planned_vs_happened(ctx, moves, roster_now, self.TXS)
        status = [m["status"] for m in diff["moves"]]
        assert status == ["done", "failed", "failed", "not_happened"]
        assert diff["moves"][1]["reason_code"] == "roster_full"
        assert diff["summary"] == {"done": 1, "failed": 2, "not_happened": 1}

    def test_claim_never_entered_or_pending_is_not_reported_done(self):
        ctx = _ctx()
        diff = diff_planned_vs_happened(ctx, _moves({"action": "claim", "player": "Jadarian Price"}),
                                        ctx.roster, [])
        assert diff["moves"][0]["status"] == "pending_or_not_entered"
        ctx.held["o1"] = ctx.rosters[1]
        diff = diff_planned_vs_happened(ctx, _moves({"action": "claim", "player": "Jeremiyah Love"}),
                                        ctx.roster, [])
        assert diff["moves"][0]["status"] == "taken_by_other"


@pytest.mark.asyncio
async def test_tool_runs_the_plan_on_the_loaded_context():
    ctx = _ctx()
    load = AsyncMock(return_value=(ctx, None, {"stale": False, "snapshot_age_seconds": None}))
    with patch.object(move_planner, "load_context", load):
        res = await tool_registry.plan_roster_moves(
            "123", 1, moves=[{"action": "claim", "player": "Jadarian Price"}])
    assert res["success"] is True and res["valid"] is False
    assert res["message"].startswith("1 of 1 step(s) invalid")
    assert res["roster_limits"]["active"] == 9


@pytest.mark.asyncio
async def test_tool_rejects_unknown_actions():
    res = await tool_registry.plan_roster_moves("123", 1, moves=[{"action": "trade", "player": "X"}])
    assert res["success"] is False and "unknown action" in res["error"]


@pytest.mark.asyncio
async def test_get_transactions_lists_failed_claims_with_the_note():
    resp = {"success": True, "transactions": TestFailedClaimsAndVerify.TXS, "week": 6, "count": 3}
    with patch("nfl_mcp.sleeper_tools.get_transactions", AsyncMock(return_value=resp)):
        res = await tool_registry.get_transactions("123", week=6)
    assert res["failed_claims_count"] == 2
    assert res["failed_claims"][0]["reason_code"] == "roster_full"


def test_registered_in_season_profile():
    names = {t.__name__ for t in tool_registry.get_all_tools("season")}
    assert "plan_roster_moves" in names
