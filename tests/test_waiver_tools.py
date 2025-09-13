"""
Test the waiver_tools module functionality.
"""

import pytest
from unittest.mock import AsyncMock, patch

from nfl_mcp import waiver_tools


class TestWaiverToolsModule:
    """Test the waiver_tools module functionality."""
    
    def test_module_imports_successfully(self):
        """Test that the waiver_tools module can be imported."""
        assert hasattr(waiver_tools, 'get_waiver_log')
        assert hasattr(waiver_tools, 'check_re_entry_status')
        assert hasattr(waiver_tools, 'get_waiver_wire_dashboard')
        assert hasattr(waiver_tools, 'WaiverAnalyzer')
    
    @pytest.mark.asyncio
    async def test_get_waiver_log_function_exists(self):
        """Test that get_waiver_log function exists and has correct signature."""
        func = getattr(waiver_tools, 'get_waiver_log')
        assert callable(func)
        
        # Test with mock transaction data
        mock_transactions = {
            "success": True,
            "transactions": [
                {
                    "transaction_id": "tx1",
                    "type": "waiver",
                    "status": "complete",
                    "created": 1640995200,  # 2022-01-01
                    "adds": {"player1": 1},
                    "drops": {"player2": 1},
                    "roster_ids": [1]
                },
                {
                    "transaction_id": "tx2",
                    "type": "free_agent",
                    "status": "complete", 
                    "created": 1641081600,  # 2022-01-02
                    "adds": {"player3": 2},
                    "drops": {},
                    "roster_ids": [2]
                }
            ]
        }
        
        with patch('nfl_mcp.waiver_tools.get_transactions', new_callable=AsyncMock) as mock_get_tx:
            mock_get_tx.return_value = mock_transactions
            
            result = await func("test_league_id")
            
            # Verify result structure
            assert "waiver_log" in result
            assert "duplicates_found" in result
            assert "total_transactions" in result
            assert "unique_transactions" in result
            assert "success" in result
            assert "error" in result
            assert result["success"] is True
    
    @pytest.mark.asyncio 
    async def test_check_re_entry_status_function_exists(self):
        """Test that check_re_entry_status function exists and has correct signature."""
        func = getattr(waiver_tools, 'check_re_entry_status')
        assert callable(func)
        
        # Test with mock transaction data showing re-entry pattern
        mock_transactions = {
            "success": True,
            "transactions": [
                {
                    "transaction_id": "tx1",
                    "type": "waiver",
                    "status": "complete",
                    "created": 1640995200,  # Drop player1
                    "adds": {},
                    "drops": {"player1": 1},
                    "roster_ids": [1]
                },
                {
                    "transaction_id": "tx2", 
                    "type": "waiver",
                    "status": "complete",
                    "created": 1641081600,  # Re-add player1
                    "adds": {"player1": 2},
                    "drops": {},
                    "roster_ids": [2]
                }
            ]
        }
        
        with patch('nfl_mcp.waiver_tools.get_transactions', new_callable=AsyncMock) as mock_get_tx:
            mock_get_tx.return_value = mock_transactions
            
            result = await func("test_league_id")
            
            # Verify result structure
            assert "re_entry_players" in result
            assert "volatile_players" in result
            assert "total_players_analyzed" in result
            assert "players_with_re_entries" in result
            assert "success" in result
            assert "error" in result
            assert result["success"] is True
    
    @pytest.mark.asyncio
    async def test_get_waiver_wire_dashboard_function_exists(self):
        """Test that get_waiver_wire_dashboard function exists and has correct signature."""
        func = getattr(waiver_tools, 'get_waiver_wire_dashboard')
        assert callable(func)
        
        # Mock both waiver log and re-entry results
        mock_waiver_log = {
            "success": True,
            "waiver_log": [],
            "duplicates_found": [],
            "total_transactions": 2,
            "unique_transactions": 2
        }
        
        mock_re_entry = {
            "success": True,
            "re_entry_players": {},
            "volatile_players": [],
            "total_players_analyzed": 3,
            "players_with_re_entries": 1
        }
        
        with patch('nfl_mcp.waiver_tools.get_waiver_log', new_callable=AsyncMock) as mock_log, \
             patch('nfl_mcp.waiver_tools.check_re_entry_status', new_callable=AsyncMock) as mock_re_entry_func:
            
            mock_log.return_value = mock_waiver_log
            mock_re_entry_func.return_value = mock_re_entry
            
            result = await func("test_league_id")
            
            # Verify result structure
            assert "waiver_log" in result
            assert "re_entry_analysis" in result 
            assert "dashboard_summary" in result
            assert "volatile_players" in result
            assert "success" in result
            assert "error" in result
            assert result["success"] is True
    
    def test_waiver_analyzer_class_exists(self):
        """Test that WaiverAnalyzer class exists and can be instantiated."""
        analyzer_class = getattr(waiver_tools, 'WaiverAnalyzer')
        analyzer = analyzer_class()
        
        assert hasattr(analyzer, '_extract_waiver_transactions')
        assert hasattr(analyzer, '_deduplicate_waiver_log')
        assert hasattr(analyzer, '_track_re_entries')
        assert hasattr(analyzer, 'waiver_cache')
        assert hasattr(analyzer, 're_entry_tracking')
    
    def test_waiver_analyzer_extract_waiver_transactions(self):
        """Test WaiverAnalyzer's _extract_waiver_transactions method."""
        analyzer = waiver_tools.WaiverAnalyzer()
        
        transactions = [
            {
                "transaction_id": "tx1",
                "type": "waiver",
                "status": "complete",
                "created": 1640995200,
                "adds": {"player1": 1},
                "drops": {"player2": 1},
                "roster_ids": [1]
            },
            {
                "transaction_id": "tx2",
                "type": "trade", 
                "status": "complete",
                "created": 1641081600,
                "adds": {"player3": 1},
                "drops": {"player4": 1},
                "roster_ids": [1, 2]
            },
            {
                "transaction_id": "tx3",
                "type": "free_agent",
                "status": "complete",
                "created": 1641168000,
                "adds": {"player5": 2},
                "drops": {},
                "roster_ids": [2]
            }
        ]
        
        waiver_transactions = analyzer._extract_waiver_transactions(transactions)
        
        # Should extract 2 transactions (waiver and free_agent, not trade)
        assert len(waiver_transactions) == 2
        assert waiver_transactions[0]["type"] == "waiver"
        assert waiver_transactions[1]["type"] == "free_agent"
    
    def test_waiver_analyzer_deduplicate_waiver_log(self):
        """Test WaiverAnalyzer's _deduplicate_waiver_log method."""
        analyzer = waiver_tools.WaiverAnalyzer()
        
        # Create transactions with one duplicate
        transactions = [
            {
                "transaction_id": "tx1",
                "type": "waiver",
                "created": 1640995200,
                "adds": {"player1": 1},
                "drops": {"player2": 1},
                "roster_ids": [1]
            },
            {
                "transaction_id": "tx2",
                "type": "waiver", 
                "created": 1640995200,  # Same timestamp and players = duplicate
                "adds": {"player1": 1},
                "drops": {"player2": 1},
                "roster_ids": [1]
            },
            {
                "transaction_id": "tx3",
                "type": "waiver",
                "created": 1641081600,
                "adds": {"player3": 2},
                "drops": {},
                "roster_ids": [2]
            }
        ]
        
        unique, duplicates = analyzer._deduplicate_waiver_log(transactions)
        
        # Should have 2 unique and 1 duplicate
        assert len(unique) == 2
        assert len(duplicates) == 1
        assert duplicates[0]["transaction_id"] == "tx2"
    
    def test_waiver_analyzer_track_re_entries(self):
        """Test WaiverAnalyzer's _track_re_entries method."""
        analyzer = waiver_tools.WaiverAnalyzer()
        
        # Create transaction pattern: player1 dropped, then re-added
        transactions = [
            {
                "transaction_id": "tx1",
                "type": "waiver",
                "created": 1640995200,
                "adds": {},
                "drops": {"player1": 1},
                "roster_ids": [1]
            },
            {
                "transaction_id": "tx2",
                "type": "waiver",
                "created": 1641081600,
                "adds": {"player1": 2},
                "drops": {},
                "roster_ids": [2]
            }
        ]
        
        re_entry_analysis = analyzer._track_re_entries(transactions)
        
        # Should detect re-entry for player1
        assert "player1" in re_entry_analysis
        player_analysis = re_entry_analysis["player1"]
        
        assert player_analysis["drops_count"] == 1
        assert player_analysis["adds_count"] == 1
        assert len(player_analysis["re_entries"]) == 1
        
        re_entry = player_analysis["re_entries"][0]
        assert re_entry["dropped_by_roster"] == 1
        assert re_entry["added_by_roster"] == 2
        assert re_entry["same_roster"] is False