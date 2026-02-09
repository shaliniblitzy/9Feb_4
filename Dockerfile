# syntax=docker/dockerfile:1

# =============================================================================
# Dockerfile — Reverse Document Generator Flask Web Server
# =============================================================================
# Production container build specification for the AI-powered Reverse Document
# Generator, deployed as a Flask WSGI web server on Google Cloud Run Service.
#
# Runtime stack:
#   - Ubuntu 24.04 LTS (Noble Numbat) base image
#   - Python 3.12.3 application runtime
#   - Node.js 20.20.0 LTS (MCP bridge for Figma and Chrome DevTools integration)
#   - Google Chrome (headless, for DevTools MCP server)
#   - Gunicorn 23.0.0 WSGI HTTP server
#
# Architecture note:
#   This Dockerfile transforms the original batch job container (CMD python main.py)
#   into a persistent web server container (CMD gunicorn) for Cloud Run Service
#   deployment. Docker-in-Docker components (iptables, supervisor, fuse-overlayfs,
#   dockerd) are intentionally removed as the Flask server does not require an
#   embedded Docker Engine.
#
# Build:
#   DOCKER_BUILDKIT=1 docker build \
#     --secret id=gcp_credentials,src=credentials.json \
#     -t reverse-document-generator .
#
# Run:
#   docker run -p 8080:8080 --env-file .env reverse-document-generator
# =============================================================================

# ---------------------------------------------------------------------------
# Base image: Ubuntu 24.04 LTS (Noble Numbat)
# ---------------------------------------------------------------------------
FROM ubuntu:24.04

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# System-level dependencies and Python 3.12
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Python 3.12 runtime and development headers
        python3.12 \
        python3.12-venv \
        python3.12-dev \
        python3-pip \
        # Build tools required for compiling native Python extensions
        build-essential \
        gcc \
        g++ \
        # Networking and download utilities
        curl \
        wget \
        ca-certificates \
        gnupg \
        # Git for repository download operations (used by blitzy_utils.scm)
        git \
        # Required for Google Cloud SDK integration and misc operations
        apt-transport-https \
        lsb-release \
        # Shared libraries required by Google Chrome and native dependencies
        libglib2.0-0 \
        libnss3 \
        libnspr4 \
        libdbus-1-3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2t64 \
        libxshmfence1 \
        libx11-xcb1 \
        libxcb1 \
        libxext6 \
        libxfixes3 \
        libx11-6 \
        fonts-liberation \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Ensure python3 and pip commands point to Python 3.12
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.12 1

# ---------------------------------------------------------------------------
# Node.js 20 LTS (required for MCP bridge subprocess)
# The MCP Manager spawns Node.js child processes for Figma API and Chrome
# DevTools integration. Node.js 20 LTS is specified per Tech Spec Section 3.1.
# ---------------------------------------------------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && npm --version

# ---------------------------------------------------------------------------
# Google Chrome stable installation (required for Chrome DevTools MCP server)
# Chrome is used by the MCP bridge for Figma design asset extraction and
# DevTools-based page interactions. Runs in headless/sandbox-disabled mode
# inside the container.
# ---------------------------------------------------------------------------
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

# Flask application target for Gunicorn WSGI server
ENV FLASK_APP=app.main:application

# Suppress D-Bus session bus warnings in containerized environment
ENV DBUS_SESSION_BUS_ADDRESS=/dev/null

# Disable Chrome sandbox for containerized execution (required when running
# as root or in environments without a proper user namespace)
ENV CHROME_DEVEL_SANDBOX=0
ENV CHROME_NO_SANDBOX=1

# Chrome binary path for MCP bridge and Puppeteer-like integrations
ENV CHROME_PATH=/usr/bin/google-chrome-stable

# Python runtime settings: unbuffered stdout/stderr for real-time log output,
# skip .pyc bytecode generation to reduce container filesystem writes
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Disable tokenizer parallelism warnings from HuggingFace tokenizers
# (transitive dependency via some LLM SDKs in blitzy-platform-shared)
ENV TOKENIZERS_PARALLELISM=false

# Default port for Cloud Run Service (overridable via PORT environment variable)
ENV PORT=8080

# ---------------------------------------------------------------------------
# Working directory
# ---------------------------------------------------------------------------
WORKDIR /app

# ---------------------------------------------------------------------------
# Python package manager: Artifact Registry authentication
# Install keyring and keyrings.google-artifactregistry-auth so that pip can
# authenticate against the private Google Artifact Registry hosting the
# blitzy-platform-shared==0.0.542 package.
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir --break-system-packages \
        keyring \
        keyrings.google-artifactregistry-auth

# ---------------------------------------------------------------------------
# Install Python dependencies
# Uses BuildKit secret mount to inject Google Cloud credentials for
# authenticating against the private Artifact Registry.
#
# requirements.txt is copied first (before application code) to leverage
# Docker layer caching — dependencies are only reinstalled when the
# requirements file changes, not on every code change.
# ---------------------------------------------------------------------------
COPY requirements.txt .

RUN --mount=type=secret,id=gcp_credentials,target=/root/.config/gcloud/application_default_credentials.json \
    pip install --no-cache-dir --break-system-packages -r requirements.txt

# ---------------------------------------------------------------------------
# Install Node.js dependencies for MCP bridge (conditional)
# The MCP bridge subprocess may require npm packages for Figma and Chrome
# DevTools tool servers. Only runs npm ci if a package.json is present.
# ---------------------------------------------------------------------------
COPY package*.json* ./
RUN if [ -f package.json ]; then npm ci --omit=dev && npm cache clean --force; fi

# ---------------------------------------------------------------------------
# Copy application source code
# Placed after dependency installation to maximize Docker layer cache hits
# during development iterations where only application code changes.
# ---------------------------------------------------------------------------
COPY . .

# ---------------------------------------------------------------------------
# Expose the HTTP port for Cloud Run Service
# Cloud Run injects the PORT environment variable (default 8080) and routes
# incoming HTTPS traffic to this container port via its internal proxy.
# Gunicorn binds to 0.0.0.0:$PORT as configured in gunicorn.conf.py.
# ---------------------------------------------------------------------------
EXPOSE 8080

# ---------------------------------------------------------------------------
# Health check for container orchestration
# Probes the Flask /health endpoint to verify the application is responsive.
# Cloud Run manages its own health checking, but this HEALTHCHECK provides
# an additional signal for local Docker and docker-compose deployments.
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# ---------------------------------------------------------------------------
# Container entrypoint: Gunicorn WSGI HTTP server
#
# CRITICAL CHANGE from original architecture:
#   Original batch job:  CMD ["/app/start.sh"]  (which ran `python main.py`)
#   Flask web server:    CMD ["gunicorn", ...]   (persistent WSGI server)
#
# Gunicorn configuration is loaded from gunicorn.conf.py which defines:
#   - Worker count and thread pool (optimized for memory-intensive LLM ops)
#   - Request timeout of 3600s (1 hour) for long-running document generation
#   - Bind address 0.0.0.0:$PORT for Cloud Run compatibility
#   - Structured logging via structlog
#   - Worker recycling (max_requests) for memory management
#   - preload_app=True for shared memory efficiency
#   - worker_tmp_dir=/dev/shm for Cloud Run tmpfs compatibility
#
# The WSGI application callable is located at app.main:application
# ---------------------------------------------------------------------------
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app.main:application"]
