"""Model follow-ups: name resolution (ROS + project_players) and the backup QB."""
import json
import types
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import lineup_tools, projections, qb_coupling, ros

# The athlete cache as it stood on 2026-10-08: Cleveland's linebacker sorts
# first, and the old ROS lookup took the first exact match with a team.
JJ_LB = {"id": "13524", "full_name": "Justin Jefferson", "position": "LB", "team_id": "CLE",
         "status": "Active", "raw": json.dumps({"search_rank": 9999999})}
JJ_WR = {"id": "6794", "full_name": "Justin Jefferson", "position": "WR", "team_id": "MIN",
         "status": "Active", "raw": json.dumps({"search_rank": 12})}
MW_A = {"id": "100", "full_name": "Mike Williams", "position": "WR", "team_id": "NYJ",
        "status": "Active", "raw": json.dumps({"search_rank": 300})}
MW_B = {"id": "200", "full_name": "Mike Williams", "position": "WR", "team_id": "PIT",
        "status": "Active", "raw": json.dumps({"search_rank": 150})}
FA = {"id": "300", "full_name": "Free Agent", "position": "WR", "team_id": None,
      "status": "Active", "raw": None}
ROWS = [JJ_LB, JJ_WR, MW_A, MW_B, FA]


class _DB:
    def search_athletes_by_name(self, name, limit=10):
        return [r for r in ROWS if name.lower() in r["full_name"].lower()][:limit]

    def get_athletes_by_ids(self, ids):
        return {r["id"]: r for r in ROWS if r["id"] in ids}


class TestNameResolution:
    def test_fantasy_player_beats_a_namesake_linebacker(self):
        ranked, ambiguous = lineup_tools.name_candidates(_DB(), "Justin Jefferson")
        assert ranked[0]["id"] == "6794" and not ambiguous
        who = lineup_tools.resolve_player(_DB(), "Justin Jefferson")
        assert (who["team"], who["position"], who["player_id"]) == ("MIN", "WR", "6794")
        assert "ambiguous" not in who

    def test_two_fantasy_players_are_ambiguous_market_rank_wins(self):
        who = lineup_tools.resolve_player(_DB(), "Mike Williams")
        assert who["team"] == "PIT" and who["ambiguous"] is True
        assert [c["team"] for c in who["candidates"]] == ["PIT", "NYJ"]

    def test_a_team_hint_settles_it(self):
        who = lineup_tools.resolve_player(_DB(), "Mike Williams", team="NYJ")
        assert who["player_id"] == "100" and "ambiguous" not in who

    def test_search_rank_parse(self):
        assert lineup_tools._search_rank({"raw": {"search_rank": 7}}) == 7
        assert lineup_tools._search_rank({"raw": "not json"}) == lineup_tools.UNRANKED_SEARCH_RANK
        assert lineup_tools._search_rank({}) == lineup_tools.UNRANKED_SEARCH_RANK


class TestRosPlayerNames:
    @pytest.mark.asyncio
    async def test_named_star_is_the_receiver_and_ranked_against_the_league(self, monkeypatch):
        from nfl_mcp import sleeper_tools

        seen = {}

        def _entry(pid, name, pos, per_game):
            return {"player": name, "player_id": pid, "position": pos, "team": "MIN",
                    "per_game": per_game, "ros_points": per_game * 11, "playoff_points": 0.0,
                    "total_points": per_game * 11, "weekly_points": {},
                    "market_position_rank": 7, "news_flags": []}

        pool = {"6794": _entry("6794", "Justin Jefferson", "WR", 13.1)}
        for i in range(15):
            pool[f"p{i}"] = _entry(f"p{i}", f"Wr {i}", "WR", 6.0 + i)

        async def _league(_):
            return {"league": {"name": "T", "settings": {}}}

        async def _rosters(_):
            return {"rosters": [{"roster_id": 1, "players": [p for p in pool if p != "6794"]},
                                {"roster_id": 2, "players": ["6794"]}]}

        async def _ros_for_ids(ids, **kw):
            seen["ids"] = list(ids)
            return ({i: dict(pool[i]) for i in ids if i in pool},
                    {"windows": {"regular": [5, 6], "playoff": []},
                     "schedule_unknown_weeks": [], "matchups_active": False,
                     "scoring_used": {}})

        monkeypatch.setattr(sleeper_tools, "get_league", _league)
        monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
        monkeypatch.setattr(ros, "ros_for_ids", _ros_for_ids)
        out = await ros.get_ros_projections(
            "L", player_names=["Justin Jefferson", "Mike Williams"], season=2026, week=5,
            db=_DB())
        assert "6794" in seen["ids"] and "13524" not in seen["ids"]
        assert "200" in seen["ids"] and len(set(seen["ids"])) == len(pool) + 1
        jj = next(p for p in out["players"] if p["player_id"] == "6794")
        assert jj["position"] == "WR"
        # Ranked against the league pool, like the roster path.
        assert jj["value_trajectory"]["market_gap"]["our_rank"] is not None
        assert any("Mike Williams" in w and "NYJ" in w for w in out["warnings"])


