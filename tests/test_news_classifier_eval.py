"""The news-text classifier (`news_signals.classify`) against hand-labelled
stored sentences (``tests/fixtures/news_labels.jsonl``, see
`evals.news_classifier_eval`), and the week-5 2026 cases that motivated the
rework: a plain return to practice is not a designation to return, losing
the work after a fumble is, a coach's-decision inactive is a healthy
scratch, and a conditional lead role is not a lead role.
"""
from datetime import UTC, datetime, timedelta

import pytest

from evals import news_classifier_eval as ev
from nfl_mcp import news_signals, value_trajectory
from tests.test_returning_teammates import _backfield, _project, _status

ENTRIES = ev.load()
DEV = [e for e in ENTRIES if e["split"] == "dev"]
HOLDOUT = [e for e in ENTRIES if e["split"] == "holdout"]

# Thresholds a pattern change must keep. Dev: the sentences the patterns
# were written against (2026-10-08: P 0.98 / R 0.96 overall). Held out:
# labelled before the classifier saw them -- precision held (1.00 on 6
# flags), recall is the honest gap (0.33: phrasings the vocabulary lacks).
DEV_MIN = {"precision": 0.95, "recall": 0.92}
DEV_FLAG_MIN = 0.85          # per flag with at least 5 gold labels
HOLDOUT_MIN = {"precision": 0.85, "recall": 0.30}
# One week of labels in the old classifier's terms: what this replaced.
BASELINE_DEV = {"precision": 0.55, "recall": 0.59}


def _flags(text, owner="owner", names=None, owner_last=""):
    return {h["flag"]: h for h in news_signals.classify(text, owner, names, owner_last)}


class TestLabelledSet:
    def test_fixture_shape(self):
        assert 250 <= len(ENTRIES) <= 400 and len(HOLDOUT) >= 60
        ids = [e["id"] for e in ENTRIES]
        assert len(ids) == len(set(ids))
        for e in ENTRIES:
            assert len(e["text"]) <= 400  # snippets, not articles
            for flag, who in e["labels"]:
                assert flag in news_signals.PATTERNS, flag
                assert who == e["owner"] or who in e["mates"], (e["id"], who)

    def test_dev_precision_recall(self):
        result = ev.score(DEV)
        assert result["_all"]["precision"] >= DEV_MIN["precision"], ev.table(result)
        assert result["_all"]["recall"] >= DEV_MIN["recall"], ev.table(result)
        for flag, c in result.items():
            if flag == "_all" or c["tp"] + c["fn"] < 5:
                continue
            assert c["precision"] is None or c["precision"] >= DEV_FLAG_MIN, (flag, c)
            assert c["recall"] >= DEV_FLAG_MIN, (flag, c)

    def test_holdout_precision_recall(self):
        result = ev.score(HOLDOUT)
        assert result["_all"]["precision"] >= HOLDOUT_MIN["precision"], ev.table(result)
        assert result["_all"]["recall"] >= HOLDOUT_MIN["recall"], ev.table(result)

    def test_better_than_the_baseline(self):
        result = ev.score(DEV)["_all"]
        assert result["precision"] > BASELINE_DEV["precision"] + 0.3
        assert result["recall"] > BASELINE_DEV["recall"] + 0.3


