"""Byes and the statuses that fell through the cracks.

A receiver whose team had no game came back from start/sit as a 17-point
must-start: the opponent was blank or "BYE", every factor fell back to neutral
and the full baseline went out. Only the briefing knew, because it builds
players from the schedule. Alongside it: `project_player(s)` skipped the injury
lookup and week inference start/sit does, `Inactive`/`Reserve` projected at
0.9 while scoring a perfect health 100, Doubtful meant ×0.35 in one tool and a
flat zero in the other, and an Out player was reported as practising fully.
"""
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp import projections as pj
from nfl_mcp import week_context as wc
from nfl_mcp.database import NFLDatabase

# 28 teams in 14 games; KC, MIA, SEA and TEN are off. `WSH` is ESPN's spelling,
# so a Sleeper `WAS` player has to be matched through normalization.
_PLAYING = ["BUF", "NYJ", "NE", "BAL", "CIN", "CLE", "PIT", "HOU", "IND", "JAX",
            "DEN", "LV", "LAC", "DAL", "NYG", "PHI", "WSH", "CHI", "DET", "GB",
            "MIN", "ATL", "CAR", "NO", "TB", "ARI", "LAR", "SF"]


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No nflverse download: week > 1 would otherwise fetch the game logs."""
    monkeypatch.setattr(pj.opportunity_tools, "_fetch_game_logs", AsyncMock(return_value={}))


def _schedule_rows(season=2026, week=5, teams=_PLAYING):
    rows = []
    for home, away in zip(teams[::2], teams[1::2], strict=True):
        rows.append({"season": season, "week": week, "team": home, "opponent": away,
                     "is_home": 1, "kickoff": "2026-10-04T17:00Z"})
        rows.append({"season": season, "week": week, "team": away, "opponent": home,
                     "is_home": 0, "kickoff": "2026-10-04T17:00Z"})
    return rows


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        database.upsert_schedule_games(_schedule_rows())
        database.upsert_injuries([{
            "player_id": "4432665", "player_name": "Brock Bowers", "team_id": "LV",
            "position": "TE", "injury_status": "Out", "sources": ["ESPN"],
        }])
        yield database


@pytest.fixture
def empty_db():
    with tempfile.TemporaryDirectory() as tmp:
        yield NFLDatabase(str(Path(tmp) / "t.db"))


# --------------------------------------------------------------------------
# Projection-engine fakes (offline)
# --------------------------------------------------------------------------

class _Values:
    async def get_values(self, *a, **k):
        return {"source": "test", "list": []}

    def lookup(self, idx, player_id=None, name=None, position=None):
        return {"position_rank": 2}  # a WR1: 17.0 full-PPR baseline


class _Defense:
    async def fetch_defense_rankings(self):
        return {}

    def get_matchup_difficulty(self, position, opponent, rankings):
        return {"matchup_tier": "neutral", "rank": 16}


class _Vegas:
    async def fetch_current_lines(self):
        return {}

    def get_game_lines(self, team, lines, opponent=None):
        return {"home_team": team, "is_fallback": True}


def _engine(db=None):
    e = pj.ProjectionEngine.__new__(pj.ProjectionEngine)
    e.db = db
    e.values, e.defense, e.vegas = _Values(), _Defense(), _Vegas()
    return e


def _wr(team="KC", opponent="", **extra):
    return {"name": "Star WR", "position": "WR", "team": team, "opponent": opponent, **extra}


# --------------------------------------------------------------------------
# The shared helper
# --------------------------------------------------------------------------

class TestByeCheck:
    def test_explicit_marker_is_a_bye_without_any_schedule(self):
        for marker in ("BYE", "bye", " Bye "):
            assert wc.bye_check("KC", marker, None)["status"] == wc.BYE

    def test_team_missing_from_a_complete_week_is_on_bye(self, db):
        schedule = wc.week_schedule(db, 2026, 5)
        got = wc.bye_check("KC", "", schedule, 5)
        assert got["status"] == wc.BYE
        assert "KC" in got["reason"] and "week 5" in got["reason"]

    def test_schedule_wins_over_a_stale_opponent_and_says_so(self, db):
        got = wc.bye_check("KC", "LV", wc.week_schedule(db, 2026, 5), 5)
        assert got["status"] == wc.BYE
        assert "LV" in got["reason"]

    def test_blank_opponent_is_filled_from_the_schedule(self, db):
        got = wc.bye_check("BUF", "", wc.week_schedule(db, 2026, 5), 5)
        assert got == {"status": wc.PLAYING, "opponent": "NYJ",
                       "source": "schedule", "reason": None}

    def test_team_codes_are_normalized_both_ways(self, db):
        schedule = wc.week_schedule(db, 2026, 5)
        assert wc.bye_check("WAS", "", schedule, 5)["opponent"] == "CHI"
        assert wc.bye_check("CHI", "", schedule, 5)["opponent"] == "WSH"  # canonical

    def test_uncached_week_is_unknown_not_bye(self, db):
        assert wc.week_schedule(db, 2026, 6) is None
        assert wc.bye_check("KC", "", None, 6)["status"] == wc.UNKNOWN
        # A named opponent is taken at its word.
        assert wc.bye_check("KC", "LV", None, 6)["status"] == wc.PLAYING

    def test_partial_week_proves_nothing(self, empty_db):
        empty_db.upsert_schedule_games(_schedule_rows(teams=_PLAYING[:6]))
        assert wc.week_opponents(empty_db, 2026, 5)
        assert wc.week_schedule(empty_db, 2026, 5) is None

    def test_no_db_or_a_mock_db_is_unknown(self):
        from unittest.mock import MagicMock
        assert wc.week_schedule(None, 2026, 5) is None
        assert wc.week_schedule(MagicMock(), 2026, 5) is None


# --------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------

class TestProjectionOnBye:
    @pytest.mark.asyncio
    async def test_schedule_bye_projects_zero_with_a_flag(self, db):
        res = await _engine(db).project_many([_wr()], season=2026, week=5)
        p = res["projections"][0]
        assert p["projected_points"] == 0.0 and p["floor"] == 0.0 and p["ceiling"] == 0.0
        assert p["on_bye"] is True and p["bye_status"] == "bye"
        assert p["opponent"] == "BYE" and p["bye_reason"]
        assert res["on_bye"] == ["Star WR"]
        assert res["schedule_known"] is True

    @pytest.mark.asyncio
    async def test_explicit_bye_opponent_projects_zero_without_a_schedule(self):
        res = await _engine(None).project_many([_wr(opponent="BYE")])
        p = res["projections"][0]
        assert p["projected_points"] == 0.0 and p["on_bye"] is True

    @pytest.mark.asyncio
    async def test_unknown_schedule_keeps_the_projection_and_flags_it(self):
        res = await _engine(None).project_many([_wr()], season=2026, week=5)
        p = res["projections"][0]
        assert p["projected_points"] > 0
        assert p["on_bye"] is False and p["bye_status"] == "unknown"
        assert res["schedule_known"] is False

    @pytest.mark.asyncio
    async def test_playing_team_gets_its_opponent_filled(self, db):
        res = await _engine(db).project_many([_wr(team="BUF")], season=2026, week=5)
        p = res["projections"][0]
        assert p["opponent"] == "NYJ" and p["bye_status"] == "playing"
        assert p["projected_points"] > 0


class TestProjectPlayerTools:
    @pytest.mark.asyncio
    async def test_project_player_infers_the_week_and_sees_the_bye(self, db, monkeypatch):
        async def _state(db=None):
            return {"season": 2026, "week": 5, "source": "nfl_state"}
        monkeypatch.setattr("nfl_mcp.week_context.current_season_week", _state)
        with patch.object(pj, "get_projection_engine", return_value=_engine(db)):
            res = await pj.project_player("Star WR", "WR", "KC", db=db)
        assert res["week_inferred"] is True and (res["season"], res["week"]) == (2026, 5)
        assert res["projection"]["on_bye"] is True
        assert res["projection"]["projected_points"] == 0.0
        assert "bye" in res["message"]

    @pytest.mark.asyncio
    async def test_project_players_looks_up_injuries(self, db):
        with patch.object(pj, "get_projection_engine", return_value=_engine(db)):
            res = await pj.project_players(
                [{"name": "Brock Bowers", "position": "TE", "team": "LV", "opponent": "DEN"}],
                season=2026, week=5, db=db)
        p = res["projections"][0]
        assert p["injury_status"] == "Out" and p["injury_source"] == "report"
        assert p["projected_points"] == 0.0

    @pytest.mark.asyncio
    async def test_the_callers_status_still_wins(self, db):
        with patch.object(pj, "get_projection_engine", return_value=_engine(db)):
            res = await pj.project_player(
                "Brock Bowers", "TE", "LV", "DEN", injury_status="Questionable",
                season=2026, week=5, db=db)
        assert res["projection"]["injury_status"] == "Questionable"
        assert res["projection"]["projected_points"] > 0

    @pytest.mark.asyncio
    async def test_registry_opportunity_wrapper_passes_scoring(self):
        from nfl_mcp import opportunity_tools, tool_registry
        mock = AsyncMock(return_value={"success": True})
        with patch.object(opportunity_tools, "get_opportunity_projections", mock):
            await tool_registry.get_opportunity_projections(season=2026, week=5, scoring="0.5")
        assert mock.call_args.kwargs["scoring"] == "0.5"

    @pytest.mark.asyncio
    async def test_registry_project_players_accepts_bye_and_blank_opponent(self):
        from nfl_mcp import tool_registry
        mock = AsyncMock(return_value={"success": True})
        with patch.object(pj, "project_players", mock):
            await tool_registry.project_players(
                [{"name": "Star WR", "position": "WR", "team": "KC", "opponent": "BYE"},
                 {"name": "Star WR", "position": "WR", "team": "KC"}])
        sent = mock.call_args.kwargs["players"]
        assert sent[0]["opponent"] == "BYE"
        assert "opponent" not in sent[1]


# --------------------------------------------------------------------------
# Start/sit family
# --------------------------------------------------------------------------

class _FullEngine:
    """Projects a flat 17.0 unless told otherwise — the number the bug returned."""

    async def project_many(self, players, **kwargs):
        return {"projections": [
            {"projected_points": 17.0, "floor": 5.0, "ceiling": 29.0,
             "breakdown": {"base_source": "rank_bucket"}} for _ in players
        ]}


def _defense():
    from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
    analyzer = DefenseRankingsAnalyzer(db=None)
    analyzer.fetch_defense_rankings = AsyncMock(return_value={})
    return analyzer


@pytest.fixture
def optimizer(db):
    opt = lo.LineupOptimizer(db=db, defense_analyzer=_defense())
    with patch("nfl_mcp.projections.get_projection_engine", return_value=_FullEngine()), \
         patch.object(lo, "get_lineup_optimizer", return_value=opt):
        yield opt


class TestStartSitOnBye:
    @pytest.mark.asyncio
    async def test_wr_on_bye_is_must_sit_not_must_start(self, optimizer):
        out = await lo.get_start_sit_recommendation(
            player_name="Star WR", position="WR", team="KC", opponent="",
            scoring="ppr", season=2026, week=5)
        rec = out["recommendation"]
        assert rec["decision"] == "must_sit"
        assert rec["projected_points"] == 0.0
        assert rec["on_bye"] is True and rec["bye_status"] == "bye"
        assert out["factors"]["schedule"] == "on bye"

    @pytest.mark.asyncio
    async def test_bye_overrides_caller_supplied_points(self, optimizer):
        out = await lo.get_start_sit_recommendation(
            player_name="Star WR", position="WR", team="KC", opponent="LV",
            projected_points=17.0, scoring="ppr", season=2026, week=5)
        assert out["recommendation"]["decision"] == "must_sit"
        assert out["recommendation"]["projected_points"] == 0.0

    @pytest.mark.asyncio
    async def test_explicit_bye_without_schedule(self, empty_db):
        opt = lo.LineupOptimizer(db=empty_db, defense_analyzer=_defense())
        with patch("nfl_mcp.projections.get_projection_engine", return_value=_FullEngine()):
            a = await opt.analyze_player("Star WR", "", "WR", "KC", "BYE",
                                         season=2026, week=5)
        assert a.on_bye and a.decision == "must_sit" and a.projected_points == 0.0

    @pytest.mark.asyncio
    async def test_unknown_schedule_keeps_the_old_answer(self, empty_db):
        opt = lo.LineupOptimizer(db=empty_db, defense_analyzer=_defense())
        with patch("nfl_mcp.projections.get_projection_engine", return_value=_FullEngine()):
            a = await opt.analyze_player("Star WR", "", "WR", "KC", "",
                                         season=2026, week=5)
        assert not a.on_bye and a.bye_status == "unknown"
        assert a.decision == "must_start"

    @pytest.mark.asyncio
    async def test_compare_ranks_the_bye_player_last(self, optimizer):
        out = await lo.compare_players_for_slot(
            players=[
                {"name": "Bye WR", "position": "WR", "team": "KC", "opponent": "LV",
                 "projection": {"projected_points": 20.0}},
                {"name": "Playing WR", "position": "WR", "team": "BUF", "opponent": "NYJ"},
            ],
            scoring="ppr", season=2026, week=5)
        assert out["winner"]["player"] == "Playing WR"
        assert out["comparison"][1]["on_bye"] is True
        assert "on bye" in out["verdict"]

    @pytest.mark.asyncio
    async def test_roster_recommendations_list_byes(self, optimizer):
        out = await lo.get_roster_recommendations(
            players=[{"name": "Bye WR", "position": "WR", "team": "MIA"},
                     {"name": "Playing WR", "position": "WR", "team": "BUF"}],
            scoring="ppr", season=2026, week=5)
        assert out["on_bye"] == ["Bye WR (WR)"]
        assert "Bye WR (WR)" in out["sits"]
        assert "Bye WR (WR)" not in out["must_starts"]

    @pytest.mark.asyncio
    async def test_full_lineup_flags_a_bye_starter(self, optimizer):
        out = await lo.analyze_full_lineup(
            lineup={"WR": [{"name": "Bye WR", "position": "WR", "team": "SEA"}],
                    "BENCH": [{"name": "Bench WR", "position": "WR", "team": "BUF"}]},
            scoring="ppr", season=2026, week=5)
        weak = out["weak_spots"][0]
        assert weak["on_bye"] is True and "bye" in weak["issue"]
        assert out["suggested_changes"][0]["bench_in"] == "Bench WR"
        assert out["starters"]["WR"][0]["decision"] == "must_sit"


# --------------------------------------------------------------------------
# Statuses
# --------------------------------------------------------------------------

class TestStatusVocabulary:
    @pytest.mark.parametrize("status", [
        "Inactive", "Reserve", "Reserve/PUP", "PUP", "PUP-R", "Sus", "Suspended",
        "IR", "Injured Reserve", "Reserve-Suspended",
    ])
    def test_unavailable_statuses_zero_everywhere(self, status):
        assert pj.availability(status) == "out"
        assert pj._injury_mult(status) == 0.0
        assert lo.injury_score(status) == 0
        opt = lo.LineupOptimizer.__new__(lo.LineupOptimizer)
        assert opt.determine_decision(
            20.0, "WR", lo.injury_score(status), injury_status=status) == "must_sit"

    def test_unknown_is_mild_uncertainty_without_a_warning(self, caplog):
        assert pj.availability("Unknown") == "uncertain"
        assert 0.9 < pj._injury_mult("Unknown") < 1.0
        assert not caplog.records
        assert 50 < lo.injury_score("Unknown") < 100

    def test_unknown_is_never_a_must_start(self):
        opt = lo.LineupOptimizer.__new__(lo.LineupOptimizer)
        assert opt.determine_decision(
            25.0, "WR", lo.injury_score("Unknown"), injury_status="Unknown") == "start"

    def test_an_unlisted_status_no_longer_scores_perfect_health(self):
        assert lo.injury_score("Some New Code") < 100


class TestDoubtfulAgrees:
    """Doubtful: projected at ×0.35 and labelled risky, never started."""

    def test_projection_keeps_the_discount(self):
        assert pj._injury_mult("Doubtful") == 0.35

    def test_decision_follows_the_discounted_points_capped_at_sit(self):
        opt = lo.LineupOptimizer.__new__(lo.LineupOptimizer)
        score = lo.injury_score("Doubtful")
        # 30 × 0.35 = 10.5 would be a WR "start"; doubtful caps it at sit.
        assert opt.determine_decision(10.5, "WR", score, injury_status="Doubtful") == "sit"
        # Low enough, the points alone say must_sit — same as the projection.
        assert opt.determine_decision(2.0, "WR", score, injury_status="Doubtful") == "must_sit"

    def test_legacy_callers_without_a_status_still_bench_on_health(self):
        opt = lo.LineupOptimizer.__new__(lo.LineupOptimizer)
        assert opt.determine_decision(20.0, "WR", 25) == "must_sit"

    @pytest.mark.asyncio
    async def test_analysis_labels_it_risky(self, optimizer):
        a = await optimizer.analyze_player(
            "Star WR", "", "WR", "BUF", "NYJ", injury_data={"status": "Doubtful"},
            season=2026, week=5)
        assert a.decision in ("sit", "must_sit")
        assert any("risky" in r for r in a.reasoning)


class TestPracticeStatus:
    @pytest.mark.asyncio
    async def test_out_player_is_not_reported_as_practising_fully(self, optimizer):
        out = await lo.get_start_sit_recommendation(
            player_name="Hurt WR", position="WR", team="BUF", opponent="NYJ",
            injury_status="Out", scoring="ppr", season=2026, week=5)
        assert "full" not in out["factors"]["health"].lower()
        assert out["factors"]["health"] == "Out"
        assert not any("full practice" in r for r in out["reasoning"])

    @pytest.mark.asyncio
    async def test_default_practice_status_is_none(self, optimizer):
        a = await optimizer.analyze_player(
            "Hurt WR", "", "WR", "BUF", "NYJ", injury_data={"status": "Out"},
            season=2026, week=5)
        assert a.practice_status is None
        assert a.to_dict()["practice_status"] is None

    @pytest.mark.asyncio
    async def test_a_reported_practice_is_still_shown(self, optimizer):
        out = await lo.get_start_sit_recommendation(
            player_name="Hurt WR", position="WR", team="BUF", opponent="NYJ",
            injury_status="Questionable", practice_status="limited",
            scoring="ppr", season=2026, week=5)
        assert out["factors"]["health"] == "Questionable, Practice: limited"


# --------------------------------------------------------------------------
# Briefing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_briefing_names_byes_separately(monkeypatch, db):
    from nfl_mcp import briefing_tools, projections, sleeper_tools, weather_tools

    db.upsert_athletes({
        "bye": {"full_name": "Bye WR", "position": "WR", "team": "KC"},
        "on": {"full_name": "Playing WR", "position": "WR", "team": "BUF"},
    })
    monkeypatch.setattr(briefing_tools, "get_shared_db", lambda *a, **k: db)

    async def _league(_):
        return {"league": {"total_rosters": 10, "scoring_settings": {"rec": 1},
                           "roster_positions": ["WR", "BN"], "settings": {}}}

    async def _rosters(_):
        return {"rosters": [{"roster_id": 1, "players": ["bye", "on"], "starters": ["on"]}]}

    async def _matchups(_, week):
        return {"matchups": [{"roster_id": 1, "matchup_id": 1, "starters": ["on"],
                              "players_points": {}}]}

    async def _weather(**_):
        return {"games": []}

    async def _project(players, **_):
        return {"projections": [
            {"player": p["name"], "position": p["position"], "team": p["team"],
             "opponent": p["opponent"], "projected_points": 10.0, "floor": 5.0,
             "ceiling": 15.0} for p in players
        ]}

    monkeypatch.setattr(sleeper_tools, "get_league", _league)
    monkeypatch.setattr(sleeper_tools, "get_rosters", _rosters)
    monkeypatch.setattr(sleeper_tools, "get_matchups", _matchups)
    monkeypatch.setattr(weather_tools, "get_weather_forecast", _weather)
    monkeypatch.setattr(projections, "project_players", _project)

    out = await briefing_tools.get_weekly_briefing("L", roster_id=1, week=5, season=2026)
    assert out["on_bye"] == ["Bye WR"]
    assert out["not_projected"] == []
