"""The 2026-10 calibration of the trend heuristics: a back's gained role priced
only when no teammate's absence explains it, the returning-teammate signal
sized to the realised drop, and the eval helpers behind the numbers."""
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import opportunity_tools, projections, role_shift, value_trajectory
from nfl_mcp import sleeper_projections as sp
from tests.test_qb_coupling_news_signals import _engine_with
from tests.test_role_shift import _row
from tests.test_vacated_volume import _games


# --------------------------------------------------------------------------
# role_shift: the gained role's candidate multiplier
# --------------------------------------------------------------------------
class TestGainMultiplier:
    def _love(self):
        """Carries share 43% -> 66% over two games (Jeremiyah Love, 2026)."""
        return [_row(1, 50, 43, 6), _row(2, 52, 43, 6), _row(3, 70, 65, 7), _row(4, 72, 67, 7)]

    def test_a_backs_two_week_gain_has_a_candidate_multiplier(self):
        got = role_shift.classify(self._love(), "RB")
        assert got["role_trend"] == "role_up" and got["recent_weeks"] == 2
        # Sized on the volume shares only: carries +53% (targets did not move).
        assert got["gain_multiplier"] == pytest.approx(
            1 + role_shift.UP_STRENGTH["RB"] * (66 - 43) / 43, abs=0.002)
        # Never priced by the read itself: the projection gates it.
        assert got["role_multiplier"] == 1.0

    def test_capped(self):
        rows = [_row(1, 20, 10, 2), _row(2, 20, 10, 2), _row(3, 95, 90, 20), _row(4, 95, 90, 20)]
        assert role_shift.classify(rows, "RB")["gain_multiplier"] == role_shift.MAX_MULTIPLIER

    def test_one_week_gains_receivers_and_tight_ends_have_none(self):
        one = [_row(1, 50, 43, 6), _row(2, 52, 43, 6), _row(3, 51, 44, 6), _row(4, 75, 70, 7)]
        got = role_shift.classify(one, "RB")
        assert got["role_trend"] == "role_up" and got["recent_weeks"] == 1
        assert got["gain_multiplier"] == 1.0
        wr = [_row(w, 80, None, t) for w, t in ((1, 14), (2, 15), (3, 26), (4, 27))]
        got = role_shift.classify(wr, "WR")
        assert got["role_trend"] == "role_up" and got["gain_multiplier"] == 1.0

    def test_a_lost_role_has_no_gain_and_the_new_floor(self):
        rows = [_row(w, 95, 90, 20) for w in (1, 2, 3)] + [_row(4, 5, 2, 0), _row(5, 5, 2, 0)]
        got = role_shift.classify(rows, "RB")
        assert got["gain_multiplier"] == 1.0
        assert got["role_multiplier"] == role_shift.MIN_MULTIPLIER == 0.85


# --------------------------------------------------------------------------
# projections: the gate
# --------------------------------------------------------------------------
def _room(lead_weeks):
    """A rising back (carries 6 -> 18 from week 3) and the lead back, who
    played `lead_weeks`."""
    rising = [{**g, "team": "CHI"} for g in _games([1, 2], carries=6.0) + _games([3, 4], carries=18.0)]
    lead = [{**g, "team": "CHI"} for g in _games(sorted(lead_weeks), carries=16.0)]
    return {"r": {"player_id": "r", "name": "Rising Back", "position": "RB", "team": "CHI",
                  "games": rising},
            "l": {"player_id": "l", "name": "Lead Back", "position": "RB", "team": "CHI",
                  "games": lead}}


_DEPTH = {("CHI", "RB"): [{"name": "Lead Back", "position_rank": 20, "player_id": "l"},
                          {"name": "Rising Back", "position_rank": 40, "player_id": "r"}]}


