# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Multi-stage build.
#
# The build stage carries the C toolchain (gcc + its hard dependency binutils)
# needed to compile any dependency that ships only an sdist. binutils alone
# accounts for the large majority of the image scanner's findings (it parses
# many binary formats -> broad CVE surface, and ships as ~8 sub-packages so each
# CVE is reported 8x). By building into an isolated virtualenv and copying ONLY
# that venv into a clean runtime stage, the final image carries no gcc/binutils
# at all -- removing that entire class of findings and shrinking the image.
# ---------------------------------------------------------------------------

# ---- Build stage -----------------------------------------------------------
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# gcc/binutils live ONLY in this stage and never reach the runtime image.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Build into a self-contained virtualenv we can copy wholesale to the runtime
# stage. The venv's interpreter symlinks resolve identically there because the
# runtime stage uses the same python:3.13-slim base.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade the bundled build tooling (wheel/setuptools) too -- the base image
# ships older versions flagged by scanners (e.g. wheel GHSA-8rrh-rw8j-w5fx).
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Install pinned, reproducible dependencies. requirements.lock is generated via:
#   uv pip compile requirements.txt --python-version 3.13 --output-file requirements.lock
# (FastMCP 4 is final now, so no --prerelease flag is needed.)
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# Install the package itself into the venv. --no-deps: the lock above is the
# complete dependency set; letting pip resolve pyproject's ranges here would
# silently upgrade past the pins. `pip check` fails the build if the lock no
# longer satisfies pyproject.
COPY . .
RUN pip install --no-cache-dir --no-deps -e . && pip check

# ---- Runtime stage ---------------------------------------------------------
FROM python:3.14-slim AS runtime

# NFL_MCP_HOST: the server binds loopback by default; inside the container it
# must listen on all interfaces so the published port reaches it. Publish it
# on the host's loopback only (docker run -p 127.0.0.1:9000:9000 ...).
# NFL_MCP_DB_PATH: keep the SQLite cache on the /data volume so it survives
# container re-creation (docker run -v nfl-mcp-data:/data ...).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    NFL_MCP_HOST=0.0.0.0 \
    NFL_MCP_DB_PATH=/data/nfl_data.db

WORKDIR /app

# Pull the latest Debian security patches, but do NOT install gcc/binutils here
# -- the runtime needs no C toolchain (all deps arrive as prebuilt wheels via
# the copied venv).
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtualenv (all deps + the installed package) and the app
# source (the editable install and PYTHONPATH resolve nfl_mcp from /app).
COPY --from=builder /opt/venv /opt/venv
COPY . .

# Smoke test: fail the build if the copied venv can't import the full app graph
# in this clean, toolchain-free runtime. `import nfl_mcp.server` transitively
# imports the tool registry -> every tool module -> every (compiled) dependency
# (lxml, pydantic-core, ...), so a broken venv copy or a missing wheel is caught
# here rather than at container start. Side-effect-free (no server bind, no DB).
RUN python -c "import nfl_mcp.server"

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /data \
    && chown -R app:app /app /data
USER app

# Persistent cache (a named volume inherits the app ownership set above).
VOLUME /data

# Expose the port
EXPOSE 9000

# Health check (Python stdlib -- avoids depending on curl in the image). The
# server answers right after the imports (startup prefetch runs in the
# background); the start period covers a cold interpreter + first DB open.
# /health returns 503 only when the database is down ("degraded", an open
# upstream circuit breaker, is still 200 -- a restart would not fix it).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:9000/health', timeout=5).status == 200 else 1)" || exit 1

# Run the server
CMD ["python", "-m", "nfl_mcp.server"]
