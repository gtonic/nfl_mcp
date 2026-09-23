"""IR slots: who belongs there under this league's rules, and who must leave.

Nothing read `reserve_slots` or `reserve_allow_*`. Eligibility is Sleeper's own
status — Sleeper enforces the rule — while the ESPN report is shown alongside.
"""
import json

import pytest

from nfl_mcp.injury_match import build_injury_index
from nfl_mcp.ir_audit import audit_roster, eligible_statuses, is_ir_eligible

# Ropeway: one slot, IR only. VLBG: two slots, also Sus/NA/DNR/COV.
ROPEWAY = {"reserve_slots": 1, "reserve_allow_out": 0, "reserve_allow_doubtful": 0,
           "reserve_allow_sus": 0, "reserve_allow_na": 0, "reserve_allow_dnr": 0,
           "reserve_allow_cov": 0}
VLBG = {"reserve_slots": 2, "reserve_allow_out": 0, "reserve_allow_doubtful": 0,
        "reserve_allow_sus": 1, "reserve_allow_na": 1, "reserve_allow_dnr": 1,
        "reserve_allow_cov": 1}


def _athlete(name, status=None, team="KC", position="WR"):
    return {"full_name": name, "team_id": team, "position": position,
            "raw": json.dumps({"injury_status": status})}


def _moves(audit):
    return [(m["action"], m["player"]) for m in audit["moves"]]


class TestEligibility:
    def test_ir_and_pup_are_always_allowed(self):
        assert eligible_statuses(ROPEWAY) == ["IR", "PUP"]

    def test_the_league_settings_add_statuses(self):
        assert eligible_statuses(VLBG) == ["IR", "PUP", "Sus", "NA", "DNR", "COV"]

    def test_out_and_doubtful_follow_the_settings(self):
        loose = {**ROPEWAY, "reserve_allow_out": 1, "reserve_allow_doubtful": 1}
        assert is_ir_eligible("Out", loose) and is_ir_eligible("Doubtful", loose)
        assert not is_ir_eligible("Out", ROPEWAY)

    def test_pup_player_in_ir_is_not_told_to_activate(self):
        """VLBG week 3: Charbonnet (PUP) sits in IR; Sleeper accepts it and
        the league allows neither Out nor Doubtful."""
        athletes = {"charb": _athlete("Zach Charbonnet", "PUP", "SEA", "RB")}
        audit = audit_roster({"players": ["charb"], "reserve": ["charb"]}, VLBG, athletes, {})
        assert _moves(audit) == []

    def test_pup_on_the_bench_can_move_to_ir(self):
        athletes = {"charb": _athlete("Zach Charbonnet", "PUP", "SEA", "RB")}
        audit = audit_roster({"players": ["charb"], "reserve": []}, ROPEWAY, athletes, {})
        assert _moves(audit) == [("move_to_ir", "Zach Charbonnet")]

    @pytest.mark.parametrize("status", ["Out", "Doubtful", "Questionable", None])
    def test_out_and_milder_are_not_eligible_when_the_league_says_so(self, status):
        assert not is_ir_eligible(status, VLBG)


