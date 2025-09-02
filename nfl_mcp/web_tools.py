"""
Web crawling MCP tools for the NFL MCP Server.

This module contains MCP tools for crawling and extracting content from web pages.
"""

import re
import httpx
from typing import Optional
from bs4 import BeautifulSoup

from .config import get_http_headers, create_http_client, is_valid_url
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors, handle_validation_error
)


@handle_http_errors(
    default_data={"url": None, "title": None, "content": "", "content_length": 0},
    operation_name="crawling URL"
)
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
        - error_type: Type of error (if any)
    """
    # Validate URL format for security
    if not is_valid_url(url):
        return handle_validation_error(
            "URL must start with http:// or https://",
            {"url": url, "title": None, "content": "", "content_length": 0}
        )
    
    headers = get_http_headers("web_crawler")
    
    async with create_http_client() as client:
        # Fetch the URL
        response = await client.get(url, headers=headers)
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
        
        return create_success_response({
            "url": url,
            "title": title,
            "content": text,
            "content_length": len(text)
        })