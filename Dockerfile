# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Set work directory
WORKDIR /app

# Install system dependencies. `apt-get upgrade` pulls the latest Debian
# security patches (glibc/ncurses/sqlite/zlib/... CVEs). `curl` is intentionally
# NOT installed — the health check uses Python's stdlib instead, so the image
# carries no curl/libcurl. gcc is only needed to build C-extension wheels.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the pinned lockfile and install exact, reproducible dependencies.
# requirements.lock is generated from requirements.txt via:
#   uv pip compile requirements.txt --python-version 3.11 --output-file requirements.lock
COPY requirements.lock .
# Upgrade the bundled build tooling too (wheel/setuptools) — the base image ships
# older versions flagged by image scanners (e.g. wheel GHSA-8rrh-rw8j-w5fx).
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.lock

# Copy project files
COPY . .

# Install the package in development mode
RUN pip install -e .

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# Expose the port
EXPOSE 9000

# Health check (Python stdlib — avoids depending on curl in the image)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:9000/health', timeout=5).status == 200 else 1)" || exit 1

# Run the server
CMD ["python", "-m", "nfl_mcp.server"]