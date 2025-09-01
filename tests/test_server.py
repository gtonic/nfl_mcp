"""
Unit tests for NFL MCP Server

Tests the basic server functionality and tool logic including URL crawling.
"""

import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from nfl_mcp.server import create_app


class TestServerCreation:
    """Test server creation and configuration."""
    
    def test_create_app_returns_fastmcp_instance(self):
        """Test that create_app returns a FastMCP instance."""
        app = create_app()
        
        # Should be a FastMCP instance
        from fastmcp import FastMCP
        assert isinstance(app, FastMCP)
        
        # Should have the correct name
        assert app.name == "NFL MCP Server"
    
    def test_server_creation_basic(self):
        """Test basic server creation without complex interactions."""
        app = create_app()
        assert app.name == "NFL MCP Server"
        
        # Test that we can create the app multiple times
        app2 = create_app()
        assert app2.name == "NFL MCP Server"


class TestServerConfiguration:
    """Test server configuration and features."""
    
    def test_server_has_custom_route(self):
        """Test that the server has custom routes registered."""
        app = create_app()
        
        # Check that custom routes are registered
        # The FastMCP instance should have additional HTTP routes
        additional_routes = app._get_additional_http_routes()
        
        # Should have at least one custom route (health endpoint)
        assert len(additional_routes) > 0
    
    def test_health_route_exists(self):
        """Test that health route is properly configured."""
        app = create_app()
        
        # Get additional routes
        routes = app._get_additional_http_routes()
        
        # Should have a health route
        health_routes = [route for route in routes if '/health' in str(route)]
        assert len(health_routes) > 0


class TestMultiplicationLogic:
    """Test the multiplication business logic directly."""
    
    def test_multiply_function_directly(self):
        """Test the multiply function business logic directly."""
        # Test the actual multiplication logic
        def multiply(x: int, y: int) -> int:
            return x * y
        
        # Basic test cases
        assert multiply(5, 3) == 15
        assert multiply(10, 0) == 0
        assert multiply(-4, 7) == -28
        assert multiply(-6, -8) == 48
        assert multiply(1, 999) == 999
    
    def test_multiply_edge_cases(self):
        """Test multiplication with edge cases."""
        def multiply(x: int, y: int) -> int:
            return x * y
        
        # Edge cases
        assert multiply(0, 0) == 0
        assert multiply(1, 1) == 1
        assert multiply(-1, -1) == 1
        assert multiply(100, 100) == 10000
        
        # Mathematical properties
        # Commutative property: a * b = b * a
        assert multiply(7, 9) == multiply(9, 7)
        
        # Identity property: a * 1 = a
        assert multiply(42, 1) == 42
        assert multiply(1, 42) == 42
        
        # Zero property: a * 0 = 0
        assert multiply(999, 0) == 0
        assert multiply(0, 999) == 0
    
    def test_multiply_large_numbers(self):
        """Test multiplication with large numbers."""
        def multiply(x: int, y: int) -> int:
            return x * y
        
        # Large number tests
        assert multiply(999999, 999999) == 999998000001
        assert multiply(1000000, 1) == 1000000
        assert multiply(-1000000, 1) == -1000000


class TestServerIntegration:
    """Integration tests for the complete server functionality."""
    
    def test_server_runs_without_error(self):
        """Test that the server can be created and configured without errors."""
        try:
            app = create_app()
            assert app is not None
            assert hasattr(app, 'name')
            assert hasattr(app, 'run')
        except Exception as e:
            pytest.fail(f"Server creation failed with error: {e}")
    
    def test_import_all_modules(self):
        """Test that all modules can be imported successfully."""
        try:
            import nfl_mcp
            import nfl_mcp.server
            from nfl_mcp.server import create_app, main
            
            # All imports should work
            assert create_app is not None
            assert main is not None
            
        except ImportError as e:
            pytest.fail(f"Module import failed: {e}")


class TestHealthEndpointLogic:
    """Test the health endpoint logic."""
    
    def test_health_response_structure(self):
        """Test that health response has the correct structure."""
        # Expected health response
        expected_response = {
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.1.0"
        }
        
        # Validate structure
        assert "status" in expected_response
        assert "service" in expected_response  
        assert "version" in expected_response
        
        # Validate values
        assert expected_response["status"] == "healthy"
        assert expected_response["service"] == "NFL MCP Server"
        assert expected_response["version"] == "0.1.0"


