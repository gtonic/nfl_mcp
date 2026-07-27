"""Tests for web_tools module (crawl_url)."""
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nfl_mcp.web_tools import crawl_url


def _mock_response(status_code=200, text="", headers=None):
    """Build a minimal mock httpx response.

    ``raise_for_status`` is a *sync* Mock because httpx's real method is
    synchronous (and crawl_url calls it without ``await``).
    """
    resp = AsyncMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    resp.raise_for_status = Mock()
    return resp


def _mock_client(response=None, responses=None):
    """Build a mock async http client returning one or a sequence of responses."""
    client = AsyncMock()
    if responses is not None:
        client.get = AsyncMock(side_effect=responses)
    else:
        client.get.return_value = response
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# The mocked-client tests below exercise parsing/redirect logic, not the SSRF
# gate, so they patch is_safe_public_url to stay fully offline (no DNS).
_ALLOW = {"return_value": (True, None)}


class TestCrawlUrl:
    """Test URL crawling functionality (parsing behavior)."""

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
        client = _mock_client(_mock_response(200, mock_html))

        with patch('nfl_mcp.web_tools.is_safe_public_url', **_ALLOW), \
                patch('nfl_mcp.web_tools.create_http_client', return_value=client):
            result = await crawl_url("https://example.com")

        assert result["success"] is True
        assert result["title"] == "Test Page"
        assert "Test content" in result["content"]
        assert result["content_length"] > 0

    @pytest.mark.asyncio
    async def test_crawl_url_invalid_url(self):
        """Invalid scheme is rejected before any network access."""
        result = await crawl_url("not-a-url")

        assert result["success"] is False
        assert "URL must start with http://" in result["error"]

    @pytest.mark.asyncio
    async def test_crawl_url_max_length(self):
        """Test URL crawling with max_length parameter."""
        mock_html = '<html><body>' + 'x' * 200 + '</body></html>'
        client = _mock_client(_mock_response(200, mock_html))

        with patch('nfl_mcp.web_tools.is_safe_public_url', **_ALLOW), \
                patch('nfl_mcp.web_tools.create_http_client', return_value=client):
            result = await crawl_url("https://example.com", max_length=50)

        assert result["success"] is True
        assert result["content_length"] <= 53  # 50 + "..."

    @pytest.mark.asyncio
    async def test_crawl_url_http_error(self):
        """Test crawling with HTTP error."""
        resp = _mock_response(404)
        resp.raise_for_status.side_effect = Exception("404")
        client = _mock_client(resp)

        with patch('nfl_mcp.web_tools.is_safe_public_url', **_ALLOW), \
                patch('nfl_mcp.web_tools.create_http_client', return_value=client):
            result = await crawl_url("https://example.com")

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_crawl_url_no_title(self):
        """Test crawling with no title tag."""
        client = _mock_client(_mock_response(200, '<html><body><p>Test</p></body></html>'))

        with patch('nfl_mcp.web_tools.is_safe_public_url', **_ALLOW), \
                patch('nfl_mcp.web_tools.create_http_client', return_value=client):
            result = await crawl_url("https://example.com")

        assert result["success"] is True
        assert result["title"] is None


class TestCrawlUrlSSRF:
    """SSRF protections for crawl_url (the only arbitrary-URL tool)."""

    @pytest.mark.asyncio
    async def test_blocks_cloud_metadata_ip(self):
        """The 169.254.169.254 cloud-metadata endpoint is blocked pre-fetch."""
        result = await crawl_url("http://169.254.169.254/latest/meta-data/")
        assert result["success"] is False
        assert "Blocked non-public address" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_loopback_ip(self):
        result = await crawl_url("http://127.0.0.1:8080/admin")
        assert result["success"] is False
        assert "Blocked non-public address" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_ipv6_loopback(self):
        result = await crawl_url("http://[::1]/")
        assert result["success"] is False
        assert "Blocked non-public address" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_host_resolving_to_private_ip(self):
        """A public-looking hostname that resolves to a private IP is blocked."""
        with patch('nfl_mcp.config.resolve_host_addresses', return_value=['10.0.0.5']):
            result = await crawl_url("http://internal.example.test/")
        assert result["success"] is False
        assert "Blocked non-public address" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_redirect_into_private_network(self):
        """A safe URL that 302-redirects to a private IP is blocked at the hop."""
        redirect = _mock_response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )
        client = _mock_client(redirect)

        # Initial host is a public IP literal (no DNS); the redirect target is
        # the metadata IP and must be refused before it is fetched.
        with patch('nfl_mcp.web_tools.create_http_client', return_value=client):
            result = await crawl_url("http://93.184.216.34/")

        assert result["success"] is False
        assert "Blocked redirect" in result["error"]

    @pytest.mark.asyncio
    async def test_follows_safe_redirect(self):
        """A redirect to another public host is followed and its content parsed."""
        redirect = _mock_response(302, headers={"location": "http://93.184.216.35/next"})
        final = _mock_response(200, "<html><title>Landed</title><body>ok</body></html>")
        client = _mock_client(responses=[redirect, final])

        with patch('nfl_mcp.web_tools.create_http_client', return_value=client):
            result = await crawl_url("http://93.184.216.34/")

        assert result["success"] is True
        assert result["title"] == "Landed"

    @pytest.mark.asyncio
    async def test_too_many_redirects(self):
        """A redirect loop terminates with a 'too many redirects' error."""
        redirect = _mock_response(302, headers={"location": "http://93.184.216.34/loop"})
        client = _mock_client(redirect)  # always returns a redirect

        with patch('nfl_mcp.web_tools.create_http_client', return_value=client):
            result = await crawl_url("http://93.184.216.34/")

        assert result["success"] is False
        assert "Too many redirects" in result["error"]
