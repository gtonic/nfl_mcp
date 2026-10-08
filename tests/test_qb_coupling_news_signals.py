"""QB <-> pass-catcher coupling, news-text signals and role security."""
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import news_signals, opportunity_tools, projections, qb_coupling, ros
from nfl_mcp import sleeper_projections as sp
from nfl_mcp.waiver_target_tools import _droppable, _pair_drop, get_waiver_targets
from tests.test_sleeper_first_blend import _engine
from tests.test_vacated_volume import _games, _index
from tests.test_waiver_targets import (
    LEAGUE,
    _stub_sleeper,
    db,  # noqa: F401  (fixture)
)

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%MZ")


def _flags(text, owner="owner", names=None, owner_last=""):
    return {h["flag"]: h["about"] for h in news_signals.classify(text, owner, names, owner_last)}


class TestClassify:
    @pytest.mark.parametrize("text,flag", [
        ("Swift didn't get another carry after his fumble in the second quarter.", "benched"),
        ("LaFleur said the two backs are 'gonna rotate' going forward.", "committee"),
        ("The Chargers will split the work between Hampton and Mitchell.", "committee"),
        ("McConkey (foot) is considered week-to-week, Harbaugh said.", "week_to_week"),
        ("The Seahawks designated Charbonnet (knee) to return to practice Wednesday.",
         "designated_to_return"),
        ("The Browns opened Davis' (quadriceps) 21-day practice window Tuesday.",
         "designated_to_return"),
        ("Love is expected to be the lead back with Conner out.", "lead_role"),
        ("Huntley is trending toward the start Sunday.", "lead_role"),
        ("He will be on a snap count in his first game back.", "limited_snaps"),
        ("Rice (hamstring) is expected to play Sunday.", "expected_to_play"),
        ("Adams is not expected to play Sunday.", "unlikely_to_play"),
        ("Overshown (hamstring) has been ruled out for Thursday's game.", "ruled_out"),
        ("Bentley (coach's decision) is inactive for Sunday's game.", "benched"),
    ])
    def test_phrases(self, text, flag):
        assert flag in _flags(text)

    @pytest.mark.parametrize("text", [
        "Higgins didn't practice Wednesday due to groin and neck injuries.",
        "Mumpfield suffered a rotator cuff contusion.",           # not "rotate"
        "Love (ankle) practiced on a limited basis Wednesday.",   # practice, not snaps
        "Smith isn't expected to start Sunday.",                  # negated lead role
        "Jones won't be benched despite the fumble.",             # negated benching
        "Humphrey (calf) has been ruled out for the rest of Sunday's game.",  # in-game
    ])
    def test_non_signals(self, text):
        assert _flags(text) == {}

    def test_a_teammates_blurb_is_attributed_to_the_player_it_names(self):
        names = {"bagent": "tyson bagent", "johnson": "x johnson"}
        flags = _flags("Keenum is expected to remain in a backup role after head coach Ben "
                       "Johnson said that Tyson Bagent will start at quarterback.",
                       "case keenum", names, "keenum")
        assert flags == {"lead_role": "tyson bagent"}

    def test_a_shared_last_name_is_settled_by_the_full_name_or_dropped(self):
        names = {"jackson": None, "lamar jackson": "lamar jackson"}
        text = "Huntley took the first-team reps with Lamar Jackson appearing unlikely to play."
        assert _flags(text, "tyler huntley", names, "huntley") == {
            "unlikely_to_play": "lamar jackson"}
        assert _flags("Jackson is not expected to play.", "tyler huntley", names,
                      "huntley") == {}

    def test_the_owners_own_name_is_the_owner(self):
        """Two Williamses on the roster: the blurb's own player is meant."""
        names = {"williams": None}
        assert _flags("Williams remains week-to-week.", "evan williams", names,
                      "williams") == {"week_to_week": "evan williams"}


