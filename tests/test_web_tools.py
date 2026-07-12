"""Tests for web_tools module (crawl_url)."""
import pytest
from unittest.mock import AsyncMock, patch
from nfl_mcp.web_tools import crawl_url


class TestCrawlUrl:
    """Test URL crawling functionality."""

    @pytest.mark.asyncio
    async def test_crawl_url_success(self):
        """Test successful URL crawling."""
        mock_html = """
        <html>
            <head><title>Test Page</title></head>
            <body>
                <script>alert('test');</script>
                <p>Test content</p>
            </body>
        </html>
        """
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = mock_html
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch('nfl_mcp.web_tools.create_http_client', return_value=mock_client):
            result = await crawl_url("https://example.com")

        assert result["success"] is True
        assert result["title"] == "Test Page"
        assert "Test content" in result["content"]
        assert result["content_length"] > 0

    @pytest.mark.asyncio
    async def test_crawl_url_invalid_url(self):
        """Test crawling with invalid URL."""
        result = await crawl_url("not-a-url")
        
        assert result["success"] is False
        assert "URL must start with http://" in result["error"]

    @pytest.mark.asyncio
    async def test_crawl_url_max_length(self):
        """Test URL crawling with max_length parameter."""
        mock_html = '<html><body>' + 'x' * 200 + '</body></html>'
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = mock_html
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch('nfl_mcp.web_tools.create_http_client', return_value=mock_client):
            result = await crawl_url("https://example.com", max_length=50)

        assert result["success"] is True
        assert result["content_length"] <= 53  # 50 + "..."

    @pytest.mark.asyncio
    async def test_crawl_url_http_error(self):
        """Test crawling with HTTP error."""
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = Exception("404")

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch('nfl_mcp.web_tools.create_http_client', return_value=mock_client):
            result = await crawl_url("https://example.com")

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_crawl_url_no_title(self):
        """Test crawling with no title tag."""
        mock_html = '<html><body><p>Test</p></body></html>'
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = mock_html
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch('nfl_mcp.web_tools.create_http_client', return_value=mock_client):
            result = await crawl_url("https://example.com")

        assert result["success"] is True
        assert result["title"] is None
