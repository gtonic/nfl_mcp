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
HOLDOUT_SPLITS = ("holdout", "holdout2", "holdout3")
HELD_OUT = [e for e in ENTRIES if e["split"] in HOLDOUT_SPLITS]

# Thresholds a pattern change must keep. Dev: the sentences the patterns
# were written against (2026-10-10: P 0.97 / R 0.94 overall, 462
# sentences). Held out, each labelled before the classifier was scored on
# it: `holdout` (2026-10-08, 70), `holdout2` (100 from player_news, Oct
# 8-10, labelled before the 2026-10-10 rework), `holdout3` (80 more from the
# same pool, labelled after it). 2026-10-10: holdout P 1.00 / R 0.56,
# holdout2 1.00 / 0.68, holdout3 0.94 / 0.52; all three 0.98 / 0.60.
DEV_MIN = {"precision": 0.95, "recall": 0.92}
DEV_FLAG_MIN = 0.80          # per flag with at least 5 gold labels
HOLDOUT_MIN = {"holdout": {"precision": 0.95, "recall": 0.50},
               "holdout2": {"precision": 0.95, "recall": 0.60},
               "holdout3": {"precision": 0.90, "recall": 0.45}}
HELD_OUT_MIN = {"precision": 0.95, "recall": 0.55}
# One week of labels in the old classifier's terms: what this replaced.
BASELINE_DEV = {"precision": 0.55, "recall": 0.59}


def _flags(text, owner="owner", names=None, owner_last=""):
    return {h["flag"]: h for h in news_signals.classify(text, owner, names, owner_last)}


class TestLabelledSet:
    def test_fixture_shape(self):
        assert 500 <= len(ENTRIES) <= 900
        assert all(sum(e["split"] == s for e in ENTRIES) >= 60 for s in HOLDOUT_SPLITS)
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

    @pytest.mark.parametrize("split", HOLDOUT_SPLITS)
    def test_holdout_precision_recall(self, split):
        result = ev.score([e for e in ENTRIES if e["split"] == split])
        assert result["_all"]["precision"] >= HOLDOUT_MIN[split]["precision"], ev.table(result)
        assert result["_all"]["recall"] >= HOLDOUT_MIN[split]["recall"], ev.table(result)

    def test_all_held_out_precision_recall(self):
        result = ev.score(HELD_OUT)
        assert result["_all"]["precision"] >= HELD_OUT_MIN["precision"], ev.table(result)
        assert result["_all"]["recall"] >= HELD_OUT_MIN["recall"], ev.table(result)

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
        flags = _flags("Trost (hamstring) was placed on injured reserve with a designation "
                       "to return by the Rams on Sunday.")
        assert set(flags) == {"multi_week_absence"}
        assert flags["multi_week_absence"]["weeks"] == news_signals.IR_MIN_GAMES
        assert flags["multi_week_absence"]["weeks_minimum"] is True

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