class TestIndexAndRecency:
    def test_recency_decays_and_cuts_off(self):
        assert news_signals.recency_weight(_ago(0), NOW) == 1.0
        assert news_signals.recency_weight(_ago(news_signals.HALF_LIFE_DAYS), NOW) == 0.5
        assert news_signals.recency_weight(_ago(news_signals.MAX_AGE_DAYS + 1), NOW) == 0.0
        assert news_signals.recency_weight(None, NOW) == news_signals.UNDATED_WEIGHT

    def test_index_from_report_rows(self):
        rows = [
            {"player_name": "Case Keenum", "team_id": "CHI", "date_reported": _ago(1),
             "injury_description": "Keenum will remain the backup after Tyson Bagent "
                                   "was named starter."},
            {"player_name": "Tyson Bagent", "team_id": "CHI", "date_reported": _ago(30),
             "injury_description": "Bagent was benched in the preseason."},
            {"player_name": "D'Andre Swift", "team_id": "CHI", "date_reported": _ago(2),
             "injury_description": "Swift didn't get another carry after his fumble."},
        ]
        index = news_signals.build_index(rows, NOW)
        bagent = news_signals.signals_for(index, "Tyson Bagent", "CHI")
        # His own month-old benching is past the cut-off; Keenum's blurb counts.
        assert [f["flag"] for f in bagent] == ["lead_role"]
        assert bagent[0]["from_player"] == "Case Keenum"
        swift = news_signals.signals_for(index, "D'Andre Swift", "CHI")
        assert swift[0]["flag"] == "benched" and 0.6 < swift[0]["weight"] < 0.8
        assert "fumble" in swift[0]["snippet"]

    def test_player_news_reads_the_database(self):
        fake = types.SimpleNamespace(get_all_current_injuries=lambda: [
            {"player_name": "Ladd McConkey", "team_id": "LAC",
             "date_reported": datetime.now(UTC).isoformat(),
             "injury_description": "McConkey is considered week-to-week."}])
        assert [f["flag"] for f in news_signals.player_news(fake, "Ladd McConkey", "LAC")] == [
            "week_to_week"]
        assert news_signals.player_news(None, "Ladd McConkey", "LAC") == []


class TestAdjustment:
    def _f(self, flag, weight=1.0):
        return {"flag": flag, "weight": weight, "snippet": "x"}

    def test_benched_cuts_the_model_share_and_confidence(self):
        adj = news_signals.adjustment([self._f("benched")])
        assert adj["model_mult"] == news_signals.EFFECTS["benched"]["model_mult"]
        assert adj["confidence_delta"] == news_signals.EFFECTS["benched"]["confidence"]

    def test_recency_scales_the_effect(self):
        adj = news_signals.adjustment([self._f("committee", 0.5)])
        assert adj["model_mult"] == round(1 - (1 - 0.93) * 0.5, 3)

    def test_bounded(self):
        adj = news_signals.adjustment([self._f(f) for f in
                                       ("benched", "committee", "limited_snaps", "week_to_week",
                                        "ruled_out")])
        assert adj["model_mult"] >= news_signals.MIN_MODEL_MULT
        assert adj["confidence_delta"] >= -news_signals.MAX_CONFIDENCE_SWING

    def test_a_role_shift_already_priced_keeps_news_to_confidence(self):
        adj = news_signals.adjustment([self._f("committee")], role_trend="role_down")
        assert adj["model_mult"] == 1.0
        assert adj["confidence_delta"] == news_signals.EFFECTS["committee"]["confidence"]

    def test_availability_flags_ignore_an_out_player(self):
        assert news_signals.adjustment([self._f("ruled_out")], availability="out")[
            "confidence_delta"] == 0

    def test_lead_role_adds_confidence_only(self):
        adj = news_signals.adjustment([self._f("lead_role")])
        assert adj["model_mult"] == 1.0 and adj["confidence_delta"] > 0