class TestGainExplainedByAbsence:
    def _index(self, lead_weeks):
        return opportunity_tools.build_name_index(_room(lead_weeks))

    def test_a_lead_back_who_missed_the_gain_weeks_explains_it(self):
        # Emanuel Wilson: the gain came while Charbonnet and Price were out.
        got = projections._gain_explained_by_absence(
            _DEPTH, "CHI", "RB", 40, self._index([1, 2]), "Rising Back", 5, 3)
        assert got == ["Lead Back"]

    def test_one_missed_gain_week_is_enough(self):
        got = projections._gain_explained_by_absence(
            _DEPTH, "CHI", "RB", 40, self._index([1, 2, 4]), "Rising Back", 5, 3)
        assert got == ["Lead Back"]

    def test_a_lead_back_on_the_field_throughout_does_not(self):
        got = projections._gain_explained_by_absence(
            _DEPTH, "CHI", "RB", 40, self._index([1, 2, 3, 4]), "Rising Back", 5, 3)
        assert got == []

    def test_a_teammate_never_on_the_field_before_the_gain_does_not(self):
        got = projections._gain_explained_by_absence(
            _DEPTH, "CHI", "RB", 40, self._index([]), "Rising Back", 5, 3)
        assert got == []

    def test_snaps_count_as_played(self):
        # No stat line in week 3 but offensive snaps (Sleeper): he played.
        got = projections._gain_explained_by_absence(
            _DEPTH, "CHI", "RB", 40, self._index([1, 2, 4]), "Rising Back", 5, 3,
            played_weeks=lambda pid: {3} if pid == "l" else set())
        assert got == []

    def test_no_data_is_none(self):
        assert projections._gain_explained_by_absence(
            _DEPTH, "CHI", "RB", 40, {}, "Rising Back", 5, 3) is None
        assert projections._gain_explained_by_absence(
            _DEPTH, "CHI", "RB", 40, self._index([1, 2]), "Rising Back", 5, None) is None


class TestRoleGain:
    ROLE = {"role_trend": "role_up", "gain_multiplier": 1.05}

    def test_priced_when_nothing_explains_it(self):
        assert projections._role_gain(self.ROLE, [], [], []) == {
            "multiplier": 1.05, "priced": True, "explained_by": []}

    @pytest.mark.parametrize("explained, returning, out", [
        (["Lead Back"], [], []),
        ([], [{"name": "Zach Charbonnet"}], []),
        ([], [], ["Rico Dowdle"]),
        (None, [], []),  # nothing to check against: not priced either
    ])
    def test_not_priced_when_an_absence_explains_it(self, explained, returning, out):
        got = projections._role_gain(self.ROLE, explained, returning, out)
        assert got["priced"] is False and got["multiplier"] == 1.0

    def test_no_gain_no_entry(self):
        assert projections._role_gain({"role_trend": "role_down", "gain_multiplier": 1.0},
                                      [], [], []) is None
        assert projections._role_gain({"role_trend": "role_up", "gain_multiplier": 1.0},
                                      [], [], []) is None


def _payload():
    return [{"player_id": "r", "team": "CHI", "opponent": "MIN",
             "player": {"first_name": "Rising", "last_name": "Back", "position": "RB",
                        "team": "CHI"},
             "stats": {"rush_att": 15.0, "rush_yd": 60.0, "pts_ppr": 10.0}}]


_VALUES = [{"name": "Lead Back", "team": "CHI", "position": "RB", "position_rank": 20,
            "value": 3000, "player_id": "l"}]


class TestOnTheBlend:
    async def _project(self, monkeypatch, lead_weeks):
        monkeypatch.setattr(opportunity_tools, "_fetch_game_logs",
                            AsyncMock(return_value=_room(lead_weeks)))
        monkeypatch.setattr(sp, "_fetch", AsyncMock(return_value=sp._index(_payload())))
        player = {"name": "Rising Back", "position": "RB", "team": "CHI", "opponent": "MIN",
                  "player_id": "r"}
        out = await _engine_with([], _VALUES).project_many([player], scoring="ppr",
                                                           season=2026, week=5)
        return out["projections"][0]

    @pytest.mark.asyncio
    async def test_a_gain_with_the_lead_back_playing_is_priced(self, monkeypatch):
        p = await self._project(monkeypatch, [1, 2, 3, 4])
        assert p["role_trend"] == "role_up"
        gain = p["role_gain"]
        assert gain["priced"] and gain["multiplier"] > 1.0 and gain["explained_by"] == []
        # On this week's Sleeper share; `role_multiplier` (which ROS carries
        # into later weeks) stays the lost-role one.
        assert p["breakdown"]["role_mult"] == gain["multiplier"]
        assert p["role_multiplier"] == 1.0
        assert p["projected_points"] == round(
            0.25 * p["model_projection"] + 0.75 * p["sleeper_projection"] * gain["multiplier"], 1)

    @pytest.mark.asyncio
    async def test_a_gain_while_the_lead_back_was_out_is_not(self, monkeypatch):
        p = await self._project(monkeypatch, [1, 2])
        assert p["role_trend"] == "role_up"
        assert p["role_gain"]["priced"] is False
        assert "Lead Back" in p["role_gain"]["explained_by"]
        assert p["role_multiplier"] == 1.0 and p["breakdown"]["role_mult"] == 1.0
        assert p["projected_points"] == round(
            0.25 * p["model_projection"] + 0.75 * p["sleeper_projection"], 1)