def _stub_engine(monkeypatch, captured):
    async def _many(players, **kw):
        captured["players"] = players
        return {"projections": [{"player": p["name"], "team": p["team"],
                                 "position": p["position"], "projected_points": 13.0}
                                for p in players], "values_source": "test"}

    engine = types.SimpleNamespace(project_many=_many)
    monkeypatch.setattr(projections, "get_projection_engine", lambda db=None: engine)
    monkeypatch.setattr(projections, "resolve_season_week",
                        AsyncMock(return_value=(2026, 5, False)))
    monkeypatch.setattr(projections, "_log_for_retro", lambda *a, **k: None)
    monkeypatch.setattr(projections, "_with_injuries", lambda players, *a, **k: players)
    from nfl_mcp import sleeper_projections
    monkeypatch.setattr(sleeper_projections, "attach", AsyncMock(return_value=None))


class TestProjectPlayersByName:
    @pytest.mark.asyncio
    async def test_a_bare_name_is_resolved_not_a_placeholder(self, monkeypatch):
        captured = {}
        _stub_engine(monkeypatch, captured)
        out = await projections.project_players(
            ["Justin Jefferson", {"name": "Free Agent"}, {"name": "Nobody Atall"},
             {"name": "Given Guy", "team": "KC", "position": "RB"}], db=_DB())
        names = [(p["name"], p["team"], p["position"]) for p in captured["players"]]
        assert names == [("Justin Jefferson", "MIN", "WR"), ("Given Guy", "KC", "RB")]
        assert captured["players"][0]["player_id"] == "6794"
        assert [u["name"] for u in out["unresolved"]] == ["Free Agent", "Nobody Atall"]
        assert "not on an NFL team" in out["unresolved"][0]["reason"]
        assert any("not projected" in w for w in out["warnings"])

    @pytest.mark.asyncio
    async def test_ambiguous_name_warns(self, monkeypatch):
        captured = {}
        _stub_engine(monkeypatch, captured)
        out = await projections.project_players([{"name": "Mike Williams"}], db=_DB())
        assert captured["players"][0]["team"] == "PIT"
        assert out["unresolved"] == [] and "ambiguous" in out["warnings"][0]

    @pytest.mark.asyncio
    async def test_nothing_resolvable_is_an_error(self, monkeypatch):
        captured = {}
        _stub_engine(monkeypatch, captured)
        out = await projections.project_players(["Nobody Atall"], db=_DB())
        assert out["success"] is False and "players" not in captured
        assert out["unresolved"][0]["name"] == "Nobody Atall"


def _qb_depth(backup_in_market=False):
    qbs = [{"name": "Baker Mayfield", "position_rank": 14, "value": 3000}]
    if backup_in_market:
        qbs.append({"name": "Teddy Bridgewater", "position_rank": 30, "value": 100})
    return {("TB", "QB"): qbs}


def _status(statuses):
    return lambda name, team: statuses.get(name)