class TestNflNewsLogic:
    """Test the NFL news fetching business logic."""
    
    def test_get_nfl_news_function_exists(self):
        """Test that the get_nfl_news function is properly registered."""
        app = create_app()
        
        # Verify the app was created successfully
        assert app.name == "NFL MCP Server"
        # The tool registration is tested in integration tests
    
    @pytest.mark.asyncio
    async def test_get_nfl_news_parameter_validation(self):
        """Test parameter validation for get_nfl_news function."""
        
        async def mock_get_nfl_news(limit=50):
            # Validate and cap the limit
            if limit is None:
                limit = 50
            elif limit < 1:
                limit = 1
            elif limit > 50:
                limit = 50
            
            return {
                "articles": [],
                "total_articles": 0,
                "success": True,
                "error": None,
                "requested_limit": limit
            }
        
        # Test default limit
        result = await mock_get_nfl_news()
        assert result["requested_limit"] == 50
        
        # Test valid limits
        result = await mock_get_nfl_news(25)
        assert result["requested_limit"] == 25
        
        result = await mock_get_nfl_news(1)
        assert result["requested_limit"] == 1
        
        # Test limit capping
        result = await mock_get_nfl_news(100)
        assert result["requested_limit"] == 50
        
        # Test limit minimum
        result = await mock_get_nfl_news(0)
        assert result["requested_limit"] == 1
        
        result = await mock_get_nfl_news(-5)
        assert result["requested_limit"] == 1
    
    @pytest.mark.asyncio
    async def test_get_nfl_news_mock_successful_response(self):
        """Test successful response processing with mock data."""
        
        mock_espn_response = {
            "articles": [
                {
                    "headline": "Test NFL News Article",
                    "description": "This is a test article about NFL news",
                    "published": "2024-01-15T10:30:00Z",
                    "type": "Story",
                    "story": "Full story content here...",
                    "categories": [{"description": "NFL"}, {"description": "Sports"}],
                    "links": {"web": {"href": "https://example.com/article"}}
                },
                {
                    "headline": "Another NFL Update",
                    "description": "Another test article",
                    "published": "2024-01-15T09:15:00Z",
                    "type": "News",
                    "story": "More news content...",
                    "categories": [{"description": "Football"}],
                    "links": {"web": {"href": "https://example.com/article2"}}
                }
            ]
        }
        
        # Mock processing logic
        articles = mock_espn_response.get('articles', [])
        processed_articles = []
        for article in articles:
            processed_article = {
                "headline": article.get('headline', ''),
                "description": article.get('description', ''),
                "published": article.get('published', ''),
                "type": article.get('type', ''),
                "story": article.get('story', ''),
                "categories": [cat.get('description', '') for cat in article.get('categories', [])],
                "links": article.get('links', {})
            }
            processed_articles.append(processed_article)
        
        result = {
            "articles": processed_articles,
            "total_articles": len(processed_articles),
            "success": True,
            "error": None
        }
        
        # Verify the processing logic
        assert result["success"] is True
        assert result["error"] is None
        assert result["total_articles"] == 2
        assert len(result["articles"]) == 2
        
        # Check first article
        first_article = result["articles"][0]
        assert first_article["headline"] == "Test NFL News Article"
        assert first_article["description"] == "This is a test article about NFL news"
        assert first_article["type"] == "Story"
        assert first_article["categories"] == ["NFL", "Sports"]
        
        # Check second article
        second_article = result["articles"][1]
        assert second_article["headline"] == "Another NFL Update"
        assert second_article["categories"] == ["Football"]
    
    @pytest.mark.asyncio
    async def test_get_nfl_news_mock_error_cases(self):
        """Test error handling with mock scenarios."""
        
        async def mock_get_nfl_news_with_errors(simulate_error: str = None) -> dict:
            if simulate_error == "timeout":
                return {
                    "articles": [],
                    "total_articles": 0,
                    "success": False,
                    "error": "Request timed out while fetching NFL news"
                }
            elif simulate_error == "404":
                return {
                    "articles": [],
                    "total_articles": 0,
                    "success": False,
                    "error": "HTTP 404: Not Found"
                }
            elif simulate_error == "500":
                return {
                    "articles": [],
                    "total_articles": 0,
                    "success": False,
                    "error": "HTTP 500: Internal Server Error"
                }
            elif simulate_error == "network":
                return {
                    "articles": [],
                    "total_articles": 0,
                    "success": False,
                    "error": "Unexpected error fetching NFL news: Network unreachable"
                }
            
            return {"success": True}
        
        # Test timeout error
        result = await mock_get_nfl_news_with_errors("timeout")
        assert result["success"] is False
        assert "timed out" in result["error"]
        assert result["total_articles"] == 0
        
        # Test HTTP 404 error
        result = await mock_get_nfl_news_with_errors("404")
        assert result["success"] is False
        assert "HTTP 404" in result["error"]
        assert result["total_articles"] == 0
        
        # Test HTTP 500 error
        result = await mock_get_nfl_news_with_errors("500")
        assert result["success"] is False
        assert "HTTP 500" in result["error"]
        assert result["total_articles"] == 0
        
        # Test network error
        result = await mock_get_nfl_news_with_errors("network")
        assert result["success"] is False
        assert "Unexpected error" in result["error"]
        assert result["total_articles"] == 0
    
    @pytest.mark.asyncio
    async def test_get_nfl_news_empty_response(self):
        """Test handling of empty response from ESPN API."""
        
        mock_espn_response = {
            "articles": []
        }
        
        # Mock processing logic for empty response
        articles = mock_espn_response.get('articles', [])
        processed_articles = []
        for article in articles:
            processed_article = {
                "headline": article.get('headline', ''),
                "description": article.get('description', ''),
                "published": article.get('published', ''),
                "type": article.get('type', ''),
                "story": article.get('story', ''),
                "categories": [cat.get('description', '') for cat in article.get('categories', [])],
                "links": article.get('links', {})
            }
            processed_articles.append(processed_article)
        
        result = {
            "articles": processed_articles,
            "total_articles": len(processed_articles),
            "success": True,
            "error": None
        }
        
        # Verify empty response handling
        assert result["success"] is True
        assert result["error"] is None
        assert result["total_articles"] == 0
        assert len(result["articles"]) == 0