class TestRecallPhrasings:
    """The 2026-10-10 vocabulary: absences with a length, snap-share drops,
    game-time decisions, trending toward (not) playing."""

    @pytest.mark.parametrize("text,length", [
        ("Garrett is facing a six-week recovery from knee surgery.", {"weeks": 6}),
        ("He is expected to miss at least three weeks.", {"weeks": 3}),
        ("Brooks is out four to six weeks.", {"weeks": 6}),
        ("He will miss at least the next four games.", {"weeks": 4}),
        ("Ferguson will be eligible to return in Week 8 against the Chargers.",
         {"return_week": 8}),
        ("He is expected back Week 9.", {"return_week": 9}),
        ("Reed will require season-ending surgery on his neck.", {"season_ending": True}),
        ("He'll miss the rest of the season.", {"season_ending": True}),
        ("The Chargers placed Slater (ankle) on injured reserve Friday.",
         {"weeks": 4, "weeks_minimum": True}),
        ("The receiver looks IR-bound after the MRI.", {"weeks": 4, "weeks_minimum": True}),
    ])
    def test_multi_week_absence_with_a_length(self, text, length):
        hit = _flags(text)["multi_week_absence"]
        assert {k: hit[k] for k in news_signals.ABSENCE_FIELDS if k in hit} == length

    def test_a_second_reading_fills_the_length(self):
        hit = _flags("Price will go on IR and miss at least the next four games and won't be "
                     "eligible to return until at least Week 8.")["multi_week_absence"]
        assert hit["weeks"] == 4 and hit["return_week"] == 8 and "weeks_minimum" not in hit

    @pytest.mark.parametrize("text", [
        "Kraft made a remarkable recovery from last year's season-ending knee injury.",
        "Payton said he doesn't expect him to be placed on injured reserve.",
        "A decision has yet to be made regarding whether he will be placed on injured reserve.",
        "Dell missed the required four games upon landing on injured reserve Aug. 21.",
        "Bosa has missed two games since injuring his calf.",
        "He will face an absence of at least four games if he's placed on injured reserve.",
    ])
    def test_no_absence(self, text):
        hit = _flags(text).get("multi_week_absence")
        assert hit is None or hit.get("conditional")

    def test_reported_speech_is_not_a_condition(self):
        assert "multi_week_absence" in _flags(
            "Bowles acknowledged that Mayfield would miss at least three weeks.")

    @pytest.mark.parametrize("text", [
        "Gainwell was out-snapped 36 to 26 by Bucky Irving.",
        "His offensive snap share dropped to 45 percent.",
        "Ridley dropped to 20 snaps and went without a catch.",
        "Kincaid saw a reduced role in the air attack for the second straight week.",
        "Despite logging a season-low 51 percent snap share, Irving earned 17 touches.",
        "He played only 13 of 62 offensive snaps (21.0 percent), well behind Tuten.",
        "He worked as the No. 2 behind the veteran on Sunday.",
    ])
    def test_snap_share_drop(self, text):
        assert "snap_share_drop" in _flags(text)

    def test_active_out_snapped_is_the_other_players_drop(self):
        names = {"ridley": "calvin ridley", "calvin ridley": "calvin ridley"}
        hits = news_signals.classify("Ayomanor again out-snapped Calvin Ridley by a 39-30 margin.",
                                     "elic ayomanor", names, "ayomanor")
        assert {(h["flag"], h["about"]) for h in hits} == {("snap_share_drop", "calvin ridley")}

    def test_conditional_secondary_role_is_marked(self):
        assert _flags("Collins's return would return Boutte to a secondary role.")[
            "snap_share_drop"].get("conditional")

    @pytest.mark.parametrize("text,flag", [
        ("Coach Kyle Shanahan said Evans (ribs) will be a game-time decision.",
         "game_time_decision"),
        ("He appears to be trending toward playing in Week 5.", "expected_to_play"),
        ("Allen is trending toward being ready to go against the Steelers.", "expected_to_play"),
        ("He could be trending toward a second consecutive absence Sunday.", "unlikely_to_play"),
        ("Smith is trending toward missing Sunday's game.", "unlikely_to_play"),
        ("He's trending toward not being available Monday.", "unlikely_to_play"),
        ("Huntley appears to be trending toward a Week 5 start.", "lead_role"),
        ("Kincaid remains in a limited role in the passing game.", "limited_snaps"),
        ("Bigsby faded an injury tag after logging a full practice Saturday.",
         "expected_to_play"),
        ("With Williams set to miss his third consecutive game, Bagent starts.", "ruled_out"),
    ])
    def test_phrasings(self, text, flag):
        assert flag in _flags(text)

    @pytest.mark.parametrize("text", [
        "He may need a full practice Friday to approach Sunday's game without an injury "
        "designation.",
        "He has two more chances to avoid an injury designation.",
        "Harvey was evaluated for a concussion and cleared to return to Sunday's game.",
        "He's unsure whether he'll be cleared to return for Sunday's game.",
    ])
    def test_hopes_are_not_availability(self, text):
        assert "expected_to_play" not in _flags(text)

    def test_ruled_out_object_and_passive(self):
        names = {"davis": None, "carlton davis": "carlton davis", "sean davis": "sean davis",
                 "johnston": "quentin johnston", "mcconkey": "ladd mcconkey"}
        hits = news_signals.classify("The Pats also ruled out fellow starting CB Carlton Davis "
                                     "(neck) for Week 5.", "christian gonzalez", names, "gonzalez")
        assert {(h["flag"], h["about"]) for h in hits} == {("ruled_out", "carlton davis")}
        hits = news_signals.classify("Quentin Johnston (chest) has been ruled out and Ladd "
                                     "McConkey may not play.", "tre harris", names, "harris")
        assert ("ruled_out", "quentin johnston") in {(h["flag"], h["about"]) for h in hits}


class TestAbsenceWeeks:
    NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)

    def _flag(self, days=0.0, **length):
        return {"flag": "multi_week_absence", "weight": 1.0, "snippet": "x",
                "date_reported": (self.NOW - timedelta(days=days)).isoformat(), **length}

    def test_counts_from_the_report(self):
        assert news_signals.absence_weeks([self._flag(weeks=6)], self.NOW.date())[0] == 6
        # Reported nine days ago: one week already gone.
        assert news_signals.absence_weeks([self._flag(9, weeks=3)], self.NOW.date())[0] == 2
        assert news_signals.absence_weeks([self._flag(30, weeks=3)], self.NOW.date())[0] == 1

    def test_return_week_needs_the_week(self):
        flags = [self._flag(return_week=8)]
        assert news_signals.absence_weeks(flags, self.NOW.date()) == (None, None)
        assert news_signals.absence_weeks(flags, self.NOW.date(), season_week=6)[0] == 2

    def test_season_ending_and_longest_wins(self):
        n, why = news_signals.absence_weeks(
            [self._flag(weeks=3), self._flag(season_ending=True)], self.NOW.date())
        assert n == news_signals.SEASON_ENDING_GAMES and "season-ending" in why

    def test_flag_carries_the_length_through_the_index(self):
        index = news_signals.build_index([{
            "player_name": "Myles Garrett", "team_id": "CLE",
            "injury_description": "Garrett is facing a six-week recovery from surgery.",
            "date_reported": self.NOW.isoformat()}], self.NOW)
        flag = news_signals.signals_for(index, "Myles Garrett", "CLE")[0]
        assert flag["flag"] == "multi_week_absence" and flag["weeks"] == 6


class TestExpectedAbsencePrecedence:
    """ESPN return date > parsed news weeks > status heuristics."""

    def test_news_weeks_extend_an_out_status(self):
        from nfl_mcp import ros
        n, why = ros.expected_absence("Out", news_weeks=5, news_reason="news: 5 weeks")
        assert n == 5 and "news" in why

    def test_return_date_wins(self):
        from datetime import date

        from nfl_mcp import ros
        n, why = ros.expected_absence("Out", return_date="2026-10-20", today=date(2026, 10, 10),
                                      news_weeks=6)
        assert n == 2 and "return date" in why

    def test_news_never_shortens_a_reserve_minimum_or_longer_text(self):
        from nfl_mcp import ros
        assert ros.expected_absence("IR", news_weeks=2)[0] == ros.IR_MIN_WEEKS
        assert ros.expected_absence("Out", "suffered a season-ending ACL tear",
                                    news_weeks=3)[0] == ros.SEASON_ENDING_WEEKS

    def test_not_out_ignores_news(self):
        from nfl_mcp import ros
        assert ros.expected_absence("Questionable", news_weeks=4)[0] == 0
