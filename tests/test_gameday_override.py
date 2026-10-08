"""The published gameday decision overrides the week's injury tag.

A questionable player's projection is an *expected* value before inactives:
it includes the chance he sits. Once he is confirmed active (~90 minutes
before kickoff) that part is settled and he is priced on what such a player
scores when he plays; officially inactive is zero. The starting QB's
decision drives his receivers' coupling the same way. Also: Doubtful and
questionable-without-a-practice-line recalibrated, and the "misses this week"
availability threshold kept apart from the Doubtful multiplier.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import gameday_inactives as gi
from nfl_mcp import injury_status, opportunity_tools, projections, qb_coupling
from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp import sleeper_projections as sp
from nfl_mcp.injury_match import MISSES_WEEK_MAX_MULT, misses_this_week
from nfl_mcp.projections import (
    CONFIRMED_ACTIVE_REALISED,
    PRACTICE_BLEND_MULT,
    QUESTIONABLE_REALISED,
    confirmed_active_blend_mult,
    confirmed_active_mult,
    practice_adjusted_mult,
)
from tests.test_qb_coupling_news_signals import _depth
from tests.test_sleeper_first_blend import PLAYERS, _engine, _logs, _payload

KICKOFF = datetime(2026, 10, 11, 17, 0, tzinfo=UTC)


class TestRecalibratedTags:
    def test_doubtful_almost_never_plays(self):
        # 1 of 60 relevant doubtful players played in 2023-25.
        assert projections._injury_mult("Doubtful") == injury_status.DOUBTFUL_MULT == 0.02
        assert injury_status.STATUS_TABLE["Doubtful"].multiplier == 0.02
        # Still not Out: Out zeroes Sleeper's share outright.
        assert projections._injury_mult("Doubtful") > projections._injury_mult("Out")

    def test_questionable_without_a_practice_line_is_the_mix(self):
        assert projections._injury_mult("Questionable") == injury_status.QUESTIONABLE_MULT == 0.74
        assert QUESTIONABLE_REALISED["NONE"] == injury_status.QUESTIONABLE_MULT
        assert practice_adjusted_mult("Questionable", None) == 0.74
        # Sleeper's share takes it too: the blend lands at the measured ~0.74.
        assert PRACTICE_BLEND_MULT["NONE"] == round(0.74 / projections.SLEEPER_QUESTIONABLE_PRICED, 2)
        assert projections.practice_blend_mult("Questionable", None) == PRACTICE_BLEND_MULT["NONE"]
        total = 0.25 * 0.74 + 0.75 * projections.SLEEPER_QUESTIONABLE_PRICED * PRACTICE_BLEND_MULT["NONE"]
        assert total == pytest.approx(0.74, abs=0.01)

    def test_full_practice_still_leaves_sleeper_alone(self):
        assert projections.practice_blend_mult("Questionable", "FP") == 1.0
        assert projections.practice_blend_mult("Questionable", "REST") == 1.0

    def test_doubtful_blend_cap_follows_the_new_share(self):
        # healthy ours 10 (x0.02 = 0.2), Sleeper still projects him in full.
        assert sp.blend(0.2, 12.0, "doubtful", 0.02) == round(0.02 * 12.0, 1)


class TestAvailabilityThresholdIsSeparate:
    def test_misses_this_week_unchanged(self):
        assert misses_this_week("Doubtful") and misses_this_week("Out")
        assert misses_this_week("IR") and misses_this_week("Inactive")
        assert not misses_this_week("Questionable") and not misses_this_week(None)
        assert not misses_this_week("Active")

    def test_threshold_is_not_the_doubtful_multiplier(self, monkeypatch):
        assert MISSES_WEEK_MAX_MULT == 0.35
        # Recalibrating Doubtful up to the threshold's neighbourhood or
        # Questionable down must not move availability semantics.
        monkeypatch.setattr(injury_status, "DOUBTFUL_MULT", 0.3)
        assert misses_this_week("Doubtful")
        assert not misses_this_week("Questionable")


class TestConfirmedActiveMult:
    @pytest.mark.parametrize("practice, pattern, expected", [
        (None, None, CONFIRMED_ACTIVE_REALISED["NONE"]),
        ("LP", None, CONFIRMED_ACTIVE_REALISED["LP"]),
        ("DNP", "DNP-DNP", CONFIRMED_ACTIVE_REALISED["DNP"]),
        ("DNP", "DNP", CONFIRMED_ACTIVE_REALISED["NONE"]),      # one DNP so far
        ("FP", None, QUESTIONABLE_REALISED["FP"]),              # never below pre-inactives
    ])
    def test_questionable(self, practice, pattern, expected):
        got = confirmed_active_mult("Questionable", practice, pattern)
        assert got == expected
        assert got >= practice_adjusted_mult("Questionable", practice, pattern)

    def test_values_are_the_played_only_shares(self):
        assert CONFIRMED_ACTIVE_REALISED == {"LP": 0.95, "DNP": 0.82, "NONE": 0.92}

    def test_other_tags(self):
        assert confirmed_active_mult(None, None) == 1.0
        assert confirmed_active_mult("Active", "DNP") == 1.0
        assert confirmed_active_mult("Doubtful", "DNP") == CONFIRMED_ACTIVE_REALISED["DNP"]
        # A stale Out the gameday note overrides.
        assert confirmed_active_mult("Out", None) == CONFIRMED_ACTIVE_REALISED["DNP"]
        assert confirmed_active_mult("Unknown", None) == projections.UNCERTAIN_MULT

    def test_sleeper_share(self):
        assert confirmed_active_blend_mult("Questionable", "LP") == 1.0
        assert confirmed_active_blend_mult("Questionable", None) == 1.0
        assert confirmed_active_blend_mult("Questionable", "DNP", "DNP-DNP") == round(
            0.82 / projections.SLEEPER_QUESTIONABLE_PRICED, 2)
        assert confirmed_active_blend_mult(None, None) == 1.0


class TestGamedayIndex:
    def test_inactive_wins_over_active(self):
        official = {
            "confirmed_active": [
                {"player_name": "Mike Evans", "team_id": "TB", "note": "is active",
                 "source": gi.SOURCE_ESPN_NOTE},
                {"player_name": "Zay Flowers", "team_id": "BAL", "note": "is active"}],
            "inactives": [{"player_name": "Zay Flowers", "team_id": "BAL",
                           "source": gi.SOURCE_SLEEPER}],
        }
        idx = gi.gameday_index(official)
        assert idx[("mike evans", "TB")]["status"] == "active"
        assert idx[("zay flowers", "BAL")]["status"] == "inactive"

    def test_empty(self):
        assert gi.gameday_index(None) == {} and gi.gameday_index({}) == {}


class _Db:
    def __init__(self, kickoffs):
        self.kickoffs = kickoffs

    def get_week_kickoffs(self, season, week):
        return self.kickoffs


class TestGamedayStatuses:
    def test_priced_teams_from_the_cached_schedule(self):
        db = _Db({"TB": KICKOFF.isoformat(), "BAL": (KICKOFF + timedelta(hours=7)).isoformat(),
                  "KC": (KICKOFF - timedelta(days=3)).isoformat()})
        assert gi.priced_teams(db, 2026, 6, KICKOFF - timedelta(hours=3)) == set()
        assert gi.priced_teams(db, 2026, 6, KICKOFF - timedelta(minutes=80)) == {"TB"}
        assert gi.priced_teams(db, 2026, 6, KICKOFF + timedelta(hours=1)) == {"TB"}
        assert gi.priced_teams(None, 2026, 6) == set()
        assert gi.priced_teams(db, None, None) == set()

    @pytest.mark.asyncio
    async def test_no_window_fetches_nothing(self, monkeypatch):
        fetch = AsyncMock()
        monkeypatch.setattr(gi, "get_official_inactives", fetch)
        db = _Db({"TB": KICKOFF.isoformat()})
        assert await gi.gameday_statuses(db, 2026, 6, now=KICKOFF - timedelta(hours=5)) == {}
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_reads_once_and_caches(self, monkeypatch):
        monkeypatch.setattr(gi, "_gameday_cache", {})
        official = {"confirmed_active": [{"player_name": "Mike Evans", "team_id": "TB"}],
                    "inactives": [{"player_name": "Jalen McMillan", "team_id": "TB"},
                                  {"player_name": "Not Yet", "team_id": "BAL"}]}
        fetch = AsyncMock(return_value=official)
        monkeypatch.setattr(gi, "get_official_inactives", fetch)
        db = _Db({"TB": KICKOFF.isoformat(), "BAL": (KICKOFF + timedelta(hours=7)).isoformat()})
        now = KICKOFF - timedelta(minutes=60)
        out = await gi.gameday_statuses(db, 2026, 6, now=now)
        # Only the teams in a priced phase.
        assert set(out) == {("mike evans", "TB"), ("jalen mcmillan", "TB")}
        await gi.gameday_statuses(db, 2026, 6, now=now + timedelta(minutes=2))
        assert fetch.await_count == 1
        await gi.gameday_statuses(db, 2026, 6, now=now + gi.GAMEDAY_CACHE_TTL + timedelta(minutes=1))
        assert fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_a_feed_failure_is_no_override(self, monkeypatch):
        monkeypatch.setattr(gi, "_gameday_cache", {})
        monkeypatch.setattr(gi, "get_official_inactives", AsyncMock(side_effect=RuntimeError("x")))
        db = _Db({"TB": KICKOFF.isoformat()})
        assert await gi.gameday_statuses(db, 2026, 6, now=KICKOFF - timedelta(minutes=30)) == {}


class TestQbCoupling:
    def test_sit_weight_reads_the_decision_first(self):
        practice = {"days": [{"day": "Wed", "status": "DNP"}, {"day": "Thu", "status": "DNP"}]}
        assert qb_coupling.starter_sit_weight("Questionable", practice)["weight"] \
            == qb_coupling.QUESTIONABLE_DNP_WEIGHT
        active = qb_coupling.starter_sit_weight("Questionable", practice, gameday="active")
        assert active == {"weight": 0.0, "basis": "gameday", "detail": "active"}
        inactive = qb_coupling.starter_sit_weight(None, gameday="inactive")
        assert inactive["weight"] == qb_coupling.OUT_WEIGHT and inactive["basis"] == "gameday"

    def test_confirmed_active_starter_cuts_no_receiver(self):
        depth, status_of = _depth("Doubtful")
        assert qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", status_of)
        gameday = {("starter qb", "TB"): {"status": "active"}}
        wrapped = projections._with_gameday(status_of, gameday)
        assert wrapped("Starter Qb", "TB") == "Active"
        assert qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", wrapped) is None

    def test_inactive_starter_counts_in_full(self):
        depth, status_of = _depth(None)       # no tag all week
        assert qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", status_of) is None
        wrapped = projections._with_gameday(status_of, {("starter qb", "TB"): {"status": "inactive"}})
        ctx = qb_coupling.receiver_context(depth, "TB", "WR", "Wide One", wrapped)
        assert ctx["starter_sit_weight"] == qb_coupling.OUT_WEIGHT
        assert ctx["starter_sit_basis"] == "gameday"
        assert ctx["sleeper_mult"] == qb_coupling.RECEIVER_MULT["WR"]["low"]

    def test_wrapper_keeps_the_lookup_attributes(self):
        def status_of(name, team):
            return "Questionable"
        status_of.news = {"x": 1}
        wrapped = projections._with_gameday(status_of, {})
        assert wrapped("Anyone", "TB") == "Questionable"
        assert wrapped.news == {"x": 1} and wrapped.gameday("Anyone", "TB") is None


def _q(practice=None, pattern=None, status="Questionable"):
    return {**PLAYERS[0], "injury": {"status": status, "practice_status": practice,
                                     "practice_pattern": pattern}}


async def _project(monkeypatch, players, gameday):
    monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_payload())))
    monkeypatch.setattr(opportunity_tools, "_fetch_game_logs", AsyncMock(return_value=_logs()))
    monkeypatch.setattr(projections, "_safe_gameday", AsyncMock(return_value=gameday))
    return (await _engine().project_many(players, scoring="ppr", season=2026,
                                         week=6))["projections"]


ACTIVE = {("wide receiver", "BUF"): {"status": "active", "note": "is active for Sunday",
                                     "source": gi.SOURCE_ESPN_NOTE}}
INACTIVE = {("wide receiver", "BUF"): {"status": "inactive", "note": "is inactive",
                                       "source": gi.SOURCE_ESPN_NOTE}}


class TestProjectionOverride:
    @pytest.mark.asyncio
    async def test_confirmed_active_lifts_a_questionable_player(self, monkeypatch):
        before = (await _project(monkeypatch, [_q("LP")], {}))[0]
        after = (await _project(monkeypatch, [_q("LP")], ACTIVE))[0]
        healthy = (await _project(monkeypatch, [PLAYERS[0]], {}))[0]
        assert before["breakdown"]["injury_mult"] == QUESTIONABLE_REALISED["LP"]
        assert after["breakdown"]["injury_mult"] == CONFIRMED_ACTIVE_REALISED["LP"]
        assert after["breakdown"]["practice_blend_mult"] == 1.0
        assert after["gameday_status"] == "active" and after["gameday_note"]
        assert after["injury_status"] == "Questionable"
        assert before["projected_points"] < after["projected_points"] <= healthy["projected_points"]
        # ~0.93 of healthy: the might-sit discount is gone.
        assert after["projected_points"] >= 0.9 * healthy["projected_points"]
        assert "gameday_status" not in before

    @pytest.mark.asyncio
    async def test_officially_inactive_is_zero(self, monkeypatch):
        p = (await _project(monkeypatch, [_q("FP")], INACTIVE))[0]
        assert p["projected_points"] == 0.0 and p["floor"] == 0.0 and p["ceiling"] == 0.0
        assert p["injury_status"] == "Inactive" and p["reported_injury_status"] == "Questionable"
        assert p["gameday_status"] == "inactive"
        # Even a healthy-tagged player.
        p = (await _project(monkeypatch, [PLAYERS[0]], INACTIVE))[0]
        assert p["projected_points"] == 0.0

    @pytest.mark.asyncio
    async def test_doubtful_confirmed_active_escapes_the_cap(self, monkeypatch):
        before = (await _project(monkeypatch, [_q(status="Doubtful")], {}))[0]
        after = (await _project(monkeypatch, [_q(status="Doubtful")], ACTIVE))[0]
        assert before["projected_points"] <= 0.5
        assert after["breakdown"]["injury_mult"] == CONFIRMED_ACTIVE_REALISED["DNP"]
        assert after["projected_points"] > 5 * max(before["projected_points"], 0.1)

    @pytest.mark.asyncio
    async def test_no_decision_changes_nothing(self, monkeypatch):
        a = (await _project(monkeypatch, [_q("LP")], {}))[0]
        other = {("someone else", "BUF"): {"status": "active"}}
        b = (await _project(monkeypatch, [_q("LP")], other))[0]
        assert a["projected_points"] == b["projected_points"]


class TestStartSitReadsTheDecision:
    def _analysis(self, status):
        return lo.PlayerAnalysis(player_name="X", player_id="1", position="WR", team="BUF", opponent="MIA",
                                 injury_status=status)

    def test_inactive(self):
        a = self._analysis("Questionable")
        reason = lo._gameday_reason(a, {"gameday_status": "inactive",
                                        "reported_injury_status": "Questionable",
                                        "gameday_note": "X is inactive"})
        assert a.injury_status == "Inactive" and "inactive" in reason.lower()
        assert lo.LineupOptimizer.__new__(lo.LineupOptimizer).determine_decision(
            12.0, "WR", lo.injury_score(a.injury_status), injury_status=a.injury_status) == "must_sit"

    def test_active_doubtful_is_no_longer_capped(self):
        a = self._analysis("Doubtful")
        reason = lo._gameday_reason(a, {"gameday_status": "active"})
        assert a.injury_status == "Active" and "Confirmed active (was Doubtful)" in reason
        assert lo.LineupOptimizer.__new__(lo.LineupOptimizer).determine_decision(
            14.0, "WR", lo.injury_score(a.injury_status), injury_status=a.injury_status) \
            not in ("sit", "must_sit")

    def test_no_decision(self):
        a = self._analysis("Questionable")
        assert lo._gameday_reason(a, {}) is None and a.injury_status == "Questionable"
