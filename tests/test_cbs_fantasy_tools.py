"""
Tests for CBS Fantasy Football tools.
"""

import pytest
import httpx
from unittest.mock import Mock, patch, AsyncMock
from nfl_mcp import cbs_fantasy_tools


class TestGetCBSPlayerNews:
    """Tests for get_cbs_player_news function."""

    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        """Test successful fetch of CBS player news."""
        mock_html = """
        <html>
            <body>
                <article class="player-news-item">
                    <a class="player-name">Patrick Mahomes</a>
                    <h3 class="headline">Mahomes throws for 300 yards</h3>
                    <p class="description">Great performance in latest game</p>
                    <time datetime="2025-11-10">Nov 10, 2025</time>
                    <span class="position">QB</span>
                    <span class="team">KC</span>
                </article>
                <article class="player-news-item">
                    <a class="player-name">Travis Kelce</a>
                    <h3 class="headline">Kelce records 10 catches</h3>
                    <p class="description">Another strong game for the tight end</p>
                    <time datetime="2025-11-10">Nov 10, 2025</time>
                    <span class="position">TE</span>
                    <span class="team">KC</span>
                </article>
            </body>
        </html>
        """
        
        with patch('nfl_mcp.cbs_fantasy_tools.create_http_client') as mock_client_creator:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_creator.return_value = mock_client
            
            result = await cbs_fantasy_tools.get_cbs_player_news(limit=10)
            
            assert result["success"] is True
            assert "news" in result
            assert result["total_news"] >= 0
            assert result["source"] == "CBS Sports Fantasy Football"

    @pytest.mark.asyncio
    async def test_limit_validation(self):
        """Test that limit parameter is properly validated."""
        mock_html = "<html><body></body></html>"
        
        with patch('nfl_mcp.cbs_fantasy_tools.create_http_client') as mock_client_creator:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_creator.return_value = mock_client
            
            # Test with excessive limit
            result = await cbs_fantasy_tools.get_cbs_player_news(limit=1000)
            assert result["success"] is True  # Should cap to max limit
            
            # Test with default limit
            result = await cbs_fantasy_tools.get_cbs_player_news()
            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_http_error_handling(self):
        """Test handling of HTTP errors."""
        with patch('nfl_mcp.cbs_fantasy_tools.create_http_client') as mock_client_creator:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.HTTPStatusError(
                "404 Not Found",
                request=Mock(),
                response=Mock(status_code=404)
            ))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_creator.return_value = mock_client
            
            result = await cbs_fantasy_tools.get_cbs_player_news()
            
            assert result["success"] is False
            assert "error" in result


