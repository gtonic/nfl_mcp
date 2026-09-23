"""Practice status in roster enrichment comes from real reports only.

It used to be derived from the injury designation (Questionable -> LP, Out ->
DNP) and defaulted to FP for everyone else, so the user was told players had
practised who never took the field. Now: this week's stored report (NFL.com
official, else a dated ESPN note) or None.
"""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from nfl_mcp.sleeper_tools import _enrich_usage_and_opponent


def _db(injury=None, practice_rows=None):
    mock_db = Mock()
    mock_db.find_player_injury = Mock(return_value=injury)
    mock_db.get_practice_reports = Mock(return_value=practice_rows or [])
    mock_db.get_usage_last_n_weeks = Mock(return_value=None)
    return mock_db


def _row(day, status, source="nfl.com", **extra):
    return {"date": day, "status": status, "source": source,
            "updated_at": datetime.now(UTC).isoformat(), **extra}


ATHLETE = {"id": "12345", "full_name": "Test Player", "position": "WR", "team_id": "KC"}


class TestPracticeStatusEnrichment:
    def test_real_week_pattern_is_exposed(self):
        rows = [_row("2026-09-23", "DNP"), _row("2026-09-24", "LP"),
                _row("2026-09-25", "FP", game_status="Questionable")]
        result = _enrich_usage_and_opponent(_db(practice_rows=rows), ATHLETE, 2026, 3)

        assert result["practice_status"] == "FP"
        assert result["practice_status_date"] == "2026-09-25"
        assert result["practice_pattern"] == "DNP-LP-FP"
        assert result["practice_trend"] == "improving"
        assert [d["day"] for d in result["practice_days"]] == ["Wed", "Thu", "Fri"]
        assert result["practice_source"] == "nfl.com"
        assert result["practice_status_source"] == "nfl.com"
        assert result["practice_report_game_status"] == "Questionable"
        assert "practice_status_age_hours" in result

    def test_lookup_is_by_name_team_and_week(self):
        db = _db()
        _enrich_usage_and_opponent(db, ATHLETE, 2026, 3)
        db.get_practice_reports.assert_called_once_with("Test Player", "KC", season=2026, week=3)

    @pytest.mark.parametrize("status", ["Out", "Questionable", "Doubtful", "Injured Reserve"])
    def test_designation_without_report_is_unreported(self, status):
        injury = {"injury_status": status, "updated_at": datetime.now(UTC).isoformat()}
        result = _enrich_usage_and_opponent(_db(injury=injury), ATHLETE, 2026, 3)
        assert result["injury_status"] == status
        assert result["practice_status"] is None
        assert result["practice_source"] == "unreported"

    def test_healthy_player_is_not_defaulted_to_full_practice(self):
        result = _enrich_usage_and_opponent(_db(), ATHLETE, 2026, 3)
        assert result["practice_status"] is None
        assert result["practice_status_source"] == "unreported"
        assert "practice_pattern" not in result

    def test_news_note_source_is_labelled(self):
        rows = [_row("2026-09-22", "LP", source="espn_news", estimated=True)]
        result = _enrich_usage_and_opponent(_db(practice_rows=rows), ATHLETE, 2026, 3)
        assert result["practice_source"] == "espn_news"
        assert result["practice_trend"] == "single_report"
        assert result["practice_days"][0]["estimated"] is True
