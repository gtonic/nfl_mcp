"""2026-10-18 (week 6) regressions.

A) A.J. Brown was activated from injured reserve on Saturday and listed
   questionable for Sunday; Sleeper still said "IR" -- he projected 0, the
   briefing listed him unavailable and the IR audit suggested "move_to_ir"
   (with the four-week minimum stint overriding the report).
B) "fantasy managers need a backup plan in case Freiermuth is ultimately
   ruled out against Tampa Bay" was read as ``ruled_out``.
"""
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from nfl_mcp import late_risk, news_signals
from nfl_mcp.injury_match import build_injury_index, lookup_injury, resolve_injury
from nfl_mcp.ir_audit import audit_roster
from nfl_mcp.ros import IR_MIN_WEEKS, expected_absence

NOW = datetime(2026, 10, 17, 21, 0, tzinfo=UTC)
ACTIVATED = ("The Patriots activated Brown (ankle) from injured reserve Saturday, but he's "
             "still listed as questionable for Sunday's game against the Jets.")
FREIERMUTH = ("Coming off a two touchdown performance in Week 5, fantasy managers need a "
              "backup plan in case Freiermuth is ultimately ruled out against Tampa Bay.")
ROPEWAY = {"reserve_slots": 1}


def _brown(status="IR"):
    return {"full_name": "A.J. Brown", "team_id": "NE", "position": "WR",
            "raw": json.dumps({"injury_status": status})}


def _report(status="Questionable", description=ACTIVATED):
    return {"player_name": "A.J. Brown", "team_id": "NE", "position": "WR",
            "injury_status": status, "injury_description": description,
            "return_date": "2026-10-18"}


class TestConditionalAvailability:
    @pytest.mark.parametrize("text", [
        FREIERMUTH,
        "Managers should have a backup plan in case he is ruled out Sunday.",
        "If he is ruled out, Jonnu Smith would start.",
        "Should he be ruled out, Jonnu Smith would start.",
        "Should Freiermuth ultimately be ruled out, Jonnu Smith would start.",
        "Were he to be ruled out, Jonnu Smith would start.",
        "Have a backup plan in case Freiermuth is not expected to play.",
    ])
    def test_a_hypothetical_is_conditional(self, text):
        hits = [h for h in news_signals.classify(text, "pat freiermuth", None, "freiermuth")
                if h["flag"] in ("ruled_out", "unlikely_to_play")]
        assert hits and all(h.get("conditional") for h in hits)

    @pytest.mark.parametrize("text", [
        "Freiermuth has been ruled out against Tampa Bay.",
        "In case you missed it, Freiermuth has been ruled out.",
    ])
    def test_a_report_is_not(self, text):
        hits = news_signals.classify(text, "pat freiermuth", None, "freiermuth")
        assert [h["flag"] for h in hits if not h.get("conditional")] == ["ruled_out"]

    def test_build_index_drops_it(self):
        news = [{"player_name": "Pat Freiermuth", "team_id": "PIT", "text": FREIERMUTH,
                 "date_reported": NOW.isoformat(), "source": "nbc"}]
        index = news_signals.build_index([], now=NOW, news=news)
        flags = news_signals.signals_for(index, "Pat Freiermuth", "PIT")
        assert "ruled_out" not in {f["flag"] for f in flags}

    def test_late_risk_ignores_a_conditional_flag(self):
        player = {"injury_status": "Questionable", "practice_status": "LP",
                  "practice_pattern": "LP-LP-LP",
                  "news_flags": [{"flag": "ruled_out", "conditional": True}]}
        p, basis = late_risk.play_probability(player)
        assert p > late_risk.NEWS_CAP["ruled_out"] and "ruled out" not in basis