class TestRoleSecurity:
    def test_rising_role(self):
        sec = news_signals.role_security({"role_trend": "role_up",
                                          "role_flags": ["carries share 43%→66% (weeks 3-4)"]})
        assert sec["label"] == "rising" and sec["score"] == 1.0
        assert "43%→66%" in sec["reasons"][0]

    def test_shrinking_role_and_borrowed_volume(self):
        sec = news_signals.role_security({
            "role_trend": "role_down",
            "news_flags": [{"flag": "committee", "weight": 1.0, "snippet": "gonna rotate"}],
            "breakdown": {"returning_teammates": [{"name": "Nico Collins"}]}})
        assert sec["score"] == -news_signals.SECURITY_RANGE
        assert sec["label"] == "shrinking"
        assert any("Nico Collins" in r for r in sec["reasons"])

    def test_volume_from_an_absent_starter_is_borrowed(self):
        sec = news_signals.role_security({"breakdown": {
            "vacated_volume": {"targets": 2.0}, "starters_out_ahead": ["Puka Nacua"]}})
        assert sec["score"] < 0 and "Puka Nacua" in sec["reasons"][0]

    def test_neutral(self):
        assert news_signals.role_security({})["label"] == "neutral"


def _depth(starter_status=None, backup_rank=40):
    depth = {
        ("TB", "QB"): [{"name": "Starter Qb", "position_rank": 12, "value": 3000},
                       {"name": "Backup Qb", "position_rank": backup_rank, "value": 100}],
        ("TB", "WR"): [{"name": "Wide One", "position_rank": 20, "value": 5000},
                       {"name": "Wide Two", "position_rank": 40, "value": 2000}],
        ("TB", "TE"): [{"name": "Tight End", "position_rank": 10, "value": 1500}],
    }
    statuses = {"Starter Qb": starter_status}

    def status_of(name, team):
        return statuses.get(name)
    return depth, status_of


def _qb_logs(qb_weeks=(1, 2, 3, 4)):
    return _index([
        {"player_id": "q", "name": "Starter Qb", "position": "QB", "team": "TB",
         "games": _games(list(qb_weeks), attempts=35.0)},
        {"player_id": "w", "name": "Wide One", "position": "WR", "team": "TB",
         "games": _games([1, 2, 3, 4], targets=8.0)},
    ])


class TestReceiverContext:
    def test_healthy_starter_is_no_context(self):
        depth, status_of = _depth(None)
        assert qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", status_of) is None

    def test_starter_out_low_backup_cuts_a_receiver(self):
        depth, status_of = _depth("Out")
        ctx = qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", status_of,
                                           _qb_logs(), 5)
        m = qb_coupling.RECEIVER_MULT["WR"]["low"]
        assert ctx["applied"] and ctx["backup"] == "Backup Qb" and ctx["backup_tier"] == "low"
        assert ctx["sleeper_mult"] == m and ctx["model_mult"] == m
        assert qb_coupling.MIN_MULT <= m < 1.0
        assert "Starter Qb" in ctx["reason"]

    def test_weeks_already_with_the_backup_scale_the_model_share(self):
        depth, status_of = _depth("IR")
        ctx = qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", status_of,
                                           _qb_logs((1, 2)), 5)
        m = qb_coupling.RECEIVER_MULT["WR"]["low"]
        assert ctx["with_starter_share"] == 0.5
        assert ctx["sleeper_mult"] == m
        assert ctx["model_mult"] == round(1 - (1 - m) * 0.5, 3)

    def test_a_starter_gone_all_window_is_not_this_offenses_qb(self):
        depth, status_of = _depth("Out")
        ctx = qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", status_of,
                                           _qb_logs(()), 5)
        assert ctx["applied"] is False and ctx["sleeper_mult"] == 1.0

    def test_doubtful_counts_at_its_weight(self):
        depth, status_of = _depth("Doubtful")
        ctx = qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", status_of)
        m = qb_coupling.RECEIVER_MULT["WR"]["low"]
        assert ctx["sleeper_mult"] == round(1 - (1 - m) * qb_coupling.DOUBTFUL_WEIGHT, 3)

    def test_tight_ends_and_starter_grade_backups_are_flagged_not_cut(self):
        depth, status_of = _depth("Out")
        te = qb_coupling.receiver_context(depth, "TB", "TE", "Tight End", status_of)
        assert te["applied"] is False and te["sleeper_mult"] == 1.0 and "tight ends" in te["reason"]
        depth, status_of = _depth("Out", backup_rank=20)
        wr = qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", status_of)
        assert wr["backup_tier"] == "starter_grade" and wr["applied"] is False

    def test_tiers(self):
        assert qb_coupling.qb_tier(20) == "starter_grade"
        assert qb_coupling.qb_tier(30) == "mid"
        assert qb_coupling.qb_tier(None) == "low"