# --------------------------------------------------------------------------
# value_trajectory: the returning-teammate signal at its realised size
# --------------------------------------------------------------------------
def _back_again(position):
    return {"player": "Fill In", "player_id": "x", "position": position, "team": "MIN",
            "per_game": 8.0, "per_game_until_return": 9.4, "per_game_recent": 10.7,
            "weekly_points": dict.fromkeys(range(5, 18), 8.0), "expected_absence_games": 0,
            "returning_teammates": [{"name": "Star", "games_until_return": 0,
                                     "expected_return_week": 5, "status": "Questionable"}]}


class TestReturningScale:
    @pytest.mark.parametrize("position", ["RB", "WR", "TE"])
    def test_scaled_by_position(self, position):
        t = value_trajectory.assess(_back_again(position), week=5)
        raw = (8.0 - 10.7) / 10.7
        sig = next(s for s in t["signals"] if s["kind"] == "returning_teammate")
        assert sig["change"] == round(value_trajectory.RETURNING_CHANGE_SCALE[position] * raw, 3)
        # The rate it reports is not scaled.
        assert t["expected_value_change"]["per_game"] == pytest.approx(8.0 - 10.7)

    def test_receivers_drop_least(self):
        s = value_trajectory.RETURNING_CHANGE_SCALE
        assert s["WR"] < s["RB"] and s["WR"] < s["TE"] and max(s.values()) <= 1.0

    def test_keep_weight_is_the_calibrated_one(self):
        assert projections.RETURNING_KEEP_WEIGHT == 0.6


# --------------------------------------------------------------------------
# Eval helpers
# --------------------------------------------------------------------------
class TestSleeperBlendHelpers:
    def _g(self, week, carries=10.0):
        return {"week": week, "targets": 0.0, "carries": carries, "attempts": 0.0, "ppr": carries}

    def test_absent_mates(self):
        from evals.backtest.sleeper_blend import _absent_mates
        prior = [self._g(w, 6.0) for w in (1, 2, 3, 4)]
        mates = {"me": {g["week"]: g for g in prior},
                 "lead": {1: self._g(1, 16), 2: self._g(2, 16)},
                 "rookie": {4: self._g(4, 2)}}
        ranks = {"lead": 20, "me": 40}
        assert _absent_mates("me", prior, [3, 4], mates, ranks) == ["lead"]
        mates["lead"].update({3: self._g(3, 16), 4: self._g(4, 16)})
        assert _absent_mates("me", prior, [3, 4], mates, ranks) == []

    def test_horizon_actual(self):
        from evals.backtest.sleeper_blend import _horizon_actual
        by_week = {w: {"ppr": float(w)} for w in (3, 4, 6, 7, 8, 9)}
        assert _horizon_actual(by_week, 4) == pytest.approx((4 + 6 + 7 + 8) / 4)
        assert _horizon_actual(by_week, 10) is None

    def test_cross_position_returning_weeks(self):
        from evals.backtest.sleeper_blend import _returning_weeks
        prior = [{"week": w, "targets": 8.0, "carries": 0.0} for w in (1, 2, 3, 4)]
        wr = {w: {"week": w, "targets": 9.0, "carries": 0.0} for w in (1, 2, 5)}
        got = _returning_weeks("te", prior, 5, "MIN", "TE", {"wr": wr}, {},
                               ahead_of=lambda m: m == "wr")
        assert got == {3, 4}
        assert _returning_weeks("te", prior, 5, "MIN", "TE", {"wr": wr}, {},
                                ahead_of=lambda m: False) == set()


