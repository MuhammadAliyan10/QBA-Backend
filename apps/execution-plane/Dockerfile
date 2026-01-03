# =============================================================================
# QUANTA EXECUTION PLANE - Production Dockerfile
# Python Worker with Playwright for Azure Container Apps
# =============================================================================
#
# CRITICAL BUILD NOTES:
# - Build context must be ./backend (not ./backend/apps/execution-plane)
# - This allows copying schema.prisma from frontend directory
# - Example: docker build -f apps/execution-plane/Dockerfile -t execution-plane:latest .
#
# =============================================================================

# ==================================================
# Stage 1: Builder - Install Dependencies
# ==================================================
FROM python:3.10-slim AS builder

WORKDIR /build

# Install system dependencies for building Python packages
RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    gnupg \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements from execution-plane directory
# Build context is ./backend, so path is relative to that
COPY apps/execution-plane/requirements.txt .

# Install Python dependencies to user site-packages
RUN pip install --user --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium only, skip deps - they're already in runtime stage)
RUN python -m playwright install chromium

# ==================================================
# Stage 2: Production Runtime
# ==================================================
FROM python:3.10-slim

WORKDIR /app

# Labels for traceability
LABEL org.opencontainers.image.title="Quanta Execution Plane"
LABEL org.opencontainers.image.description="Python worker for browser automation with Playwright"
LABEL org.opencontainers.image.vendor="Quanta"

# Install runtime dependencies for Playwright + tini for process management
# tini is CRITICAL to prevent zombie processes from orphaned Playwright children
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    # Playwright browser dependencies
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
    fonts-liberation \
    libappindicator3-1 \
    xdg-utils \
    # CRITICAL: tini for zombie process reaping
    tini \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder
COPY --from=builder /root/.local /root/.local

# Copy Playwright browsers from builder
COPY --from=builder /root/.cache/ms-playwright /root/.cache/ms-playwright

# ==================================================
# CRITICAL: Copy Prisma Schema and Generate Client
# ==================================================
# Build context is ./backend, so we can access frontend/prisma/schema.prisma
# via a relative path from the build context (need to copy from parent during CI)

# Create prisma directory
RUN mkdir -p /app/prisma

# Copy schema from frontend (assumes build context is ./backend and frontend is sibling)
# In CI/CD, the schema should be in: ../../frontend/prisma/schema.prisma
# We handle this by copying during build with proper context
COPY apps/execution-plane/prisma/schema.prisma ./prisma/schema.prisma

# Generate Prisma client
RUN /root/.local/bin/prisma generate --schema=prisma/schema.prisma

# ==================================================
# Copy Application Code
# ==================================================

# Copy application source code
COPY apps/execution-plane/src/ ./src/

# Create directories for optional files
RUN mkdir -p ./api/gen/python ./scripts

# Copy config.py if it exists (use a RUN command with shell for optional copy)
# Note: If config.py doesn't exist in the build context, this will be skipped
RUN if [ -f ./apps/execution-plane/config.py ]; then cp ./apps/execution-plane/config.py ./config.py; fi || true

# Copy proto gen files (optional - may not exist in all builds)
COPY api/gen/python/ ./api/gen/python/

# Copy sync script for debugging (optional)
COPY scripts/ ./scripts/

# ==================================================
# Environment Configuration
# ==================================================

# Add local bin to PATH
ENV PATH=/root/.local/bin:$PATH

# Python optimizations
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONFAULTHANDLER=1

# Playwright configuration
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

# Application defaults (override in deployment)
ENV TEMPORAL_HOST=localhost:7233
ENV NATS_URL=nats://localhost:4222
ENV REDIS_URL=redis://localhost:6379
ENV ENABLE_S3_UPLOAD=false

# ==================================================
# Health Check
# ==================================================

# Verify Python can import main modules
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python3 -c "import sys; sys.path.insert(0, 'src'); from workflows import BrowserWorkflow; print('OK')" || exit 1

# ==================================================
# Entrypoint - Use tini as PID 1
# ==================================================

# CRITICAL: Use tini as init process
# This properly reaps zombie processes from Playwright browser children
# Without this, orphaned Chrome processes will cause PID exhaustion
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run the Temporal worker
CMD ["python3", "src/worker.py"]
