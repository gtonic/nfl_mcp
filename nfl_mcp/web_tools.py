"""
Web crawling MCP tools for the NFL MCP Server.

This module contains MCP tools for crawling and extracting content from web pages.
"""

import asyncio
import ipaddress
import os
import re
import zlib
from urllib.parse import urljoin

import httpcore
import httpx
from bs4 import BeautifulSoup

from .config import (
    allow_private_urls,
    create_http_client,
    get_http_headers,
    resolve_safe_url,
    validate_limit,
)
from .errors import (
    ErrorType,
    create_error_response,
    create_success_response,
    handle_http_errors,
    handle_validation_error,
)

# Maximum number of redirect hops crawl_url will follow (each re-validated).
MAX_CRAWL_REDIRECTS = 5
_REDIRECT_STATUS = {301, 302, 303, 307, 308}

# Hard cap on the body bytes read from a crawled URL (the text limit applies
# after parsing; without this a huge or endless body is buffered whole).
DEFAULT_CRAWL_MAX_BYTES = 2 * 1024 * 1024

# Wall-clock budget for the whole crawl (all redirect hops + body). The
# per-read httpx timeout alone lets a slow-drip server hold a call open.
CRAWL_TOTAL_TIMEOUT_SECONDS = 20.0

# Only textual documents are parsed. A missing Content-Type is treated as HTML.
_ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/json",
    "text/xml",
    "application/xml",
}
_MARKUP_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/xml", "application/xml"}


def _crawl_max_bytes() -> int:
    try:
        value = int(os.getenv("NFL_MCP_CRAWL_MAX_BYTES", str(DEFAULT_CRAWL_MAX_BYTES)))
    except ValueError:
        return DEFAULT_CRAWL_MAX_BYTES
    return value if value > 0 else DEFAULT_CRAWL_MAX_BYTES


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _content_type_allowed(media_type: str) -> bool:
    return not media_type or media_type in _ALLOWED_CONTENT_TYPES or media_type.endswith("+xml")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


class _PinnedDNSBackend(httpcore.AsyncNetworkBackend):
    """Connects only to addresses crawl_url has already validated.

    ``pins`` maps a hostname to the IPs that passed the SSRF check; the TCP
    connection goes to one of those IPs while TLS SNI/certificate checks and
    the Host header still use the hostname (httpcore takes both from the
    request URL, not from the address we connect to). A host that was never
    validated is refused outright, so a DNS answer that changes between
    "resolve to validate" and "resolve to connect" (rebinding) cannot reach
    the private network.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend):
        self._inner = inner
        self.pins: dict[str, list[str]] = {}

    def pin(self, host: str, addresses: list[str]) -> None:
        self.pins[host.lower()] = list(addresses)

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = self.pins.get(host.lower())
        if addresses is None:
            if not (_is_ip_literal(host) or allow_private_urls()):
                raise httpcore.ConnectError(f"Refusing to connect to unvalidated host {host}")
            addresses = [host]
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last_error = e
        raise last_error or httpcore.ConnectError(f"No address to connect to for {host}")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not allowed for crawling")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _pinned_transport() -> tuple[httpx.AsyncHTTPTransport, _PinnedDNSBackend]:
    # trust_env=False: an HTTP(S)_PROXY from the environment would move the
    # connect (and the DNS lookup) to the proxy, bypassing the pin.
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    backend = _PinnedDNSBackend(transport._pool._network_backend)
    transport._pool._network_backend = backend
    return transport, backend


def _pin_key(url: str) -> str:
    """The host as httpcore will see it (IDNA-encoded, lowercase)."""
    return httpx.URL(url).raw_host.decode("ascii").lower()


class _UnsupportedEncoding(Exception):
    pass


def _decompressor(content_encoding: str | None):
    """A zlib decompressobj for the body's Content-Encoding (None = identity)."""
    encoding = (content_encoding or "").strip().lower()
    if encoding in ("", "identity"):
        return None
    if encoding in ("gzip", "x-gzip"):
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.decompressobj(zlib.MAX_WBITS | 32)  # zlib- or gzip-wrapped
    raise _UnsupportedEncoding(encoding)


async def _read_capped(response, max_bytes: int) -> tuple[bytes, bool]:
    """Read at most ``max_bytes`` of the *decoded* body; returns (body, truncated).

    Reads the raw (still-compressed) stream and inflates it here with
    ``max_length``: httpx's own decoder inflates each network chunk whole, so
    a ~200 KB gzip bomb became a 64 MB chunk before any cap applied. Both the
    compressed input and the inflated output are bounded by ``max_bytes``.
    """
    decoder = _decompressor(response.headers.get("content-encoding"))
    chunks: list[bytes] = []
    size = 0
    raw_size = 0
    async for raw in response.aiter_raw():
        raw_size += len(raw)
        data = raw
        while data:
            remaining = max_bytes - size
            if decoder is None:
                piece, data = data, b""
            else:
                if decoder.eof:
                    break
                # +1 so hitting the cap exactly is still seen as truncation.
                piece = decoder.decompress(data, remaining + 1)
                data = decoder.unconsumed_tail
                if not piece:
                    continue
            if len(piece) >= remaining:
                chunks.append(piece[:remaining])
                return b"".join(chunks), True
            chunks.append(piece)
            size += len(piece)
        if raw_size >= max_bytes:
            return b"".join(chunks), True
    return b"".join(chunks), False