class TestCatchersContext:
    def test_top_catchers_out_are_reported_not_priced(self):
        depth, _ = _depth()

        def status_of(name, team):
            return {"Wide One": "Out", "Tight End": "Questionable"}.get(name)
        ctx = qb_coupling.catchers_context(depth, "TB", status_of)
        assert ctx["top_pass_catchers"] == ["Wide One", "Wide Two"]
        assert ctx["missing"] == [{"name": "Wide One", "status": "Out"}]
        assert ctx["multiplier"] == 1.0
        assert ctx["confidence_delta"] == qb_coupling.CATCHERS_OUT_CONFIDENCE

    def test_healthy_catchers(self):
        depth, status_of = _depth()
        assert qb_coupling.catchers_context(depth, "TB", status_of) is None


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def get_all_current_injuries(self):
        return self.rows


def _engine_with(rows, values):
    eng = _engine()
    eng.db = _FakeDb(rows)
    eng.values = types.SimpleNamespace(
        get_values=AsyncMock(return_value={"list": values, "source": "test"}),
        lookup=lambda *a, **k: None)
    return eng


def _tb_logs():
    return {
        "q": {"player_id": "q", "name": "Starter Qb", "position": "QB", "team": "TB",
              "games": [{**g, "team": "TB"} for g in _games([1, 2, 3, 4], attempts=35.0)]},
        "w": {"player_id": "w", "name": "Wide One", "position": "WR", "team": "TB",
              "games": [{**g, "team": "TB"} for g in _games([1, 2, 3, 4], targets=8.0)]},
    }


_VALUES = [
    {"name": "Starter Qb", "team": "TB", "position": "QB", "position_rank": 12, "value": 3000},
    {"name": "Backup Qb", "team": "TB", "position": "QB", "position_rank": 45, "value": 50},
    {"name": "Wide One", "team": "TB", "position": "WR", "position_rank": 20, "value": 5000},
]


def _sleeper(points=10.0):
    return [{"player_id": "1", "team": "TB", "opponent": "DAL",
             "player": {"first_name": "Wide", "last_name": "One", "position": "WR", "team": "TB"},
             "stats": {"rec": 5.0, "rec_yd": 60.0, "pts_ppr": points}}]


