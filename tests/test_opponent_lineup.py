"""The opponent's set lineup vs his best lineup (offline).

VLBG week 5 2026: the briefing's opponent number went 53.7 -> 80.0 overnight
because his set lineup carried two Out starters (and a questionable one) that
he replaced the next day. Win probability stays on the set lineup; the best
lineup, the lineup issues and locked actual points are reported beside it.
"""
import tempfile
from pathlib import Path

import pytest

from nfl_mcp import win_probability as wp
from nfl_mcp.database import NFLDatabase
from nfl_mcp.opponent_lineup import (
    assess_opponent_lineup,
    explain,
    issues_from_players,
    merge_locked_starters,
)

pytestmark = pytest.mark.usefixtures("offline_sources")

SLOTS = ["QB", "RB", "WR", "TE", "FLEX", "DST"]
PAST = {"kickoff": "2026-01-01T00:00Z"}       # final
FUTURE = {"kickoff": "2099-01-01T00:00Z"}     # not started


def _c(pid, name, pos, team, pts, **kw):
    return {"player_id": pid, "name": name, "position": pos, "team": team,
            "projected_points": pts, "floor": pts * 0.6, "ceiling": pts * 1.4, **kw}


def _cands():
    return {
        "qb_out": _c("qb_out", "Lamar", "QB", "BAL", 0.0, injury_status="Out"),
        "rb_out": _c("rb_out", "Saquon", "RB", "PHI", 0.0, injury_status="Out"),
        "wr": _c("wr", "Jefferson", "WR", "MIN", 10.0, injury_status="Questionable"),
        "te": _c("te", "Ferguson", "TE", "DAL", 7.6),
        "flex_d": _c("flex_d", "Doubtful RB", "RB", "CHI", 1.0, injury_status="Doubtful"),
        # bench
        "qb2": _c("qb2", "Rodgers", "QB", "PIT", 17.8),
        "rb2": _c("rb2", "Pollard", "RB", "TEN", 9.0),
        "wr2": _c("wr2", "Coleman", "WR", "BUF", 8.0),
        "wr_played": _c("wr_played", "Played WR", "WR", "TB", 12.0),
    }


GAMES = {"DAL": PAST, "TB": PAST, **dict.fromkeys(("BAL", "PHI", "MIN", "CHI", "PIT", "TEN", "BUF"), FUTURE)}


class TestAssess:
    def test_set_vs_best_issues_and_locked_actuals(self):
        view = assess_opponent_lineup(
            SLOTS, ["qb_out", "rb_out", "wr", "te", "flex_d", "0"], _cands(),
            ["qb2", "rb2", "wr2", "wr_played"], {}, GAMES, {"te": 3.4, "wr_played": 20.0})
        # Ferguson's game is final: his 3.4, not his 7.6 projection.
        assert view["set_lineup_points"] == pytest.approx(0 + 0 + 10.0 + 3.4 + 1.0)
        assert view["locked"] == [{"player": "Ferguson", "slot": "TE", "actual": 3.4,
                                   "counted": 3.4}]
        # Best: Rodgers at QB, Pollard at RB, Coleman at FLEX; Ferguson stays
        # locked; the WR whose game already started on the bench cannot come in.
        assert view["best_lineup_points"] == pytest.approx(17.8 + 9.0 + 10.0 + 3.4 + 8.0)
        assert view["points_at_risk"] == pytest.approx(view["best_lineup_points"]
                                                       - view["set_lineup_points"], abs=0.1)
        issues = [(i["slot"], i["player"], i["issue"]) for i in view["lineup_issues"]]
        assert issues == [("DST", None, "empty"), ("QB", "Lamar", "out"),
                          ("RB", "Saquon", "out"), ("FLEX", "Doubtful RB", "doubtful")]
        starts = {s["player"] for s in view["best_lineup_changes"]["start"]}
        assert starts == {"Rodgers", "Pollard", "Coleman"}
        assert "Played WR" not in starts
        note = explain(view)
        assert "Lamar (QB: out)" in note and f"up to {view['points_at_risk']}" in note

    def test_bye_and_unprojectable_starters_are_zero_and_named(self):
        labels = {"kc": {"name": "Bye WR", "position": "WR", "team": "KC", "reason": "bye"},
                  "x": {"name": "Unknown", "position": "RB", "team": None,
                        "reason": "unprojectable"}}
        view = assess_opponent_lineup(["WR", "RB"], ["kc", "x"], {}, [], labels, {}, {})
        assert view["set_lineup_points"] == 0.0
        assert [i["issue"] for i in view["lineup_issues"]] == ["bye", "unprojected"]
        assert {b["player"] for b in view["best_lineup_changes"]["bench"]} == {"Bye WR",
                                                                               "Unknown"}

    def test_unprojected_locked_starter_counts_his_actual(self):
        labels = {"fer": {"name": "Ferguson", "position": "TE", "team": "DAL",
                          "reason": "unprojectable"}}
        view = assess_opponent_lineup(["TE"], ["fer"], {}, [], labels, {"DAL": PAST},
                                      {"fer": 3.4})
        assert view["set_lineup_points"] == 3.4 and view["lineup_issues"] == []

    def test_unknown_bench_means_best_equals_set(self):
        view = assess_opponent_lineup(["QB"], ["qb_out"], _cands(), None, {}, GAMES, {})
        assert view["best_lineup_points"] == view["set_lineup_points"] == 0.0
        assert view["bench_considered"] is False

    def test_clean_lineup(self):
        view = assess_opponent_lineup(["QB"], ["qb2"], _cands(), ["qb_out"], {}, GAMES, {})
        assert view["lineup_issues"] == [] and view["points_at_risk"] == 0.0
        assert "No empty, bye or injured starters" in explain(view)


