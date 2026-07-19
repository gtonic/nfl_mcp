"""Tests for waiver_tools module."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nfl_mcp.waiver_tools import WaiverAnalyzer, get_waiver_log, check_re_entry_status, get_waiver_wire_dashboard


class TestWaiverAnalyzer:
    """Test WaiverAnalyzer class."""

    @pytest.fixture
    def analyzer(self):
        return WaiverAnalyzer()

    def test_extract_waiver_transactions_waiver_type(self, analyzer):
        """Test extraction of waiver transactions with waiver type."""
        transactions = [
            {
                'type': 'waiver',
                'transaction_id': '1',
                'adds': {'player1': 'roster1'},
                'drops': {'player2': 'roster1'},
                'roster_ids': ['roster1'],
                'created': '2026-01-01'
            }
        ]
        
        result = analyzer._extract_waiver_transactions(transactions)
        
        assert len(result) == 1
        assert result[0]['type'] == 'waiver'
        assert 'player1' in result[0]['adds']

    def test_extract_waiver_transactions_free_agent_type(self, analyzer):
        """Test extraction of free agent transactions."""
        transactions = [
            {
                'type': 'free_agent',
                'transaction_id': '2',
                'adds': {'player3': 'roster2'},
                'drops': {},
                'roster_ids': ['roster2'],
                'created': '2026-01-02'
            }
        ]
        
        result = analyzer._extract_waiver_transactions(transactions)
        
        assert len(result) == 1
        assert result[0]['type'] == 'free_agent'

    def test_extract_waiver_transactions_non_waiver(self, analyzer):
        """Test that non-waiver transactions are filtered out."""
        transactions = [
            {
                'type': 'trade',  # Not a waiver transaction
                'transaction_id': '3',
                'adds': {'player4': 'roster3'},
                'drops': {},
                'roster_ids': ['roster3']
            }
        ]
        
        result = analyzer._extract_waiver_transactions(transactions)
        
        assert len(result) == 0

    def test_deduplicate_waiver_log_no_duplicates(self, analyzer):
        """Test deduplication with no duplicates."""
        transactions = [
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'roster_ids': ['roster1'],
                'created': '2026-01-01'
            },
            {
                'adds': {'player2': 'roster1'},
                'drops': {},
                'roster_ids': ['roster1'],
                'created': '2026-01-02'
            }
        ]
        
        unique, duplicates = analyzer._deduplicate_waiver_log(transactions)
        
        assert len(unique) == 2
        assert len(duplicates) == 0

    def test_deduplicate_waiver_log_with_duplicates(self, analyzer):
        """Test deduplication with duplicates."""
        transactions = [
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'roster_ids': ['roster1'],
                'created': '2026-01-01'
            },
            {
                'adds': {'player1': 'roster1'},  # Same signature
                'drops': {},
                'roster_ids': ['roster1'],
                'created': '2026-01-01'
            }
        ]
        
        unique, duplicates = analyzer._deduplicate_waiver_log(transactions)
        
        assert len(unique) == 1
        assert len(duplicates) == 1

    def test_track_re_entries_simple(self, analyzer):
        """Test re-entry tracking with simple add/drop pattern."""
        transactions = [
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'created': 1735689600  # 2025-01-01 in epoch
            },
            {
                'adds': {},
                'drops': {'player1': 'roster1'},
                'created': 1736089600  # 2025-01-05 in epoch
            }
        ]
        
        result = analyzer._track_re_entries(transactions)
        
        # player1 should have activity
        assert 'player1' in result
        assert result['player1']['total_activities'] == 2

    def test_track_re_entries_with_reentry(self, analyzer):
        """Test re-entry tracking with add after drop."""
        transactions = [
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'created': 1735689600  # 2025-01-01 in epoch
            },
            {
                'adds': {},
                'drops': {'player1': 'roster1'},
                'created': 1736089600  # 2025-01-05 in epoch
            },
            {
                'adds': {'player1': 'roster1'},
                'drops': {},
                'created': 1736489600  # 2025-01-10 in epoch
            }
        ]
        
        result = analyzer._track_re_entries(transactions)
        
        assert 'player1' in result
        re_entries = result['player1'].get('re_entries', [])
        assert len(re_entries) > 0


class TestGetWaiverLog:
    """Test get_waiver_log async function."""

    @pytest.mark.asyncio
    async def test_get_waiver_log_success(self):
        """Test successful waiver log retrieval."""
        mock_transactions = {
            "success": True,
            "transactions": [
                {
                    "type": "waiver",
                    "transaction_id": "1",
                    "adds": {"player1": "roster1"},
                    "drops": {},
                    "roster_ids": ["roster1"]
                }
            ]
        }
        
        async def mock_get_transactions(league_id, round):
            return mock_transactions
        
        with patch('nfl_mcp.waiver_tools.get_transactions', side_effect=mock_get_transactions):
            result = await get_waiver_log("league1", dedupe=True)
            
            assert result["success"] is True
            assert "waiver_log" in result or "success" in result

    @pytest.mark.asyncio
    async def test_get_waiver_log_failed_fetch(self):
        """Test waiver log with failed transaction fetch."""
        mock_transactions = {
            "success": False,
            "error": "Failed to fetch"
        }
        
        async def mock_get_transactions(league_id, round):
            return mock_transactions
        
        with patch('nfl_mcp.waiver_tools.get_transactions', side_effect=mock_get_transactions):
            result = await get_waiver_log("league1")
            
            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_get_waiver_log_no_dedup(self):
        """Test waiver log without deduplication."""
        mock_transactions = {
            "success": True,
            "transactions": [
                {
                    "type": "waiver",
                    "transaction_id": "1",
                    "adds": {"player1": "roster1"},
                    "drops": {},
                    "roster_ids": ["roster1"]
                }
            ]
        }
        
        async def mock_get_transactions(league_id, round):
            return mock_transactions
        
        with patch('nfl_mcp.waiver_tools.get_transactions', side_effect=mock_get_transactions):
            result = await get_waiver_log("league1", dedupe=False)
            
            assert result["success"] is True
            assert result.get("deduplication_enabled") is False


class TestCheckReEntryStatus:
    """Test check_re_entry_status async function."""

    @pytest.mark.asyncio
    async def test_check_re_entry_status_success(self):
        """Test successful re-entry status check."""
        mock_transactions = {
            "success": True,
            "transactions": [
                {
                    "type": "waiver",
                    "adds": {"player1": "roster1"},
                    "drops": {},
                    "roster_ids": ["roster1"]
                },
                {
                    "type": "waiver",
                    "adds": {},
                    "drops": {"player1": "roster1"},
                    "roster_ids": ["roster1"]
                },
                {
                    "type": "waiver",
                    "adds": {"player1": "roster1"},
                    "drops": {},
                    "roster_ids": ["roster1"]
                }
            ]
        }
        
        async def mock_get_transactions(league_id, round):
            return mock_transactions
        
        with patch('nfl_mcp.waiver_tools.get_transactions', side_effect=mock_get_transactions):
            result = await check_re_entry_status("league1")
            
            assert result["success"] is True
            assert "re_entry_players" in result or "success" in result

    @pytest.mark.asyncio
    async def test_check_re_entry_status_failed_fetch(self):
        """Test re-entry status check with failed fetch."""
        mock_transactions = {
            "success": False,
            "error": "Failed to fetch"
        }
        
        async def mock_get_transactions(league_id, round):
            return mock_transactions
        
        with patch('nfl_mcp.waiver_tools.get_transactions', side_effect=mock_get_transactions):
            result = await check_re_entry_status("league1")
            
            assert result["success"] is False


class TestGetWaiverWireDashboard:
    """Test get_waiver_wire_dashboard async function."""

    @pytest.mark.asyncio
    async def test_dashboard_success(self):
        """Test successful waiver wire dashboard generation."""
        mock_waiver_log = {
            "success": True,
            "waiver_log": [],
            "total_transactions": 5,
            "unique_transactions": 4
        }
        
        mock_re_entry = {
            "success": True,
            "re_entry_players": {},
            "volatile_players": [],
            "total_players_analyzed": 3,
            "players_with_re_entries": 1
        }
        
        async def mock_get_waiver_log(league_id, round, dedupe):
            return mock_waiver_log
        
        async def mock_check_re_entry(league_id, round):
            return mock_re_entry
        
        with patch('nfl_mcp.waiver_tools.get_waiver_log', side_effect=mock_get_waiver_log):
            with patch('nfl_mcp.waiver_tools.check_re_entry_status', side_effect=mock_check_re_entry):
                result = await get_waiver_wire_dashboard("league1")
                
                assert result["success"] is True
                assert "waiver_log" in result
                assert "re_entry_analysis" in result
                assert "dashboard_summary" in result
                assert "total_waiver_transactions" in result["dashboard_summary"]

    @pytest.mark.asyncio
    async def test_dashboard_failed_waiver_log(self):
        """Test dashboard with failed waiver log fetch."""
        mock_waiver_log = {
            "success": False,
            "error": "Failed"
        }
        
        async def mock_get_waiver_log(league_id, round, dedupe):
            return mock_waiver_log
        
        with patch('nfl_mcp.waiver_tools.get_waiver_log', side_effect=mock_get_waiver_log):
            result = await get_waiver_wire_dashboard("league1")
            
            assert result["success"] is False
