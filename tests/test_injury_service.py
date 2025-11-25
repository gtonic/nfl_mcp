"""Tests for injury_service module.

Tests the InjuryAggregator class and related functionality:
- Status normalization
- Severity calculation
- Confidence scoring
- Multi-source aggregation
"""

import asyncio
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from nfl_mcp.injury_service import (
    InjuryAggregator,
    InjuryReport,
    InjurySeverity,
    STATUS_NORMALIZATIONS,
    STATUS_SEVERITY,
    get_injury_reports,
    get_player_injury_report,
)


class TestInjurySeverity:
    """Test InjurySeverity enum."""
    
    def test_severity_values(self):
        """Test severity enum values."""
        assert InjurySeverity.MINOR == 1
        assert InjurySeverity.QUESTIONABLE == 2
        assert InjurySeverity.MODERATE == 3
        assert InjurySeverity.SIGNIFICANT == 4
        assert InjurySeverity.SEVERE == 5


class TestInjuryReport:
    """Test InjuryReport dataclass."""
    
    def test_default_values(self):
        """Test default values for InjuryReport."""
        report = InjuryReport(
            player_id="12345",
            player_name="Test Player",
            team_id="KC"
        )
        assert report.player_id == "12345"
        assert report.player_name == "Test Player"
        assert report.team_id == "KC"
        assert report.injury_status == "Unknown"
        assert report.confidence == 50
        assert report.sources == ["ESPN"]
    
    def test_to_dict(self):
        """Test InjuryReport.to_dict() method."""
        report = InjuryReport(
            player_id="12345",
            player_name="Patrick Mahomes",
            team_id="KC",
            position="QB",
            injury_status="Questionable",
            injury_type="Ankle",
            injury_description="Ankle sprain",
            game_status="Active",
            severity=2,
            confidence=80,
            sources=["ESPN", "CBS"],
            date_reported="2024-01-15"
        )
        
        result = report.to_dict()
        
        assert result["player_id"] == "12345"
        assert result["player_name"] == "Patrick Mahomes"
        assert result["team_id"] == "KC"
        assert result["position"] == "QB"
        assert result["injury_status"] == "Questionable"
        assert result["severity"] == 2
        assert result["confidence"] == 80
        assert result["sources"] == ["ESPN", "CBS"]


class TestStatusNormalization:
    """Test status normalization."""
    
    def test_normalize_common_statuses(self):
        """Test normalization of common injury statuses."""
        assert InjuryAggregator.normalize_status("out") == "Out"
        assert InjuryAggregator.normalize_status("OUT") == "Out"
        assert InjuryAggregator.normalize_status("questionable") == "Questionable"
        assert InjuryAggregator.normalize_status("QUESTIONABLE") == "Questionable"
        assert InjuryAggregator.normalize_status("doubtful") == "Doubtful"
        assert InjuryAggregator.normalize_status("probable") == "Probable"
    
    def test_normalize_special_statuses(self):
        """Test normalization of special statuses."""
        assert InjuryAggregator.normalize_status("injured reserve") == "IR"
        assert InjuryAggregator.normalize_status("ir") == "IR"
        assert InjuryAggregator.normalize_status("pup") == "PUP"
        assert InjuryAggregator.normalize_status("nfi") == "NFI"
        assert InjuryAggregator.normalize_status("day-to-day") == "Questionable"
    
    def test_normalize_practice_statuses(self):
        """Test normalization of practice statuses."""
        assert InjuryAggregator.normalize_status("did not practice") == "DNP"
        assert InjuryAggregator.normalize_status("dnp") == "DNP"
        assert InjuryAggregator.normalize_status("limited participation") == "LP"
        assert InjuryAggregator.normalize_status("lp") == "LP"
        assert InjuryAggregator.normalize_status("full participation") == "FP"
    
    def test_normalize_unknown_status(self):
        """Test that unknown statuses are title-cased."""
        assert InjuryAggregator.normalize_status("some new status") == "Some New Status"
    
    def test_normalize_empty_status(self):
        """Test that empty/None status returns Unknown."""
        assert InjuryAggregator.normalize_status("") == "Unknown"
        assert InjuryAggregator.normalize_status(None) == "Unknown"