class TestActivationNews:
    @pytest.mark.parametrize("text", [
        ACTIVATED,
        "Patriots activated A.J. Brown (ankle) from injured reserve.",
        "Brown was activated from injured reserve this week and will suit up versus the Jets.",
    ])
    def test_activation(self, text):
        assert news_signals.activation_in(text)

    @pytest.mark.parametrize("text", [
        "Brown (ankle) will likely be activated from injured reserve to play Sunday.",
        "Assuming he's activated from IR, Brown will slot right back into the WR1 spot.",
        "The Patriots designated Brown for return from injured reserve earlier Wednesday.",
        "Brown is expected to be activated from IR on Saturday.",
        "Brown has not been activated from IR.",
        "The Patriots will need to activate him from injured reserve Saturday if he plays.",
    ])
    def test_not_an_activation(self, text):
        assert not news_signals.activation_in(text)

    def _flags(self, *items):
        news = [{"player_name": "A.J. Brown", "team_id": "NE", "text": t,
                 "date_reported": d.isoformat(), "source": "espn_fantasy"} for t, d in items]
        index = news_signals.build_index([], now=NOW, news=news)
        return news_signals.signals_for(index, "A.J. Brown", "NE")

    def test_reserve_lifted_by_fresh_news(self):
        flags = self._flags(
            (ACTIVATED, NOW - timedelta(hours=1)),
            ("After opening his 21-day practice window Wednesday, Brown logged limited work.",
             NOW - timedelta(minutes=5)))
        assert news_signals.reserve_lifted(flags, NOW)

    def test_not_lifted_by_a_window_opening_or_a_later_placement(self):
        assert not news_signals.reserve_lifted(self._flags(
            ("The Patriots opened Brown's 21-day practice window Wednesday.", NOW)), NOW)
        flags = self._flags((ACTIVATED, NOW - timedelta(days=3)),
                            ("The Patriots placed Brown on injured reserve Friday.", NOW))
        assert not news_signals.reserve_lifted(flags, NOW)
        stale = self._flags((ACTIVATED, NOW - timedelta(days=9)))
        assert not news_signals.reserve_lifted(stale, NOW)


class TestStatus:
    def test_report_designation_wins_after_an_activation(self):
        injury = resolve_injury(_brown(), build_injury_index([_report()]), "NE")
        assert injury["status"] == "Questionable"
        assert injury["reserve_lifted"] is True and injury["sleeper_status"] == "IR"

    def test_activation_from_news_flags(self):
        report = _report(description="Brown (ankle) was a limited practice participant Friday.")
        flags = [{"flag": "designated_to_return", "activated": True,
                  "date_reported": datetime.now(UTC).isoformat()}]
        injury = resolve_injury(_brown(), build_injury_index([report]), "NE", flags)
        assert injury["status"] == "Questionable"

    def test_without_an_activation_ir_still_wins(self):
        report = _report(description="Brown (ankle) was a limited practice participant Friday.")
        injury = resolve_injury(_brown(), build_injury_index([report]), "NE")
        assert injury["status"] == "IR" and "reserve_lifted" not in injury

    def test_lookup_injury_reads_the_news(self):
        class DB:
            def find_player_injury(self, name, team, max_age_hours=None):
                return _report(description="Brown (ankle) is questionable for Sunday.")

            def search_athletes_by_name(self, name, limit=10):
                return [_brown()]

            def get_all_current_injuries(self):
                return [self.find_player_injury("A.J. Brown", "NE")]

            def get_player_news(self, since=None, teams=None):
                return [{"player_name": "A.J. Brown", "team": "NE", "source": "nbc",
                         "headline": "Patriots activated A.J. Brown (ankle) from injured reserve.",
                         "text": "Brown makes his return from a four-game absence.",
                         "published_at": datetime.now(UTC).isoformat()}]

        assert lookup_injury(DB(), "A.J. Brown", "NE")["status"] == "Questionable"

    def test_no_minimum_stint_after_an_activation(self):
        weeks, why = expected_absence("IR", ACTIVATED, "2026-10-18", today=date(2026, 10, 17))
        assert weeks == 0 and "activated" in why
        weeks, why = expected_absence("IR", "Brown (ankle) was placed on IR.", "2026-10-18",
                                      today=date(2026, 10, 17))
        assert weeks == IR_MIN_WEEKS and "is earlier" in why


class TestIrAudit:
    def test_activated_player_is_not_moved_to_ir(self):
        audit = audit_roster({"players": ["5859"], "reserve": []}, ROPEWAY,
                             {"5859": _brown()}, build_injury_index([_report()]))
        assert audit["moves"] == []

    def test_activated_player_in_the_ir_slot_is_to_be_activated(self):
        audit = audit_roster({"players": ["5859"], "reserve": ["5859"]}, ROPEWAY,
                             {"5859": _brown()}, build_injury_index([_report()]))
        [move] = audit["moves"]
        assert move["action"] == "activate" and "activated him" in move["reason"]
        assert not move["expected_weeks_out"]

    def test_ir_player_without_news_still_moves(self):
        report = _report(description="Brown (ankle) was placed on IR.", status="Out")
        audit = audit_roster({"players": ["5859"], "reserve": []}, ROPEWAY,
                             {"5859": _brown()}, build_injury_index([report]))
        assert [m["action"] for m in audit["moves"]] == ["move_to_ir"]