class TestMergeLocked:
    def test_a_locked_starter_dropped_after_his_game_stays(self):
        teams = {"fer": "DAL", "x": "BUF"}
        out, taken = merge_locked_starters(["qb", "0"], ["qb", "fer"], teams.get, {"DAL": PAST})
        assert out == ["qb", "fer"] and taken == ["fer"]

    def test_an_unstarted_stale_matchup_starter_is_not_taken(self):
        out, taken = merge_locked_starters(["qb", "new"], ["qb", "x"], {"x": "BUF"}.get,
                                           {"BUF": FUTURE})
        assert out == ["qb", "new"] and taken == []


class TestWinProbability:
    def test_best_lineup_figures_beside_the_set_basis(self):
        mine = [_c("m1", "Mine QB", "QB", "BUF", 20.0), _c("m2", "Mine RB", "RB", "BUF", 15.0)]
        set_ = [_c("o1", "Opp QB", "QB", "NYJ", 0.0, injury_status="Out"),
                _c("o2", "Opp RB", "RB", "NYJ", 12.0)]
        best = [_c("o3", "Opp QB2", "QB", "NYJ", 18.0), set_[1]]
        res = wp.optimize_win_probability(mine, set_, {"QB": 1, "RB": 1},
                                          opponent_best_players=best,
                                          opponent_basis="set_lineup")
        assert res["opponent_projection_basis"] == "set_lineup"
        assert res["opponent_projected_points"] == 12.0
        assert res["opponent_best_lineup_points"] == 30.0
        assert res["win_probability_if_opponent_fixes_lineup"] < res["win_probability"]
        plain = wp.optimize_win_probability(mine, set_, {"QB": 1, "RB": 1})
        assert plain["opponent_projection_basis"] == "as_given"
        assert "opponent_best_lineup_points" not in plain

    async def test_tool_settles_opponent_kickoffs_and_reads_his_bench(self, monkeypatch):
        async def _resolve(season, week):
            return 2026, 5, "test"
        monkeypatch.setattr("nfl_mcp.week_context.resolve_season_week", _resolve)
        monkeypatch.setattr(wp, "week_games", lambda *a, **k: {"DAL": PAST, "NYJ": FUTURE,
                                                               "BUF": FUTURE})
        mine = [_c("m1", "Mine QB", "QB", "BUF", 20.0), _c("m2", "Mine TE", "TE", "BUF", 8.0)]
        opp = [_c("o1", "Opp QB", "QB", "NYJ", 0.0, injury_status="Out"),
               {**_c("o2", "Opp TE", "TE", "DAL", 7.6), "actual_points": 3.4}]
        bench = [_c("o3", "Opp QB2", "QB", "NYJ", 18.0),
                 _c("o4", "Played TE", "TE", "DAL", 9.0)]
        res = await wp.get_win_probability_lineup(
            your_players=mine, opponent_players=opp, slots={"QB": 1, "TE": 1},
            opponent_bench=bench, risk_mode="neutral")
        assert res["success"]
        assert res["opponent_projected_points"] == 3.4  # 0 + Ferguson's actual
        assert res["opponent_best_lineup_points"] == 21.4  # QB2 in, played TE cannot
        assert res["opponent_locked_players"] == [{"player": "Opp TE", "actual": 3.4,
                                                   "counted": 3.4}]
        assert [i["issue"] for i in res["opponent_lineup_issues"]] == ["out"]

    async def test_tool_warns_when_a_started_opponent_has_no_actual(self, monkeypatch):
        async def _resolve(season, week):
            return 2026, 5, "test"
        monkeypatch.setattr("nfl_mcp.week_context.resolve_season_week", _resolve)
        monkeypatch.setattr(wp, "week_games", lambda *a, **k: {"DAL": PAST})
        res = await wp.get_win_probability_lineup(
            your_players=[_c("m1", "Mine QB", "QB", "BUF", 20.0)],
            opponent_players=[_c("o2", "Opp TE", "TE", "DAL", 7.6)],
            slots={"QB": 1}, risk_mode="neutral")
        assert res["opponent_projected_points"] == 7.6
        assert "without actual_points" in res["warnings"][0]

    def test_issues_from_a_plain_list(self):
        out = issues_from_players([{"name": "A", "on_bye": True},
                                   {"name": "B", "projected_points": 0.0},
                                   {"name": "C", "projected_points": 5.0,
                                    "gameday_status": "inactive"},
                                   {"name": "D", "projected_points": 9.0}])
        assert [(i["player"], i["issue"]) for i in out] == [
            ("A", "bye"), ("C", "inactive"), ("B", "zero_projection")]