class TestBackupQb:
    def test_unranked_backup_is_named_from_the_depth_chart(self):
        ctx = qb_coupling.receiver_context(
            _qb_depth(), "TB", "WR", "Emeka Egbuka", _status({"Baker Mayfield": "Out"}),
            qb_order={"TB": ["Jalon Daniels", "Baker Mayfield"]},
            sleeper_ranks={("jalon daniels", "TB"): 19})
        assert ctx["backup"] == "Jalon Daniels"
        # Tiered "low" (the backtest rejects Sleeper's week rank as a tier) ...
        assert ctx["backup_tier"] == "low" and ctx["backup_tier_source"] == "unranked"
        assert ctx["sleeper_mult"] == qb_coupling.RECEIVER_MULT["WR"]["low"]
        # ... but the rank is reported, and the reason names him.
        assert ctx["backup_sleeper_rank"] == 19
        assert "Jalon Daniels" in ctx["reason"] and "unranked backup" not in ctx["reason"]

    def test_an_out_backup_is_skipped_and_the_market_backs_up_the_chart(self):
        ctx = qb_coupling.receiver_context(
            _qb_depth(backup_in_market=True), "TB", "WR", "Wr", _status(
                {"Baker Mayfield": "Out", "Jalon Daniels": "Out"}),
            qb_order={"TB": ["Jalon Daniels"]})
        assert ctx["backup"] == "Teddy Bridgewater"
        assert ctx["backup_tier"] == "mid" and ctx["backup_tier_source"] == "market_rank"

    def test_no_chart_no_market_backup(self):
        ctx = qb_coupling.receiver_context(_qb_depth(), "TB", "WR", "Wr",
                                           _status({"Baker Mayfield": "Out"}))
        assert ctx["backup"] is None and ctx["backup_tier"] == "low"

    def test_backup_rank_sources(self):
        ranks = {("jalon daniels", "TB"): 19}
        assert qb_coupling.backup_rank("X", "TB", 30, ranks) == (30, "market_rank")
        assert qb_coupling.backup_rank("Jalon Daniels", "TB", None, ranks) == (None, "unranked")
        assert qb_coupling.backup_rank("Jalon Daniels", "TB", None, ranks, by_sleeper=True) \
            == (19, "sleeper_week_rank")

    def test_sleeper_qb_ranks(self):
        index = {"by_id": {
            "1": {"name": "Top Qb", "team": "BUF", "position": "QB", "stats": {"pts_ppr": 24}},
            "2": {"name": "Jalon Daniels", "team": "TB", "position": "QB",
                  "stats": {"pts_ppr": 15}},
            "3": {"name": "A Receiver", "team": "TB", "position": "WR", "stats": {"pts_ppr": 30}},
        }}
        assert qb_coupling.sleeper_qb_ranks(index) == {("top qb", "BUF"): 1,
                                                       ("jalon daniels", "TB"): 2}
        assert qb_coupling.sleeper_qb_ranks(None) == {}

    def test_engine_reads_the_depth_chart_order(self):
        eng = projections.ProjectionEngine.__new__(projections.ProjectionEngine)
        rows = [
            {"full_name": "Baker Mayfield", "team_id": "TB",
             "raw": json.dumps({"depth_chart_order": 2})},
            {"full_name": "Jalon Daniels", "team_id": "TB",
             "raw": json.dumps({"depth_chart_order": 1})},
            {"full_name": "Third Guy", "team_id": "TB", "raw": json.dumps({})},
        ]
        eng.db = types.SimpleNamespace(get_athletes_by_positions=lambda positions: rows)
        assert eng._qb_depth_order() == {"TB": ["Jalon Daniels", "Baker Mayfield"]}
        eng.db = None
        assert eng._qb_depth_order() == {}


class TestCallersDatabaseHandle:
    def test_injury_context_reads_the_callers_handle(self, monkeypatch):
        from nfl_mcp import database, role_shift, usage_trends
        monkeypatch.setattr(database, "_shared_db", None)
        handle = object()
        seen = []
        monkeypatch.setattr(usage_trends, "_injury_index", lambda db: seen.append(db) or {})
        monkeypatch.setattr(usage_trends, "_injury_history", lambda db, idx, name, team: [
            {"injury_status": "Questionable", "recorded_at": "2026-09-22T02:00Z"}])
        monkeypatch.setattr(usage_trends, "_kickoffs",
                            lambda db, season, wk: {"CIN": "2026-09-21T17:00Z"})
        history, kickoffs = role_shift._injury_context("Ja'Marr Chase", "CIN", [3], 2026,
                                                       db=handle)
        assert seen == [handle] and history and kickoffs == {3: "2026-09-21T17:00Z"}
        assert role_shift._injury_context("Ja'Marr Chase", "CIN", [3], 2026) == ([], {})

    @pytest.mark.asyncio
    async def test_ros_hands_the_engine_its_database(self, monkeypatch):
        monkeypatch.setattr(projections, "_engine", None)
        monkeypatch.setattr(ros, "schedules_for", AsyncMock(return_value={}))
        monkeypatch.setattr(ros, "_defense_rankings", AsyncMock(return_value={}))
        handle = types.SimpleNamespace()
        await ros.ros_projections([], season=2026, week=5, db=handle)
        assert projections._engine.db is handle