class TestSeverityCalculation:
    """Test severity calculation from status."""
    
    def test_minor_severity(self):
        """Test severity for minor injuries."""
        assert InjuryAggregator.get_severity("Active") == InjurySeverity.MINOR
        assert InjuryAggregator.get_severity("Probable") == InjurySeverity.MINOR
        assert InjuryAggregator.get_severity("FP") == InjurySeverity.MINOR
    
    def test_questionable_severity(self):
        """Test severity for questionable injuries."""
        assert InjuryAggregator.get_severity("Questionable") == InjurySeverity.QUESTIONABLE
        assert InjuryAggregator.get_severity("LP") == InjurySeverity.QUESTIONABLE
    
    def test_moderate_severity(self):
        """Test severity for moderate injuries."""
        assert InjuryAggregator.get_severity("DNP") == InjurySeverity.MODERATE
    
    def test_significant_severity(self):
        """Test severity for significant injuries."""
        assert InjuryAggregator.get_severity("Out") == InjurySeverity.SIGNIFICANT
        assert InjuryAggregator.get_severity("Doubtful") == InjurySeverity.SIGNIFICANT
    
    def test_severe_severity(self):
        """Test severity for severe injuries."""
        assert InjuryAggregator.get_severity("IR") == InjurySeverity.SEVERE
        assert InjuryAggregator.get_severity("PUP") == InjurySeverity.SEVERE
        assert InjuryAggregator.get_severity("NFI") == InjurySeverity.SEVERE
        assert InjuryAggregator.get_severity("Suspended") == InjurySeverity.SEVERE
    
    def test_unknown_status_defaults_to_moderate(self):
        """Test that unknown statuses default to MODERATE."""
        assert InjuryAggregator.get_severity("Unknown Status") == InjurySeverity.MODERATE


class TestConfidenceCalculation:
    """Test confidence score calculation."""
    
    def test_single_source_confidence(self):
        """Test confidence with single source."""
        conf = InjuryAggregator.calculate_confidence(["ESPN"], True)
        assert conf == 80  # 40 base + 20 source + 20 match
    
    def test_dual_source_matching(self):
        """Test confidence with two sources that match."""
        conf = InjuryAggregator.calculate_confidence(["ESPN", "CBS"], True)
        assert conf == 100  # 40 base + 40 sources + 20 agreement
    
    def test_dual_source_not_matching(self):
        """Test confidence with two sources that don't match."""
        conf = InjuryAggregator.calculate_confidence(["ESPN", "CBS"], False)
        assert conf == 80  # 40 base + 40 sources + 0 agreement
    
    def test_many_sources_capped(self):
        """Test that source points are capped at 40."""
        conf = InjuryAggregator.calculate_confidence(
            ["ESPN", "CBS", "Yahoo", "Rotoworld"], True
        )
        assert conf == 100  # Capped at 100
    
    def test_confidence_never_exceeds_100(self):
        """Test that confidence is capped at 100."""
        conf = InjuryAggregator.calculate_confidence(
            ["S1", "S2", "S3", "S4", "S5"], True
        )
        assert conf == 100


class TestInjuryAggregation:
    """Test injury aggregation from multiple sources."""
    
    def test_aggregate_espn_only(self):
        """Test aggregation with ESPN injuries only."""
        aggregator = InjuryAggregator()
        
        espn = [
            InjuryReport(
                player_id="123",
                player_name="Player A",
                team_id="KC",
                injury_status="Questionable",
                sources=["ESPN"]
            )
        ]
        cbs = []
        
        result = aggregator._aggregate_injuries(espn, cbs)
        
        assert len(result) == 1
        assert result[0].player_id == "123"
        assert result[0].sources == ["ESPN"]
    
    def test_aggregate_merges_sources(self):
        """Test that aggregation merges sources for same player."""
        aggregator = InjuryAggregator()
        
        espn = [
            InjuryReport(
                player_id="123",
                player_name="Player A",
                team_id="KC",
                injury_status="Questionable",
                confidence=60,
                sources=["ESPN"]
            )
        ]
        cbs = [
            InjuryReport(
                player_id="123",
                player_name="Player A",
                team_id="KC",
                injury_status="Questionable",
                confidence=60,
                sources=["CBS"]
            )
        ]
        
        result = aggregator._aggregate_injuries(espn, cbs)
        
        assert len(result) == 1
        assert set(result[0].sources) == {"ESPN", "CBS"}
        assert result[0].confidence == 100  # Sources match
    
    def test_aggregate_different_statuses_lower_confidence(self):
        """Test that disagreeing sources result in lower confidence."""
        aggregator = InjuryAggregator()
        
        espn = [
            InjuryReport(
                player_id="123",
                player_name="Player A",
                team_id="KC",
                injury_status="Questionable",
                confidence=60,
                sources=["ESPN"]
            )
        ]
        cbs = [
            InjuryReport(
                player_id="123",
                player_name="Player A",
                team_id="KC",
                injury_status="Out",  # Different status
                confidence=60,
                sources=["CBS"]
            )
        ]
        
        result = aggregator._aggregate_injuries(espn, cbs)
        
        assert len(result) == 1
        assert set(result[0].sources) == {"ESPN", "CBS"}
        assert result[0].confidence == 80  # No agreement bonus
    
    def test_aggregate_cbs_only_lower_confidence(self):
        """Test that CBS-only injuries get lower confidence."""
        aggregator = InjuryAggregator()
        
        espn = []
        cbs = [
            InjuryReport(
                player_id="456",
                player_name="Player B",
                team_id="PHI",
                injury_status="Doubtful",
                confidence=60,
                sources=["CBS"]
            )
        ]
        
        result = aggregator._aggregate_injuries(espn, cbs)
        
        assert len(result) == 1
        assert result[0].confidence == 40  # Single source, not primary