class TestOnTheProjection:
    @pytest.mark.asyncio
    async def test_starter_out_cuts_both_shares(self, monkeypatch):
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_tb_logs()))
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_sleeper())))
        rows = [{"player_name": "Starter Qb", "team_id": "TB", "injury_status": "Out",
                 "injury_description": "out", "date_reported": None}]
        player = {"name": "Wide One", "position": "WR", "team": "TB", "opponent": "DAL",
                  "player_id": "1"}
        healthy = (await _engine_with([], _VALUES).project_many(
            [player], season=2026, week=5))["projections"][0]
        p = (await _engine_with(rows, _VALUES).project_many(
            [player], season=2026, week=5))["projections"][0]
        m = qb_coupling.RECEIVER_MULT["WR"]["low"]
        ctx = p["qb_context"]
        assert ctx["applied"] and ctx["sleeper_mult"] == m and ctx["model_mult"] == m
        assert p["breakdown"]["qb_sleeper_mult"] == m
        assert p["model_projection"] == round(healthy["model_projection"] * m, 1)
        assert p["projected_points"] == round(
            0.25 * p["model_projection"] + 0.75 * p["sleeper_projection"] * m, 1)
        assert p["projected_points"] < healthy["projected_points"]
        assert "qb_context" not in healthy and healthy["news_flags"] == []

    @pytest.mark.asyncio
    async def test_news_moves_only_the_model_share(self, monkeypatch):
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_tb_logs()))
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_sleeper())))
        rows = [{"player_name": "Wide One", "team_id": "TB", "injury_status": "Active",
                 "date_reported": datetime.now(UTC).isoformat(),
                 "injury_description": "Wide One was benched after two drops."}]
        player = {"name": "Wide One", "position": "WR", "team": "TB", "opponent": "DAL",
                  "player_id": "1"}
        base = (await _engine_with([], _VALUES).project_many(
            [player], season=2026, week=5))["projections"][0]
        p = (await _engine_with(rows, _VALUES).project_many(
            [player], season=2026, week=5))["projections"][0]
        assert [f["flag"] for f in p["news_flags"]] == ["benched"]
        mult = p["breakdown"]["news_model_mult"]
        assert 0.84 < mult < 0.86
        assert p["model_projection"] == round(base["model_projection"] * mult, 1)
        assert p["projected_points"] == round(
            0.25 * p["model_projection"] + 0.75 * p["sleeper_projection"], 1)
        assert p["confidence"] < base["confidence"]

    @pytest.mark.asyncio
    async def test_model_only_takes_the_model_multipliers(self, monkeypatch):
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_tb_logs()))
        rows = [{"player_name": "Starter Qb", "team_id": "TB", "injury_status": "Out"}]
        player = {"name": "Wide One", "position": "WR", "team": "TB", "opponent": "DAL"}
        p = (await _engine_with(rows, _VALUES).project_many(
            [player], season=2026, week=5))["projections"][0]
        assert p["projection_source"] == "model_only"
        assert p["projected_points"] == p["model_projection"]
        vol = projections._VOLATILITY["WR"]
        assert p["floor"] == round(p["projected_points"] * (1 - vol), 1)


class TestRos:
    def test_week_to_week_is_two_games(self):
        games, reason = ros.expected_absence("Out", "Mitchell remains week-to-week.")
        assert games == ros.WEEK_TO_WEEK_GAMES and "week-to-week" in reason
        assert ros.expected_absence("Out", "out this week")[0] == 1
        assert ros.expected_absence("Questionable", "week-to-week")[0] == 0

    @pytest.mark.asyncio
    async def test_backup_qb_weeks_and_news_flags_carry_into_ros(self, monkeypatch):
        flags = [{"flag": "committee", "weight": 0.8, "snippet": "rotate"}]

        async def _project(players, **kw):
            return {"projections": [{
                "player": p["name"], "team": p["team"], "position": p["position"],
                "opponent": p["opponent"], "projected_points": 10.0,
                "breakdown": {"base_ppg": 12.0, "base_source": "rank_bucket"},
                "news_flags": flags, "role_trend": "stable",
                "qb_context": {"applied": True, "model_mult": 0.9, "games_out": 2,
                               "starter": "Starter Qb", "reason": "backup"}}
                for p in players]}
        monkeypatch.setattr(projections, "project_players", _project)

        async def _sched(_db, season, weeks):
            return {w: {"TB": "DAL", "DAL": "TB"} for w in weeks}
        monkeypatch.setattr(ros, "schedules_for", _sched)
        monkeypatch.setattr(ros, "_defense_rankings", AsyncMock(return_value={}))
        out = await ros.ros_projections(
            [{"name": "Wide One", "position": "WR", "team": "TB"}],
            season=2026, week=5, settings={"playoff_week_start": 15})
        entry = out["players"][0]
        assert entry["news_flags"] == flags
        assert "role_trend" not in entry  # only a moved role is passed on (#247)
        weekly = entry["weekly_points"]
        # Week 5 is the weekly projection; week 6 the second game without the
        # starter (x0.9); week 7 on, the full rate.
        assert weekly[5] == 10.0
        assert weekly[6] == round(12.0 * 0.9, 2)
        assert weekly[7] == 12.0
        assert entry["qb_context"]["games_out"] == 2