# --------------------------------------------------------------------------
# Briefing
# --------------------------------------------------------------------------

@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        d = NFLDatabase(str(Path(tmp) / "opp.db"))
        d.upsert_athletes({
            "mq": {"full_name": "My QB", "position": "QB", "team": "BUF"},
            "mr": {"full_name": "My RB", "position": "RB", "team": "BUF"},
            "oq": {"full_name": "Out QB", "position": "QB", "team": "NYJ"},
            "oq2": {"full_name": "Backup QB", "position": "QB", "team": "NYJ"},
            "or": {"full_name": "Opp RB", "position": "RB", "team": "NYJ"},
            "fer": {"full_name": "Jake Ferguson", "position": "TE", "team": "DAL"},
        })
        rows = []
        for team, opp, kick in (("BUF", "MIA", "2099-01-01T00:00Z"),
                                ("MIA", "BUF", "2099-01-01T00:00Z"),
                                ("NYJ", "NE", "2099-01-01T00:00Z"),
                                ("NE", "NYJ", "2099-01-01T00:00Z"),
                                ("DAL", "TB", "2026-01-01T00:15Z"),
                                ("TB", "DAL", "2026-01-01T00:15Z")):
            rows.append({"season": 2026, "week": 5, "team": team, "opponent": opp,
                         "is_home": 1, "kickoff": kick})
        d.upsert_schedule_games(rows)
        yield d
        d.close()


async def test_briefing_reports_set_and_best_opponent_lineups(monkeypatch, db):
    from nfl_mcp import briefing_tools, projections, sleeper_tools

    monkeypatch.setattr(briefing_tools, "get_shared_db", lambda *a, **k: db)

    async def _league(_):
        return {"league": {"total_rosters": 10, "scoring_settings": {"rec": 0.5},
                           "roster_positions": ["QB", "RB", "TE", "BN", "BN"],
                           "settings": {}}}

    async def _rosters(_):
        return {"rosters": [
            {"roster_id": 1, "players": ["mq", "mr"], "starters": ["mq", "mr", "0"]},
            # Ferguson was dropped after his Thursday game: the roster shows
            # "0" at TE, the matchup still starts (and scores) him.
            {"roster_id": 2, "players": ["oq", "oq2", "or"], "starters": ["oq", "or", "0"]},
        ]}

    async def _matchups(_, week):
        return {"matchups": [
            {"roster_id": 1, "matchup_id": 1, "starters": ["mq", "mr", "0"],
             "players_points": {}},
            {"roster_id": 2, "matchup_id": 1, "starters": ["oq", "or", "fer"],
             "players": ["oq", "oq2", "or", "fer"], "players_points": {"fer": 3.4}},
        ]}

    async def _project(players, **_):
        out = []
        for p in players:
            out_qb = p["name"] == "Out QB"
            out.append({"player": p["name"], "position": p["position"], "team": p["team"],
                        "opponent": p["opponent"], "projected_points": 0.0 if out_qb else 10.0,
                        "floor": 0.0 if out_qb else 6.0, "ceiling": 0.0 if out_qb else 14.0,
                        **({"injury_status": "Out"} if out_qb else {})})
        return {"projections": out}

    monkeypatch.setattr(sleeper_tools, "get_league", _league)
    monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
    monkeypatch.setattr(sleeper_tools, "get_matchups", _matchups)
    monkeypatch.setattr(projections, "project_players", _project)

    out = await briefing_tools.get_weekly_briefing("L", roster_id=1, week=5, season=2026,
                                                   risk_mode="neutral")
    assert out["success"] is not False
    assert out["opponent_projection_basis"] == "set_lineup"
    # Out QB 0 + Opp RB 10 + Ferguson's actual 3.4 (the locked, dropped TE).
    assert out["opponent_set_lineup_points"] == 13.4
    assert out["opponent_projected_points"] == 13.4
    # Fixed: the backup QB (10) in for the Out starter.
    assert out["opponent_best_lineup_points"] == 23.4
    assert out["opponent_points_at_risk"] == 10.0
    assert out["win_probability_if_opponent_fixes_lineup"] < out["win_probability"]
    assert [(i["player"], i["issue"]) for i in out["opponent_lineup_issues"]] == [
        ("Out QB", "out")]
    assert out["opponent_locked_players"][0]["player"] == "Jake Ferguson"
    assert out["opponent_locked_players"][0]["counted"] == 3.4
    assert "Out QB" in out["opponent_projection_note"]