class TestInjuryAggregatorAsync:
    """Test async methods of InjuryAggregator."""
    
    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test InjuryAggregator as async context manager."""
        with patch("nfl_mcp.config.create_http_client") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value = MagicMock()
            mock_cm.__aexit__.return_value = None
            mock_client.return_value = mock_cm
            
            async with InjuryAggregator() as aggregator:
                assert aggregator is not None
    
    @pytest.mark.asyncio
    async def test_get_team_injuries_calls_fetch(self):
        """Test that get_team_injuries calls fetch_all_injuries."""
        aggregator = InjuryAggregator()
        aggregator.fetch_all_injuries = AsyncMock(return_value=[])
        
        result = await aggregator.get_team_injuries("KC")
        
        aggregator.fetch_all_injuries.assert_called_once_with(["KC"], True, None)
    
    @pytest.mark.asyncio
    async def test_get_player_injury_from_cache(self):
        """Test getting player injury from cache."""
        mock_db = MagicMock()
        mock_db.get_player_injury_from_cache.return_value = {
            "player_id": "123",
            "player_name": "Test Player",
            "team_id": "KC",
            "injury_status": "Questionable",
            "updated_at": datetime.now(UTC).isoformat()
        }
        
        aggregator = InjuryAggregator(db=mock_db)
        result = await aggregator.get_player_injury("123")
        
        assert result is not None
        assert result.player_id == "123"
        mock_db.get_player_injury_from_cache.assert_called_once()


class TestConvenienceFunctions:
    """Test module-level convenience functions."""
    
    @pytest.mark.asyncio
    async def test_get_injury_reports(self):
        """Test get_injury_reports convenience function."""
        with patch("nfl_mcp.injury_service.InjuryAggregator") as mock_agg:
            mock_instance = AsyncMock()
            mock_instance.fetch_all_injuries = AsyncMock(return_value=[
                InjuryReport(
                    player_id="123",
                    player_name="Test",
                    team_id="KC",
                    injury_status="Out"
                )
            ])
            mock_agg.return_value.__aenter__.return_value = mock_instance
            mock_agg.return_value.__aexit__.return_value = None
            
            result = await get_injury_reports(teams=["KC"])
            
            assert len(result) == 1
            assert result[0]["player_id"] == "123"
    
    @pytest.mark.asyncio
    async def test_get_player_injury_report(self):
        """Test get_player_injury_report convenience function."""
        with patch("nfl_mcp.injury_service.InjuryAggregator") as mock_agg:
            mock_instance = AsyncMock()
            mock_instance.get_player_injury = AsyncMock(return_value=InjuryReport(
                player_id="123",
                player_name="Test",
                team_id="KC",
                injury_status="Questionable"
            ))
            mock_agg.return_value.__aenter__.return_value = mock_instance
            mock_agg.return_value.__aexit__.return_value = None
            
            result = await get_player_injury_report("123", "KC")
            
            assert result is not None
            assert result["player_id"] == "123"
    
    @pytest.mark.asyncio
    async def test_get_player_injury_report_not_found(self):
        """Test get_player_injury_report when player not found."""
        with patch("nfl_mcp.injury_service.InjuryAggregator") as mock_agg:
            mock_instance = AsyncMock()
            mock_instance.get_player_injury = AsyncMock(return_value=None)
            mock_agg.return_value.__aenter__.return_value = mock_instance
            mock_agg.return_value.__aexit__.return_value = None
            
            result = await get_player_injury_report("999", "XX")
            
            assert result is None


class TestDatabaseIntegration:
    """Test database integration for injury caching."""
    
    def test_cache_injuries(self):
        """Test caching injuries to database."""
        mock_db = MagicMock()
        mock_db.upsert_injuries.return_value = 2
        
        aggregator = InjuryAggregator(db=mock_db)
        injuries = [
            InjuryReport(player_id="1", player_name="A", team_id="KC", injury_status="Out"),
            InjuryReport(player_id="2", player_name="B", team_id="PHI", injury_status="Questionable"),
        ]
        
        count = aggregator._cache_injuries(injuries)
        
        assert count == 2
        mock_db.upsert_injuries.assert_called_once()
    
    def test_get_cached_injuries(self):
        """Test getting cached injuries from database."""
        mock_db = MagicMock()
        mock_db.get_team_injuries_from_cache.return_value = [
            {
                "player_id": "123",
                "player_name": "Test Player",
                "team_id": "KC",
                "injury_status": "Questionable",
                "confidence": 80,
                "sources": ["ESPN", "CBS"],
            }
        ]
        
        aggregator = InjuryAggregator(db=mock_db)
        result = aggregator._get_cached_injuries(["KC"], None)
        
        assert len(result) == 1
        assert result[0].player_id == "123"
        assert result[0].confidence == 80
        assert result[0].sources == ["ESPN", "CBS"]


class TestNFLTeams:
    """Test NFL team list."""
    
    def test_all_32_teams(self):
        """Test that all 32 NFL teams are included."""
        assert len(InjuryAggregator.NFL_TEAMS) == 32
    
    def test_team_abbreviations_format(self):
        """Test that team abbreviations are properly formatted."""
        for team in InjuryAggregator.NFL_TEAMS:
            assert team.isupper()
            assert 2 <= len(team) <= 3
    
    def test_washington_uses_wsh(self):
        """Test that Washington uses WSH abbreviation."""
        assert "WSH" in InjuryAggregator.NFL_TEAMS
        assert "WAS" not in InjuryAggregator.NFL_TEAMS


class TestCacheManagement:
    """Test cache management utilities."""
    
    def test_clear_caches(self):
        """Test clearing all in-memory caches."""
        # Add some test data to caches
        InjuryAggregator._athlete_name_cache["test_id"] = "Test Player"
        InjuryAggregator._etag_cache["test_url"] = "test_etag"
        InjuryAggregator._last_modified_cache["test_url"] = "test_date"
        
        # Clear caches
        stats = InjuryAggregator.clear_caches()
        
        # Verify stats returned
        assert stats["athlete_names"] >= 1
        assert stats["etags"] >= 1
        assert stats["last_modified"] >= 1
        
        # Verify caches are empty
        assert len(InjuryAggregator._athlete_name_cache) == 0
        assert len(InjuryAggregator._etag_cache) == 0
        assert len(InjuryAggregator._last_modified_cache) == 0
    
    def test_get_cache_stats(self):
        """Test getting cache statistics."""
        # Clear first to have known state
        InjuryAggregator.clear_caches()
        
        # Add test data
        InjuryAggregator._athlete_name_cache["12345"] = "Patrick Mahomes"
        InjuryAggregator._athlete_name_cache["67890"] = "Travis Kelce"
        
        stats = InjuryAggregator.get_cache_stats()
        
        assert stats["athlete_name_cache_size"] == 2
        assert stats["athlete_name_cache_max"] == 500  # ATHLETE_CACHE_SIZE
        assert stats["etag_cache_size"] == 0
        assert stats["last_modified_cache_size"] == 0
        assert len(stats["sample_athletes"]) == 2
        
        # Cleanup
        InjuryAggregator.clear_caches()
    
    def test_cache_stats_sample_limited(self):
        """Test that sample_athletes is limited to 5 entries."""
        InjuryAggregator.clear_caches()
        
        # Add more than 5 entries
        for i in range(10):
            InjuryAggregator._athlete_name_cache[str(i)] = f"Player {i}"
        
        stats = InjuryAggregator.get_cache_stats()
        
        assert stats["athlete_name_cache_size"] == 10
        assert len(stats["sample_athletes"]) == 5
        
        # Cleanup
        InjuryAggregator.clear_caches()


class TestConcurrencyConfiguration:
    """Test concurrency configuration constants."""
    
    def test_semaphores_created(self):
        """Test that semaphores are created with correct limits."""
        from nfl_mcp.injury_service import MAX_CONCURRENT_TEAMS, MAX_CONCURRENT_INJURIES
        
        aggregator = InjuryAggregator()
        
        # Verify semaphores exist
        assert hasattr(aggregator, '_team_semaphore')
        assert hasattr(aggregator, '_injury_semaphore')
        
        # Verify they're semaphores
        assert isinstance(aggregator._team_semaphore, asyncio.Semaphore)
        assert isinstance(aggregator._injury_semaphore, asyncio.Semaphore)
    
    def test_configuration_constants(self):
        """Test that configuration constants have reasonable values."""
        from nfl_mcp.injury_service import (
            MAX_CONCURRENT_TEAMS,
            MAX_CONCURRENT_INJURIES,
            ATHLETE_CACHE_SIZE,
            REQUEST_TIMEOUT
        )
        
        assert 1 <= MAX_CONCURRENT_TEAMS <= 32  # Not more than total teams
        assert 1 <= MAX_CONCURRENT_INJURIES <= 50  # Not more than page limit
        assert ATHLETE_CACHE_SIZE >= 100  # Reasonable cache size
        assert 5.0 <= REQUEST_TIMEOUT <= 60.0  # Reasonable timeout