class TestAuditRoster:
    def test_ropeway_week_3(self):
        """The real roster: Brown in the one slot, Mason IR on the bench."""
        athletes = {
            "brown": _athlete("A.J. Brown", "IR", "NE"),
            "mason": _athlete("Jordan Mason", "IR", "MIN", "RB"),
            "bowers": _athlete("Brock Bowers", "Out", "LV", "TE"),
            "gibbs": _athlete("Jahmyr Gibbs", None, "DET", "RB"),
        }
        roster = {"players": list(athletes), "reserve": ["brown"]}
        audit = audit_roster(roster, ROPEWAY, athletes, {})
        assert audit["reserve_free"] == 0
        assert _moves(audit) == [("ir_full", "Jordan Mason"), ("not_eligible", "Brock Bowers")]

    def test_a_free_slot_means_move_to_ir(self):
        athletes = {"mason": _athlete("Jordan Mason", "IR", "MIN", "RB")}
        audit = audit_roster({"players": ["mason"], "reserve": []}, ROPEWAY, athletes, {})
        assert _moves(audit) == [("move_to_ir", "Jordan Mason")]

    def test_a_healthy_player_in_ir_must_be_activated(self):
        athletes = {"brown": _athlete("A.J. Brown", None, "NE"),
                    "mason": _athlete("Jordan Mason", "IR", "MIN", "RB")}
        roster = {"players": ["brown", "mason"], "reserve": ["brown"]}
        audit = audit_roster(roster, ROPEWAY, athletes, {})
        # Activating Brown frees the slot, so Mason can take it.
        assert _moves(audit) == [("activate", "A.J. Brown"), ("move_to_ir", "Jordan Mason")]
        assert "will not process your adds" in audit["moves"][0]["reason"]

    def test_suspended_is_eligible_only_where_the_league_allows_it(self):
        athletes = {"x": _athlete("Suspended Guy", "Sus")}
        roster = {"players": ["x"], "reserve": []}
        assert _moves(audit_roster(roster, VLBG, athletes, {})) == [("move_to_ir", "Suspended Guy")]
        assert _moves(audit_roster(roster, ROPEWAY, athletes, {})) == [("not_eligible", "Suspended Guy")]

    def test_espn_out_alone_is_reported_but_never_moved(self):
        # Sleeper still says Questionable, so Sleeper would refuse the move.
        athletes = {"x": _athlete("Some WR", "Questionable")}
        index = build_injury_index([{"player_id": "e1", "player_name": "Some WR",
                                     "team_id": "KC", "injury_status": "Out"}])
        audit = audit_roster({"players": ["x"], "reserve": []}, VLBG, athletes, index)
        assert _moves(audit) == [("not_eligible", "Some WR")]
        assert audit["moves"][0]["report_status"] == "Out"

    def test_taxi_players_are_left_alone(self):
        athletes = {"t": _athlete("Taxi Guy", "IR")}
        audit = audit_roster({"players": ["t"], "reserve": [], "taxi": ["t"]}, VLBG, athletes, {})
        assert audit["moves"] == []

    def test_no_ir_slots_at_all(self):
        athletes = {"mason": _athlete("Jordan Mason", "IR", "MIN", "RB")}
        audit = audit_roster({"players": ["mason"], "reserve": []}, {}, athletes, {})
        assert audit["reserve_slots"] == 0
        assert _moves(audit) == [("ir_full", "Jordan Mason")]
        assert "no IR slot" in audit["moves"][0]["reason"]