class TestWeekFiveCases:
    def test_pollard_resumed_practicing_is_practice_progress(self):
        flags = _flags("Tony Pollard (foot) resumed practicing on Thursday.")
        assert set(flags) == {"practice_progress"}

    @pytest.mark.parametrize("text", [
        "Swift (hip/knee) returned to practice on a limited basis Thursday.",
        "Dallas Goedert (knee) returned to practice Wednesday.",
    ])
    def test_returned_to_practice_is_not_designated(self, text):
        assert "designated_to_return" not in _flags(text)
        assert "practice_progress" in _flags(text)

    @pytest.mark.parametrize("text", [
        "The Seahawks designated Charbonnet (knee) to return to practice Wednesday.",
        "The Browns opened the 21-day practice window for Gabriel (back) on Tuesday.",
        "The Raiders activated Thornton from injured reserve Saturday.",
    ])
    def test_reserve_list_returns_are_designated(self, text):
        assert "designated_to_return" in _flags(text)

    def test_ir_placement_with_a_designation_is_not_a_return(self):
        assert _flags("Trost (hamstring) was placed on injured reserve with a designation "
                      "to return by the Rams on Sunday.") == {}

    def test_swift_lost_the_work_to_monangai(self):
        names = {"monangai": "kyle monangai", "kyle monangai": "kyle monangai"}
        hits = news_signals.classify(
            "Swift also lost a fumble in the third quarter and watched Monangai dominate "
            "touches the rest of the way.", "d'andre swift", names, "swift")
        got = {(h["flag"], h["about"]) for h in hits}
        assert got == {("benched", "d'andre swift"), ("lead_role", "kyle monangai")}

    @pytest.mark.parametrize("text", [
        "Swift didn't get another carry after his fumble in the second quarter.",
        "Swift was benched after fumbling in the second quarter.",
        "Swift didn't see the field again after his fumble.",
        "Swift saw his snaps dwindle after the second-quarter fumble.",
        "Swift lost work to Monangai after the fumble.",
    ])
    def test_losing_work_after_a_fumble(self, text):
        assert "benched" in _flags(text)

    def test_coach_decision_inactive_is_a_healthy_scratch_not_benched(self):
        flags = _flags("Milroe (coach's decision) is inactive for Sunday's game against "
                       "Washington.")
        assert set(flags) == {"inactive_healthy_scratch"}
        assert news_signals.EFFECTS["inactive_healthy_scratch"] == {
            "model_mult": 1.0, "confidence": 0}

    def test_injury_inactive_is_ruled_out(self):
        assert set(_flags("Mims (foot) is listed as inactive Sunday versus the Rams.")) == {
            "ruled_out"}

    def test_conditional_lead_role(self):
        names = {"hall": "breece hall", "allen": "braelon allen"}
        hits = news_signals.classify(
            "If Hall isn't ready to return in Week 5, Allen would retain the lead role in the "
            "backfield.", "braelon allen", names, "allen")
        assert hits == [{"flag": "lead_role", "about": "braelon allen", "conditional": True,
                         "snippet": hits[0]["snippet"]}]

    @pytest.mark.parametrize("text", [
        "If he can't go, MarShawn Lloyd and Kaleb Johnson will split the backfield.",
        "If Lance is not cleared to play, Uiagalelei would be the backup.",
        "Brown could have had an enhanced role if he was cleared to play in Week 4.",
        "Smith had a big game, and if he were to be ruled out, Wicks would see more targets.",
    ])
    def test_conditionals_are_marked(self, text):
        assert all(h.get("conditional") for h in news_signals.classify(text, "owner"))

    def test_an_indirect_question_is_not_a_condition(self):
        flags = _flags("It's unclear if the Chargers intend to keep up more of a timeshare.")
        assert "committee" in flags and not flags["committee"].get("conditional")

    def test_a_title_names_the_lead_back(self):
        names = {"taylor": "jonathan taylor", "jonathan taylor": "jonathan taylor",
                 "mcgowan": "seth mcgowan"}
        hits = news_signals.classify(
            "Seth McGowan is the lone depth option behind bell-cow running back Jonathan "
            "Taylor.", "dj giddens", names, "giddens")
        assert {(h["flag"], h["about"]) for h in hits} == {("lead_role", "jonathan taylor")}

    @pytest.mark.parametrize("text", [
        "His scoring floor should be elevated week-to-week as long as Allen plays.",
        "Bigsby doesn't seem likely to have a significant week-to-week role.",
        "McConkey may be more day-to-day than week-to-week.",
        "The touchdown was originally ruled out of bounds.",
        "He didn't record a catch Sunday prior to being ruled out against the Saints.",
        "The Eagles will play Week 5 with a banged-up group.",
        "His offensive snap count was fourth-most among the receivers.",
    ])
    def test_old_false_positives(self, text):
        assert _flags(text) == {}


class TestIndex:
    NOW = datetime(2026, 10, 8, 20, tzinfo=UTC)

    def _row(self, text, name="Braelon Allen", days=0.5):
        return {"player_name": name, "team_id": "NYJ", "injury_description": text,
                "date_reported": (self.NOW - timedelta(days=days)).isoformat()}

    def test_conditional_lead_role_counts_at_half_weight(self):
        index = news_signals.build_index([
            self._row("If Hall isn't ready, Allen would retain the lead role."),
            self._row("Hall is out.", name="Breece Hall")], self.NOW)
        flags = news_signals.signals_for(index, "Braelon Allen", "NYJ")
        lead = next(f for f in flags if f["flag"] == "lead_role")
        full = news_signals.recency_weight(self._row("x")["date_reported"], self.NOW)
        assert lead["conditional"] is True
        assert lead["weight"] == pytest.approx(full * news_signals.CONDITIONAL_WEIGHT, abs=0.002)

    def test_conditional_availability_is_dropped(self):
        index = news_signals.build_index([
            self._row("If Allen is not cleared to play, Davis would start.")], self.NOW)
        assert news_signals.signals_for(index, "Braelon Allen", "NYJ") == []

    def test_practice_progress_from_last_week_is_dropped(self):
        index = news_signals.build_index([
            self._row("Allen returned to practice Wednesday.", days=8)], self.NOW)
        assert news_signals.signals_for(index, "Braelon Allen", "NYJ") == []


class TestDownstream:
    def test_practice_progress_moves_a_reserve_list_return_only(self):
        flag = [{"flag": "practice_progress", "weight": 1.0,
                 "snippet": "He returned to practice Wednesday."}]
        base = {"player": "X", "player_id": "x", "position": "WR", "team": "SEA",
                "per_game": 10.0, "weekly_points": {}, "expected_absence_games": 5}
        on_ir = value_trajectory.assess({**base, "injury_status": "IR", "news_flags": flag},
                                        week=5)
        out = value_trajectory.assess({**base, "injury_status": "Out", "news_flags": flag},
                                      week=5)
        assert any(s["kind"] == "injury_return" for s in on_ir["signals"])
        assert "back at practice" in on_ir["reasons"][0]
        assert not any(s["kind"] == "injury_return" for s in out["signals"])

    def test_teammate_back_at_practice_still_shortens_his_return(self):
        # Projections read a return cue only for an unavailable teammate,
        # where back at practice means the window opened.
        from nfl_mcp import projections
        values, index = _backfield()
        status = _status("IR", "Lead Back returned to practice Wednesday.")
        due = _project(values, index, status)["breakdown"]["returning_teammates"][0]
        assert due["games_until_return"] == projections.DESIGNATED_RETURN_GAMES