def _mine(name, points, value, role=None, pid=None):
    return {"player_id": pid or name, "name": name, "position": "WR",
            "projected_points": points, "value": value, "ros_total": None,
            "role_security": news_signals.role_security(role or {})}


class TestDrops:
    def test_a_shrinking_role_surfaces_first(self):
        slots = {"WR": 1}
        starter = _mine("Starter", 20.0, 9000)
        settled = _mine("Settled", 8.0, 3000)
        shrinking = _mine("Shrinking", 8.5, 3100, {
            "role_trend": "role_down", "role_flags": ["target share 26%→6% (week 4)"]})
        drops = _droppable([starter, settled, shrinking], slots, set())
        assert [d["name"] for d in drops] == ["Shrinking", "Settled"]
        assert drops[0]["role_security"]["label"] == "shrinking"
        # Without the role read the order is by value.
        plain = _droppable([starter, settled, _mine("Shrinking", 8.5, 3100)], slots, set())
        assert [d["name"] for d in plain] == ["Settled", "Shrinking"]

    def test_the_pairing_note_says_why(self):
        slots = {"WR": 1}
        players = [_mine("Starter", 20.0, 9000), _mine("Shrinking", 8.5, 3100, {
            "role_trend": "role_down", "role_flags": ["target share 26%→6% (week 4)"]})]
        target = {**_mine("Target", 9.0, 4000), "position": "RB"}
        drop, note = _pair_drop(target, players, slots, set(), 0)
        assert drop["name"] == "Shrinking" and "role down" in note
        assert drop["role_security"]["label"] == "shrinking"


class TestWaiverRanking:
    @pytest.mark.asyncio
    async def test_a_shrinking_role_ranks_below_an_equal_upgrade(self, db, monkeypatch):  # noqa: F811
        _stub_sleeper(monkeypatch, {
            "My Starter WR": 14.0, "My Second WR": 9.0, "My Weak RB": 3.0,
            "Free Good RB": 6.0, "Free Weak WR": 15.0,
        })
        stub = projections.project_players

        async def _with_roles(players, **kw):
            out = await stub(players, **kw)
            for p in out["projections"]:
                if p["player"] == "Free Weak WR":
                    p.update(role_trend="role_down",
                             role_flags=["target share 26%→6% (week 4)"])
            return out
        monkeypatch.setattr(projections, "project_players", _with_roles)
        out = await get_waiver_targets(LEAGUE, roster_id=7)
        top = {t["name"]: t for t in out["targets"]}
        assert top["Free Weak WR"]["upgrade_points"] == top["Free Good RB"]["upgrade_points"]
        # Trending adds used to break the tie for the WR; his lost role now does.
        assert [t["name"] for t in out["targets"]][:2] == ["Free Good RB", "Free Weak WR"]
        wr = top["Free Weak WR"]
        assert wr["role_security"]["label"] == "shrinking"
        assert wr["rank_score"] == round(wr["upgrade_points"] - 0.75, 2)
        assert wr["verdict"] == "upgrade"  # the verdict rests on the points alone


