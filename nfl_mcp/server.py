#!/usr/bin/env python3
"""
NFL MCP Server

A FastMCP server that provides:
- Health endpoint (non-MCP REST endpoint)
- Multiply tool (MCP tool for arithmetic operations)
- URL crawling tool (MCP tool for web content extraction)
"""

import re
import httpx
from typing import Optional
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from starlette.responses import JSONResponse


def create_app() -> FastMCP:
    """Create and configure the FastMCP server application."""
    
    # Create FastMCP server instance
    mcp = FastMCP(
        name="NFL MCP Server"
    )
    
    # Health endpoint (non-MCP, directly exposed REST endpoint)
    @mcp.custom_route(path="/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint for monitoring server status."""
        return JSONResponse({
            "status": "healthy",
            "service": "NFL MCP Server",
            "version": "0.1.0"
        })
    
    # MCP Tool: Multiply two integers
    @mcp.tool
    def multiply(x: int, y: int) -> int:
        """
        Multiply two integer numbers and return the result.
        
        Args:
            x: First integer to multiply
            y: Second integer to multiply
            
        Returns:
            The product of x and y
        """
        return x * y
    
    # MCP Tool: Crawl URL and extract content
    @mcp.tool
    async def crawl_url(url: str, max_length: Optional[int] = 10000) -> dict:
        """
        Crawl a URL and extract its text content in a format understandable by LLMs.
        
        This tool fetches a web page, extracts the main text content, and returns
        it in a clean, structured format suitable for LLM processing.
        
        Args:
            url: The URL to crawl (must include http:// or https://)
            max_length: Maximum length of extracted text (default: 10000 characters)
            
        Returns:
            A dictionary containing:
            - url: The crawled URL
            - title: Page title (if available)
            - content: Cleaned text content
            - content_length: Length of extracted content
            - success: Whether the crawl was successful
            - error: Error message (if any)
        """
        # Validate URL format
        if not url.startswith(('http://', 'https://')):
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": "URL must start with http:// or https://"
            }
        
        try:
            # Set reasonable timeout and user agent
            timeout = httpx.Timeout(30.0, connect=10.0)
            headers = {
                "User-Agent": "NFL-MCP-Server/0.1.0 (Web Content Extractor)"
            }
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                # Fetch the URL
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                
                # Parse HTML content
                soup = BeautifulSoup(response.text, 'lxml')
                
                # Extract title
                title_tag = soup.find('title')
                title = title_tag.get_text().strip() if title_tag else None
                
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
                
                # Apply length limit if specified
                if max_length and len(text) > max_length:
                    text = text[:max_length] + "..."
                
                return {
                    "url": url,
                    "title": title,
                    "content": text,
                    "content_length": len(text),
                    "success": True,
                    "error": None
                }
                
        except httpx.TimeoutException:
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": "Request timed out"
            }
        except httpx.HTTPStatusError as e:
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
            }
        except Exception as e:
            return {
                "url": url,
                "title": None,
                "content": "",
                "content_length": 0,
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }
    
    return mcp


def main():
    """Main entry point for the server."""
    app = create_app()
    
    # Run the server with HTTP transport on port 9000
    app.run(transport="http", port=9000, host="0.0.0.0")


if __name__ == "__main__":
    main()