def _extract_text(raw_text: str, media_type: str | None) -> tuple[str | None, str]:
    """(title, cleaned text) of a fetched document; markup is parsed, text kept."""
    title = None
    if not media_type or media_type in _MARKUP_CONTENT_TYPES or media_type.endswith("+xml"):
        # Parse HTML content
        soup = BeautifulSoup(raw_text, 'lxml')

        # Extract title
        title_tag = soup.find('title')
        title = title_tag.get_text().strip() if title_tag else None

        # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "aside", "form"]):
            script.extract()

        # Get text content
        text = soup.get_text()
    else:
        text = raw_text

    # Clean up the text
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = ' '.join(chunk for chunk in chunks if chunk)

    # Remove excessive whitespace and normalize
    text = re.sub(r'\s+', ' ', text).strip()
    return title, text


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
        - truncated_bytes: True when the body exceeded the byte cap
        - success: Whether the crawl was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    _error_data = {"url": url, "title": None, "content": "", "content_length": 0}
    # Documented range 100-50000 (default 10000); out-of-range values clamp.
    max_length = validate_limit(max_length, 100, 50000, default=10000)

    # SSRF protection: validate scheme + resolved IP before contacting the host
    # (resolved on the event loop's resolver, not a blocking getaddrinfo).
    ok, reason, _host, addresses = await resolve_safe_url(url)
    if not ok:
        return handle_validation_error(reason, _error_data)

    # Ask for an uncompressed body; _read_capped still inflates (boundedly)
    # when a server compresses anyway.
    headers = {**get_http_headers("web_crawler"), "Accept-Encoding": "identity"}
    max_bytes = _crawl_max_bytes()
    transport, backend = _pinned_transport()
    if addresses:
        backend.pin(_pin_key(url), addresses)

    # Follow redirects manually so every hop is re-validated (and pinned) —
    # otherwise a public URL could 3xx-redirect into the private network.
    # The whole crawl (every hop + the body) runs under one wall-clock budget.
    try:
        async with asyncio.timeout(CRAWL_TOTAL_TIMEOUT_SECONDS):
            async with create_http_client(follow_redirects=False, transport=transport) as client:
                current_url = url
                response = None
                try:
                    for _ in range(MAX_CRAWL_REDIRECTS + 1):
                        request = client.build_request("GET", current_url, headers=headers)
                        response = await client.send(request, stream=True)

                        if response.status_code in _REDIRECT_STATUS:
                            location = response.headers.get("location")
                            if not location:
                                break  # malformed redirect; fall through to normal handling
                            next_url = urljoin(current_url, location)
                            ok, reason, _host, addresses = await resolve_safe_url(next_url)
                            if not ok:
                                return handle_validation_error(f"Blocked redirect: {reason}", _error_data)
                            if addresses:
                                backend.pin(_pin_key(next_url), addresses)
                            await response.aclose()
                            response = None
                            current_url = next_url
                            continue

                        # Non-redirect response: process it.
                        break
                    else:
                        return handle_validation_error(
                            f"Too many redirects (>{MAX_CRAWL_REDIRECTS})", _error_data
                        )

                    response.raise_for_status()

                    media_type = _media_type(response.headers.get("content-type"))
                    if not _content_type_allowed(media_type):
                        return handle_validation_error(
                            f"Unsupported content type: {media_type} (only HTML, text, JSON and XML are crawled)",
                            _error_data,
                        )

                    try:
                        body, truncated_bytes = await _read_capped(response, max_bytes)
                    except _UnsupportedEncoding as e:
                        return handle_validation_error(
                            f"Unsupported content encoding: {e}", _error_data
                        )
                    except zlib.error as e:
                        return handle_validation_error(
                            f"Corrupt compressed body: {e}", _error_data
                        )
                finally:
                    if response is not None:
                        await response.aclose()
    except TimeoutError:
        return create_error_response(
            f"Crawl exceeded {CRAWL_TOTAL_TIMEOUT_SECONDS:.0f}s total time limit",
            ErrorType.TIMEOUT,
            _error_data,
        )

    encoding = getattr(response, "charset_encoding", None) or "utf-8"
    try:
        raw_text = body.decode(encoding, errors="replace")
    except LookupError:
        raw_text = body.decode("utf-8", errors="replace")

    # Parsing a 2 MB page is CPU-bound: keep it off the event loop.
    title, text = await asyncio.to_thread(_extract_text, raw_text, media_type)

    # Apply length limit if specified
    if max_length and len(text) > max_length:
        text = text[:max_length] + "..."

    return create_success_response({
        "url": url,
        "title": title,
        "content": text,
        "content_length": len(text),
        "truncated_bytes": truncated_bytes,
    })