class TestInjuryExitWeeks:
    def _project(self, games, role):
        logs = _index([{"player_id": "w", "name": "Wide One", "position": "WR", "team": "TB",
                        "games": games}])
        return _engine()._project_one(
            {"name": "Wide One", "position": "WR", "team": "TB", "opponent": "DAL"},
            {"list": []}, {}, {}, logs, 5, 1.0, role=role)

    def test_an_injury_shortened_game_is_left_out_of_the_base(self):
        """Jefferson's 12%-snap exit, Chase's concussion game: not his rate."""
        games = _games([1, 2, 3], targets=9.0) + _games([4], targets=1.0)
        plain = self._project(games, None)
        exited = self._project(games, {"injury_exit_weeks": [4]})
        assert exited["breakdown"]["injury_exit_weeks"] == [4]
        assert plain["breakdown"]["injury_exit_weeks"] == []
        assert exited["breakdown"]["usage_games"] == plain["breakdown"]["usage_games"] - 1
        assert exited["breakdown"]["base_ppg"] > plain["breakdown"]["base_ppg"]
        assert exited["projected_points"] > plain["projected_points"]

    @pytest.mark.asyncio
    async def test_exits_reach_the_base_even_without_a_role_read(self, monkeypatch):
        """Jefferson: 92% / 100% / 12% (hurt) / out -- too few games for a
        role read, but week 3 still has to leave his volume."""
        logs = {"j": {"player_id": "j", "name": "Wide One", "position": "WR", "team": "MIN",
                      "games": [{**g, "team": "MIN"} for g in _games([1, 2, 3], targets=9.0)]}}
        rows = [{"week": 1, "status": "played", "snap_share": 92.0, "target_share": 39.0},
                {"week": 2, "status": "played", "snap_share": 100.0, "target_share": 32.0},
                {"week": 3, "status": "played", "snap_share": 12.0, "target_share": 8.0,
                 "injury_exit": True},
                {"week": 4, "status": "did_not_play"}]
        monkeypatch.setattr(projections.role_shift, "player_rows", lambda *a, **k: rows)
        reads, _ = await projections._role_reads(
            [{"name": "Wide One", "position": "WR", "team": "MIN"}], logs,
            opportunity_tools.build_name_index(logs), 2026, 5)
        assert reads[0]["role_trend"] == "insufficient_data"
        assert reads[0]["injury_exit_weeks"] == [3]

    def test_too_few_games_left_keeps_them(self):
        games = _games([3], targets=9.0) + _games([4], targets=1.0)
        p = self._project(games, {"injury_exit_weeks": [4]})
        assert p["breakdown"]["base_source"] == "opportunity"
        assert p["breakdown"]["injury_exit_weeks"] == []


class TestTrajectoryNews:
    def _assess(self, **extra):
        from nfl_mcp import value_trajectory
        weeks = list(range(5, 18))
        e = {"player": "Back", "player_id": "Back", "position": "RB", "team": "LAC",
             "per_game": 10.0, "weekly_points": dict.fromkeys(weeks, 10.0),
             "expected_absence_games": 0, **extra}
        return value_trajectory.assess(e, week=5)

    def test_committee_is_a_soft_falling_signal_never_a_call_alone(self):
        t = self._assess(news_flags=[{"flag": "committee", "weight": 1.0,
                                      "snippet": "the backs are gonna rotate"}])
        assert any(s["kind"] == "news_role" and s["change"] < 0 for s in t["signals"])
        assert t["signal"] == "hold"
        assert "gonna rotate" in t["reasons"][0]

    def test_news_does_not_double_a_usage_read(self):
        t = self._assess(role_trend="role_down",
                         role_flags=["carries share 60%→30% (weeks 3-4)"],
                         news_flags=[{"flag": "committee", "weight": 1.0, "snippet": "rotate"}])
        assert not any(s["kind"] == "news_role" for s in t["signals"])

    def test_own_designation_to_return_is_a_hard_signal(self):
        t = self._assess(expected_absence_games=4, injury_status="IR", news_flags=[
            {"flag": "designated_to_return", "weight": 1.0,
             "snippet": "The Seahawks opened his 21-day practice window."}])
        assert t["signal"] == "buy_low"
        assert any(s["kind"] == "injury_return" for s in t["signals"])