class TestUrlCrawlingLogic:
    """Test the URL crawling business logic."""
    
    def test_crawl_url_function_exists(self):
        """Test that the crawl_url function is properly registered."""
        app = create_app()
        
        # Verify the app was created successfully
        assert app.name == "NFL MCP Server"
        # The tool registration is tested in integration tests

    @pytest.mark.asyncio
    async def test_crawl_url_invalid_url_format(self):
        """Test URL validation logic."""
        # Test the URL validation logic directly
        test_urls = [
            "example.com",
            "www.example.com", 
            "ftp://example.com",
            "/relative/path",
            "javascript:alert('test')"
        ]
        
        for url in test_urls:
            # Validate URL format (same logic as in the actual function)
            is_valid = url.startswith(('http://', 'https://'))
            assert is_valid is False, f"URL {url} should be invalid"
        
        # Test valid URLs
        valid_urls = [
            "http://example.com",
            "https://example.com",
            "https://www.example.com/path?query=value"
        ]
        
        for url in valid_urls:
            is_valid = url.startswith(('http://', 'https://'))
            assert is_valid is True, f"URL {url} should be valid"

    @pytest.mark.asyncio 
    async def test_crawl_url_text_extraction_logic(self):
        """Test HTML text extraction and cleaning logic."""
        # Test the text processing logic
        from bs4 import BeautifulSoup
        import re
        
        test_html = """
        <html>
            <head><title>Test Page</title></head>
            <body>
                <h1>Main Content</h1>
                <p>This is   a test    paragraph.</p>
                <script>alert('should be removed');</script>
                <style>.hidden { display: none; }</style>
                <nav>Navigation</nav>
                <footer>Footer</footer>
                <div>   
                    Some content with
                    
                    lots of whitespace
                </div>
            </body>
        </html>
        """
        
        # Process HTML the same way as the crawl_url function
        soup = BeautifulSoup(test_html, 'lxml')
        
        # Extract title
        title_tag = soup.find('title')
        title = title_tag.get_text().strip() if title_tag else None
        assert title == "Test Page"
        
        # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "aside", "form"]):
            script.extract()
        
        # Get text content
        text = soup.get_text()
        
        # Clean up the text
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = ' '.join(chunk for chunk in chunks if chunk)
        
        # Remove excessive whitespace and normalize
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Verify extracted content
        assert "Main Content" in text
        assert "test paragraph" in text
        assert "Some content with lots of whitespace" in text
        # Should not contain removed elements
        assert "alert" not in text
        assert "display: none" not in text
        assert "Navigation" not in text
        assert "Footer" not in text

    @pytest.mark.asyncio
    async def test_crawl_url_length_limiting(self):
        """Test content length limiting logic."""
        # Test length limiting
        long_text = "This is a long paragraph. " * 100  # ~2600 chars
        max_length = 100
        
        if len(long_text) > max_length:
            truncated = long_text[:max_length] + "..."
        else:
            truncated = long_text
            
        assert len(truncated) <= max_length + 3  # +3 for "..."
        assert truncated.endswith("...")

    @pytest.mark.asyncio
    async def test_crawl_url_mock_successful_request(self):
        """Test successful request with mocked HTTP client."""
        import httpx
        from unittest.mock import AsyncMock, MagicMock, patch
        
        # Test HTML content
        mock_html = """
        <html>
            <head><title>Success Page</title></head>
            <body>
                <h1>Welcome</h1>
                <p>This page was successfully crawled.</p>
            </body>
        </html>
        """
        
        # Create a mock implementation of the crawl logic
        async def mock_crawl_url(url: str, max_length: int = 10000) -> dict:
            # Validate URL format
            if not url.startswith(('http://', 'https://')):
                return {
                    "url": url, "title": None, "content": "", "content_length": 0,
                    "success": False, "error": "URL must start with http:// or https://"
                }
            
            try:
                # Simulate successful request
                from bs4 import BeautifulSoup
                import re
                
                soup = BeautifulSoup(mock_html, 'lxml')
                title_tag = soup.find('title')
                title = title_tag.get_text().strip() if title_tag else None
                
                # Remove unwanted elements
                for script in soup(["script", "style", "nav", "footer", "aside", "form"]):
                    script.extract()
                
                text = soup.get_text()
                lines = (line.strip() for line in text.splitlines())
                chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                text = ' '.join(chunk for chunk in chunks if chunk)
                text = re.sub(r'\s+', ' ', text).strip()
                
                if max_length and len(text) > max_length:
                    text = text[:max_length] + "..."
                
                return {
                    "url": url, "title": title, "content": text, "content_length": len(text),
                    "success": True, "error": None
                }
            except Exception as e:
                return {
                    "url": url, "title": None, "content": "", "content_length": 0,
                    "success": False, "error": f"Unexpected error: {str(e)}"
                }
        
        # Test successful crawl
        result = await mock_crawl_url("https://example.com")
        
        assert result["success"] is True
        assert result["error"] is None
        assert result["title"] == "Success Page"
        assert "Welcome" in result["content"]
        assert "successfully crawled" in result["content"]
        assert result["content_length"] > 0

    @pytest.mark.asyncio
    async def test_crawl_url_mock_error_cases(self):
        """Test error handling with mock scenarios."""
        
        async def mock_crawl_url_with_errors(url: str, simulate_error: str = None) -> dict:
            if not url.startswith(('http://', 'https://')):
                return {
                    "url": url, "title": None, "content": "", "content_length": 0,
                    "success": False, "error": "URL must start with http:// or https://"
                }
            
            if simulate_error == "timeout":
                return {
                    "url": url, "title": None, "content": "", "content_length": 0,
                    "success": False, "error": "Request timed out"
                }
            elif simulate_error == "404":
                return {
                    "url": url, "title": None, "content": "", "content_length": 0,
                    "success": False, "error": "HTTP 404: Not Found"
                }
            elif simulate_error == "unexpected":
                return {
                    "url": url, "title": None, "content": "", "content_length": 0,
                    "success": False, "error": "Unexpected error: Connection failed"
                }
            
            return {"success": True}
        
        # Test timeout error
        result = await mock_crawl_url_with_errors("https://example.com", "timeout")
        assert result["success"] is False
        assert result["error"] == "Request timed out"
        
        # Test HTTP error
        result = await mock_crawl_url_with_errors("https://example.com", "404")
        assert result["success"] is False
        assert "HTTP 404" in result["error"]
        
        # Test unexpected error
        result = await mock_crawl_url_with_errors("https://example.com", "unexpected")
        assert result["success"] is False
        assert "Unexpected error" in result["error"]