class TestPracticeBacktestHelpers:
    def test_bucket_of(self):
        from evals.backtest.practice_backtest import bucket_of
        assert bucket_of(None) == "none"
        assert bucket_of({"status": "Q", "practice": "LP"}) == "Q/LP"
        assert bucket_of({"status": "", "practice": "DNP"}) == "-/DNP"

    def test_load_injuries_parses_the_release(self, tmp_path, monkeypatch):
        from evals.backtest import practice_backtest as pb
        (tmp_path / "injuries_2024.csv").write_text(
            "season,game_type,team,week,gsis_id,position,full_name,first_name,last_name,"
            "report_primary_injury,report_secondary_injury,report_status,"
            "practice_primary_injury,practice_secondary_injury,practice_status,date_modified\n"
            "2024,REG,ARI,1,00-1,WR,A B,A,B,Ankle,,Questionable,Ankle,,"
            "Limited Participation in Practice,2024-09-06T19:05:21Z\n"
            "2024,REG,ARI,2,00-2,RB,C D,C,D,Knee,,,Knee,,Full Participation in Practice,x\n"
            "2024,POST,ARI,19,00-1,WR,A B,A,B,Ankle,,Out,Ankle,,"
            "Did Not Participate In Practice,x\n")
        monkeypatch.setattr(pb, "_CACHE_DIR", str(tmp_path))
        got = pb.load_injuries(2024)
        assert got == {(1, "00-1"): {"status": "Q", "practice": "LP"},
                       (2, "00-2"): {"status": "", "practice": "FP"}}

    def test_implied_and_interval(self):
        from evals.backtest.practice_backtest import implied
        rows = [{"actual": 7.0, "model": 10.0}] * 20 + [{"actual": 0.0, "model": 10.0}] * 5
        m, lo, hi = implied(rows, ref=1.0)
        assert m == pytest.approx(0.56) and lo <= m <= hi

    def test_live_total_matches_the_blend(self):
        from evals.backtest.practice_backtest import _live_total
        assert _live_total("Q/LP") == pytest.approx(
            0.25 * projections.QUESTIONABLE_BY_PRACTICE["LP"]
            + 0.75 * projections.PRACTICE_BLEND_MULT["LP"]
            * projections.SLEEPER_QUESTIONABLE_PRICED)
        # ...which lands on the measured share (0.72) within rounding.
        assert _live_total("Q/LP") == pytest.approx(
            projections.QUESTIONABLE_REALISED["LP"], abs=0.01)
        assert _live_total("Q/DNP") == pytest.approx(
            projections.QUESTIONABLE_REALISED["DNP"], abs=0.01)
        assert _live_total("-/FP") is None

    def test_wilson(self):
        from evals.backtest.practice_backtest import _wilson
        lo, hi = _wilson(15, 32)
        assert 0.3 < lo < 15 / 32 < hi < 0.65


class TestSignalHistoryHelpers:
    def test_a_player_who_sat_the_latest_week_is_scored(self):
        from evals.backtest import signal_history as sh
        recs = [{"player": "Brock Bowers", "team": "LV", "week": w, "ppr": 10.0} for w in (1, 2, 3)]
        recs.append({"player": "Someone Else", "team": "LV", "week": 4, "ppr": 9.0})
        assert sh.ratios(recs)[(4, "LV", "brock bowers")] == 0.0

    def test_low_volume_players_are_not_scored(self):
        from evals.backtest import signal_history as sh
        recs = [{"player": "Camp Body", "team": "LV", "week": w, "ppr": 1.0} for w in (1, 2, 3)]
        assert sh.ratios(recs) == {}

    def test_a_designation_read_late_still_labels_the_week(self):
        from evals.backtest import signal_history as sh
        ko = datetime(2026, 9, 27, 20, 25, tzinfo=UTC)
        rows = [
            {"week": 3, "team": "LV", "name_key": "brock bowers", "date": "2026-09-25",
             "status": "LP", "source": "nfl.com", "game_status": None,
             "recorded_at": "2026-09-25T21:00:00+00:00"},
            # The page re-read on Tuesday carries Friday's designation.
            {"week": 3, "team": "LV", "name_key": "brock bowers", "date": "2026-09-25",
             "status": "LP", "source": "nfl.com", "game_status": "Questionable",
             "recorded_at": "2026-09-29T12:00:00+00:00"},
        ]
        got = sh.practice_at_kickoff(rows, {(3, "LV"): ko})
        assert got[(3, "LV", "brock bowers")]["game_status"] == "Questionable"

    def test_transitions(self):
        from evals.backtest import signal_history as sh

        def day(d, status, game_status=None):
            return {"week": 4, "team": "LV", "name_key": "x", "date": f"2026-10-0{d}",
                    "status": status, "source": "nfl.com", "game_status": game_status,
                    "recorded_at": f"2026-10-0{d}T20:00:00+00:00"}
        got = sh.transitions([day(1, "DNP"), day(2, "LP"), day(3, "LP", "Questionable")])
        assert got == {"DNP": {"LP": 1}}
        # Not questionable in the end: not counted.
        assert sh.transitions([day(1, "DNP"), day(2, "LP"), day(3, "FP")]) == {}

    def test_bootstrap_ci_brackets_the_ratio(self):
        from evals.backtest.signal_history import bootstrap_ci
        lo, hi = bootstrap_ci([0.5] * 10 + [0.0] * 5, [1.0] * 20)
        assert lo <= 1 / 3 <= hi