class TestBriefingSurfacesThem:
    """End to end through get_weekly_briefing, which had no such test."""

    @pytest.fixture
    def briefing(self, monkeypatch, tmp_path):
        from nfl_mcp import briefing_tools, projections, sleeper_tools, weather_tools
        from nfl_mcp.database import NFLDatabase

        db = NFLDatabase(str(tmp_path / "t.db"))
        db.upsert_athletes({
            "qb": {"full_name": "Joe Burrow", "position": "QB", "team": "CIN"},
            "te1": {"full_name": "Brock Bowers", "position": "TE", "team": "LV",
                    "injury_status": "Out"},
            "te2": {"full_name": "Jake Ferguson", "position": "TE", "team": "DAL"},
            "rb": {"full_name": "Jordan Mason", "position": "RB", "team": "MIN",
                   "injury_status": "IR"},
            "wr": {"full_name": "A.J. Brown", "position": "WR", "team": "NE",
                   "injury_status": "IR"},
        })
        db.upsert_schedule_games([
            {"season": 2026, "week": 3, "team": t, "opponent": o, "is_home": h}
            for t, o, h in (("CIN", "PIT", 1), ("PIT", "CIN", 0), ("LV", "NO", 1),
                            ("NO", "LV", 0), ("DAL", "BAL", 1), ("BAL", "DAL", 0),
                            ("MIN", "TB", 1), ("TB", "MIN", 0), ("NE", "JAX", 1),
                            ("JAX", "NE", 0))
        ])
        monkeypatch.setattr(briefing_tools, "NFLDatabase", lambda *a, **k: db)
        monkeypatch.setattr("nfl_mcp.ir_audit.NFLDatabase", lambda *a, **k: db, raising=False)

        async def _league(_):
            return {"league": {"name": "Ropeway", "total_rosters": 10,
                               "scoring_settings": {"rec": 0.5},
                               "roster_positions": ["QB", "RB", "TE", "BN", "BN", "IR"],
                               "settings": ROPEWAY}}

        async def _rosters(_):
            return {"rosters": [{"roster_id": 1, "owner_id": "me",
                                 "players": ["qb", "te1", "te2", "rb", "wr"],
                                 "reserve": ["wr"], "starters": ["qb", "rb", "te2"]}]}

        async def _matchups(_, week):
            return {"matchups": [{"roster_id": 1, "matchup_id": 1,
                                  "starters": ["qb", "rb", "te2"], "players_points": {}}]}

        async def _weather(**_):
            return {"games": []}

        async def _project(players, **_):
            out = []
            for p in players:
                status = (p.get("injury") or {}).get("status")
                points = 0.0 if status in ("Out", "IR") else 10.0
                out.append({"player": p["name"], "position": p["position"], "team": p["team"],
                            "opponent": p["opponent"], "projected_points": points,
                            "floor": points / 2, "ceiling": points * 1.5})
            return {"projections": out, "vegas_active": False}

        monkeypatch.setattr(sleeper_tools, "get_league", _league)
        monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
        monkeypatch.setattr(sleeper_tools, "get_matchups", _matchups)
        monkeypatch.setattr(weather_tools, "get_weather_forecast", _weather)
        monkeypatch.setattr(projections, "project_players", _project)
        return briefing_tools.get_weekly_briefing

    @pytest.mark.asyncio
    async def test_out_players_are_listed_not_dropped_silently(self, briefing):
        out = await briefing("L", roster_id=1, week=3, season=2026)
        unavailable = {u["player"]: u for u in out["unavailable"]}
        assert set(unavailable) == {"Brock Bowers", "Jordan Mason"}
        assert unavailable["Brock Bowers"]["status"] == "Out"
        assert unavailable["Brock Bowers"]["in_recommended_lineup"] is False

    @pytest.mark.asyncio
    async def test_ir_moves_ride_along(self, briefing):
        out = await briefing("L", roster_id=1, week=3, season=2026)
        assert [(m["action"], m["player"]) for m in out["ir_moves"]] == [("ir_full", "Jordan Mason")]


@pytest.mark.asyncio
async def test_the_message_names_a_player_stuck_behind_a_full_slot(monkeypatch, tmp_path):
    from nfl_mcp import sleeper_tools
    from nfl_mcp.database import NFLDatabase
    from nfl_mcp.ir_audit import audit_ir_slots

    db = NFLDatabase(str(tmp_path / "t.db"))
    db.upsert_athletes({
        "wr": {"full_name": "A.J. Brown", "position": "WR", "team": "NE", "injury_status": "IR"},
        "rb": {"full_name": "Jordan Mason", "position": "RB", "team": "MIN", "injury_status": "IR"},
    })
    monkeypatch.setattr("nfl_mcp.database.NFLDatabase", lambda *a, **k: db)

    async def _league(_):
        return {"league": {"name": "Ropeway", "settings": ROPEWAY}}

    async def _rosters(_):
        return {"rosters": [{"roster_id": 1, "players": ["wr", "rb"], "reserve": ["wr"]}]}

    monkeypatch.setattr(sleeper_tools, "get_league", _league)
    monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
    out = await audit_ir_slots("L", roster_id=1)
    assert out["message"].startswith("No IR move possible: Jordan Mason")
