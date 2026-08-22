# =============================================================================
# QUANTA EXECUTION PLANE - Production Dockerfile
# Build context: ./backend (NOT ./backend/apps/execution-plane)
# Command: docker build -f apps/execution-plane/Dockerfile -t quanta-execution-plane:latest .
# =============================================================================

# syntax=docker/dockerfile:1.5
# Enables BuildKit cache mounts — eliminates re-downloading apt/pip on rebuild.

# ==================================================
# Stage 1: Builder — Install Python deps + browsers
# ==================================================
FROM python:3.10-slim AS builder

WORKDIR /build

# apt cache mount: packages cached between builds, never re-downloaded
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        wget \
        gnupg \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (separate layer — only rebuilt if requirements.txt changes)
COPY apps/execution-plane/requirements.txt .

# pip cache mount: wheels cached between builds
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser binary
# PLAYWRIGHT_BROWSERS_PATH is read by playwright install at runtime too
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
RUN --mount=type=cache,target=/root/.cache/ms-playwright \
    python -m playwright install chromium

# Pre-cache HuggingFace model — cached in a separate layer so code changes
# don't trigger a re-download of the 90MB model.
ENV HF_HOME=/root/.cache/huggingface
RUN --mount=type=cache,target=/root/.cache/huggingface \
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
    || echo "[WARN] HuggingFace rate-limited — model will be downloaded at first runtime."

# ==================================================
# Stage 2: Production Runtime
# ==================================================
FROM python:3.10-slim

WORKDIR /app

LABEL org.opencontainers.image.title="Quanta Execution Plane"
LABEL org.opencontainers.image.description="Python Temporal worker with Playwright for web automation"
LABEL org.opencontainers.image.vendor="Quanta"

# Runtime system deps — only what Chromium and tini actually need at runtime
# (no build tools, no git, no compilers)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        # Process management — reaps orphaned Chromium children
        tini \
        # TLS
        ca-certificates \
        curl \
        # Chromium runtime shared libraries
        libglib2.0-0 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libdbus-1-3 \
        libxcb1 \
        libxkbcommon0 \
        libx11-6 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        # Font rendering
        fonts-liberation \
        libappindicator3-1 \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /root/.local /root/.local

# Copy Playwright Chromium binary from builder
COPY --from=builder /root/.cache/ms-playwright /root/.cache/ms-playwright

# Copy pre-baked HuggingFace model from builder
COPY --from=builder /root/.cache/huggingface /root/.cache/huggingface

# ==================================================
# Application Code
# ==================================================
# Prisma schema (needed by some activity imports)
RUN mkdir -p /app/prisma
COPY apps/execution-plane/prisma/schema.prisma ./prisma/schema.prisma

# Source code
COPY apps/execution-plane/src/ ./src/

# Shared API definitions (gRPC stubs etc.)
COPY api/ ./api/

# Helper scripts (optional — used for debugging inside container)
COPY scripts/ ./scripts/

# ==================================================
# Environment
# ==================================================
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONPATH=/app:/app/src
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONFAULTHANDLER=1
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
ENV HF_HOME=/root/.cache/huggingface

# Runtime defaults (all overridable via docker-compose / env injection)
ENV TEMPORAL_HOST=localhost:7233
ENV NATS_URL=nats://localhost:4222
ENV REDIS_URL=redis://localhost:6379
ENV ENABLE_S3_UPLOAD=false

# ==================================================
# Health + Entrypoint
# ==================================================
HEALTHCHECK --interval=30s --timeout=30s --start-period=45s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)" || exit 1

# tini as PID 1: properly reaps zombie processes from Playwright child browsers
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "src/worker.py"]
