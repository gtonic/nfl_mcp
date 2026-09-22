"""Enrichment blocks that silently never ran.

Each of these returned a placeholder for every player while the real data was
one argument or one key away: matchup difficulty was asked without the
rankings sitting in the database, the Vegas block read a `team` key athlete
rows do not have, and the depth-chart snap estimate checked for a dict where
the database hands back JSON text.
"""
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nfl_mcp.database import NFLDatabase
from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
from nfl_mcp.sleeper_enrichment import _cached_defense_rankings, _enrich_usage_and_opponent


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = NFLDatabase(str(Path(tmp) / "t.db"))
        database.upsert_defense_rankings(
            {"WR": [{"team": "KC", "rank": 30, "points_allowed_avg": 41.2,
                     "matchup_tier": "smash"}]},
            season=2026,
        )
        yield database


def _athlete(**extra):
    return {"id": "1", "player_id": "1", "full_name": "Test Receiver",
            "position": "WR", "team_id": "DAL", **extra}


def _quiet(mock_db):
    """Stub the lookups these tests are not about."""
    mock_db.get_player_snap_pct = MagicMock(return_value=None)
    mock_db.find_player_injury = MagicMock(return_value=None)
    mock_db.get_player_injury_from_cache = MagicMock(return_value=None)
    mock_db.get_latest_practice_status = MagicMock(return_value=None)
    mock_db.get_usage_last_n_weeks = MagicMock(return_value=None)
    mock_db.get_opponent = MagicMock(return_value="KC")
    return mock_db


class TestMatchupRankings:
    def test_rankings_come_from_the_database(self, db):
        analyzer = DefenseRankingsAnalyzer(db=db)
        rankings = _cached_defense_rankings(analyzer, db, 2026)
        assert rankings["WR"][0]["team"] == "KC"

    def test_the_in_memory_cache_wins_over_the_database(self, db):
        analyzer = DefenseRankingsAnalyzer(db=db)
        fresh = {"WR": [{"team": "KC", "rank": 2, "points_allowed_avg": 20.0,
                         "matchup_tier": "tough"}]}
        analyzer._rankings_cache["defense_rankings_2026"] = {
            "data": fresh, "timestamp": datetime.now(UTC)}
        assert _cached_defense_rankings(analyzer, db, 2026) is fresh

    def test_no_season_or_no_data_means_none(self, db):
        analyzer = DefenseRankingsAnalyzer(db=db)
        assert _cached_defense_rankings(analyzer, db, None) is None
        assert _cached_defense_rankings(analyzer, db, 2019) is None

    def test_enrichment_reports_the_real_rank(self, db):
        analyzer = DefenseRankingsAnalyzer(db=db)
        mock_db = _quiet(MagicMock())
        mock_db.get_defense_rankings = db.get_defense_rankings
        with patch("nfl_mcp.matchup_tools.get_defense_analyzer", return_value=analyzer):
            out = _enrich_usage_and_opponent(mock_db, _athlete(), 2026, 3)
        assert out["matchup_rank"] == 30
        assert out["matchup_tier"] == "smash"
        assert out["matchup_source"] == "defense_rankings"


class TestVegasTeamKey:
    def test_vegas_block_runs_off_team_id(self):
        vegas = MagicMock()
        # Shaped like a real line: no `is_fallback` key at all.
        vegas.get_game_lines.return_value = {
            "home_team": "DAL", "total": 48.5,
            "home_implied_total": 26.0, "away_implied_total": 22.5,
            "home_spread": -3.5, "game_environment": {"tier": "high", "indicator": "🔥"},
        }
        vegas._normalize_team.side_effect = lambda t: t
        with patch("nfl_mcp.vegas_tools.get_vegas_analyzer", return_value=vegas):
            out = _enrich_usage_and_opponent(_quiet(MagicMock()), _athlete(), 2026, 3)
        vegas.get_game_lines.assert_called_once_with("DAL", opponent="KC")
        assert out["implied_team_total"] == 26.0
        assert out["vegas_source"] == "lines"


class TestDepthEstimate:
    def test_json_text_raw_feeds_the_depth_estimate(self):
        athlete = _athlete(raw=json.dumps({"depth_chart_order": 1}))
        out = _enrich_usage_and_opponent(_quiet(MagicMock()), athlete, 2026, 3)
        # Without a depth rank there is no estimate at all, so a value here
        # proves the JSON was read.
        assert out["snap_pct"] == 85.0
        assert out["snap_pct_source"] == "estimated"

    def test_unparseable_raw_is_ignored(self):
        athlete = _athlete(raw="{not json")
        out = _enrich_usage_and_opponent(_quiet(MagicMock()), athlete, 2026, 3)
        assert "snap_pct" not in out
