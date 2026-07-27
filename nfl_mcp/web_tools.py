"""
Web crawling MCP tools for the NFL MCP Server.

This module contains MCP tools for crawling and extracting content from web pages.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .config import create_http_client, get_http_headers, is_safe_public_url

# Maximum number of redirect hops crawl_url will follow (each re-validated).
MAX_CRAWL_REDIRECTS = 5
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
from .errors import create_success_response, handle_http_errors, handle_validation_error


@handle_http_errors(
    default_data={"url": None, "title": None, "content": "", "content_length": 0},
    operation_name="crawling URL"
)
async def crawl_url(url: str, max_length: int | None = 10000) -> dict:
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
    _error_data = {"url": url, "title": None, "content": "", "content_length": 0}

    # SSRF protection: validate scheme + resolved IP before contacting the host.
    ok, reason = is_safe_public_url(url)
    if not ok:
        return handle_validation_error(reason, _error_data)

    headers = get_http_headers("web_crawler")

    # Follow redirects manually so every hop is re-validated — otherwise a
    # public URL could 3xx-redirect into the private network / cloud metadata.
    async with create_http_client(follow_redirects=False) as client:
        current_url = url
        for _ in range(MAX_CRAWL_REDIRECTS + 1):
            response = await client.get(current_url, headers=headers)

            if response.status_code in _REDIRECT_STATUS:
                location = response.headers.get("location")
                if not location:
                    break  # malformed redirect; fall through to normal handling
                next_url = urljoin(current_url, location)
                ok, reason = is_safe_public_url(next_url)
                if not ok:
                    return handle_validation_error(f"Blocked redirect: {reason}", _error_data)
                current_url = next_url
                continue

            # Non-redirect response: process it.
            break
        else:
            return handle_validation_error(
                f"Too many redirects (>{MAX_CRAWL_REDIRECTS})", _error_data
            )

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