class TestGetCBSProjections:
    """Tests for get_cbs_projections function."""

    @pytest.mark.asyncio
    async def test_successful_fetch_with_week(self):
        """Test successful fetch of CBS projections with week parameter."""
        mock_html = """
        <html>
            <body>
                <table class="stats-table">
                    <thead>
                        <tr>
                            <th>Player</th>
                            <th>Team</th>
                            <th>Pass Yds</th>
                            <th>Pass TD</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td><a href="/player/123">Patrick Mahomes</a></td>
                            <td>KC</td>
                            <td>285</td>
                            <td>2</td>
                        </tr>
                    </tbody>
                </table>
            </body>
        </html>
        """
        
        with patch('nfl_mcp.cbs_fantasy_tools.create_http_client') as mock_client_creator:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_creator.return_value = mock_client
            
            result = await cbs_fantasy_tools.get_cbs_projections(
                position="QB",
                week=11,
                season=2025,
                scoring="ppr"
            )
            
            assert result["success"] is True
            assert "projections" in result
            assert result["week"] == 11
            assert result["position"] == "QB"
            assert result["season"] == 2025
            assert result["scoring"] == "ppr"

    @pytest.mark.asyncio
    async def test_missing_week_parameter(self):
        """Test that week parameter is required."""
        result = await cbs_fantasy_tools.get_cbs_projections(position="QB")
        
        assert result["success"] is False
        assert "error" in result
        assert "week" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_invalid_week_parameter(self):
        """Test validation of week parameter."""
        result = await cbs_fantasy_tools.get_cbs_projections(position="QB", week=25)
        
        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_position(self):
        """Test validation of position parameter."""
        result = await cbs_fantasy_tools.get_cbs_projections(position="INVALID", week=11)
        
        assert result["success"] is False
        assert "error" in result
        assert "position" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_valid_positions(self):
        """Test all valid positions are accepted."""
        mock_html = "<html><body><table class='stats'></table></body></html>"
        
        with patch('nfl_mcp.cbs_fantasy_tools.create_http_client') as mock_client_creator:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_creator.return_value = mock_client
            
            valid_positions = ['QB', 'RB', 'WR', 'TE', 'K', 'DST']
            
            for position in valid_positions:
                result = await cbs_fantasy_tools.get_cbs_projections(
                    position=position,
                    week=11
                )
                assert result["success"] is True, f"Position {position} should be valid"

    @pytest.mark.asyncio
    async def test_scoring_format_validation(self):
        """Test that invalid scoring formats default to ppr."""
        mock_html = "<html><body><table class='stats'></table></body></html>"
        
        with patch('nfl_mcp.cbs_fantasy_tools.create_http_client') as mock_client_creator:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_creator.return_value = mock_client
            
            result = await cbs_fantasy_tools.get_cbs_projections(
                position="QB",
                week=11,
                scoring="invalid_format"
            )
            
            assert result["success"] is True
            assert result["scoring"] == "ppr"  # Should default to ppr


class TestGetCBSExpertPicks:
    """Tests for get_cbs_expert_picks function."""

    @pytest.mark.asyncio
    async def test_successful_fetch_with_week(self):
        """Test successful fetch of CBS expert picks with week parameter."""
        mock_html = """
        <html>
            <body>
                <table class="picks-table">
                    <tr>
                        <td>
                            <a>Kansas City Chiefs</a> vs <a>Buffalo Bills</a>
                        </td>
                        <td>KC -3</td>
                        <td>BUF -3</td>
                    </tr>
                </table>
            </body>
        </html>
        """
        
        with patch('nfl_mcp.cbs_fantasy_tools.create_http_client') as mock_client_creator:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_creator.return_value = mock_client
            
            result = await cbs_fantasy_tools.get_cbs_expert_picks(week=10)
            
            assert result["success"] is True
            assert "picks" in result
            assert result["week"] == 10
            assert result["source"] == "CBS Sports Expert Picks"

    @pytest.mark.asyncio
    async def test_missing_week_parameter(self):
        """Test that week parameter is required."""
        result = await cbs_fantasy_tools.get_cbs_expert_picks()
        
        assert result["success"] is False
        assert "error" in result
        assert "week" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_invalid_week_parameter(self):
        """Test validation of week parameter."""
        result = await cbs_fantasy_tools.get_cbs_expert_picks(week=25)
        
        assert result["success"] is False
        assert "error" in result
        assert "week" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_valid_week_range(self):
        """Test that weeks 1-18 are valid."""
        mock_html = "<html><body><table class='picks'></table></body></html>"
        
        with patch('nfl_mcp.cbs_fantasy_tools.create_http_client') as mock_client_creator:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.text = mock_html
            mock_response.raise_for_status = Mock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_creator.return_value = mock_client
            
            # Test valid weeks
            for week in [1, 9, 18]:
                result = await cbs_fantasy_tools.get_cbs_expert_picks(week=week)
                assert result["success"] is True, f"Week {week} should be valid"
            
            # Test invalid weeks
            for week in [0, 19, 25]:
                result = await cbs_fantasy_tools.get_cbs_expert_picks(week=week)
                assert result["success"] is False, f"Week {week} should be invalid"
