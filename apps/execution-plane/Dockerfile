# ==================================================
# Stage 1: Builder - Install Dependencies
# ==================================================
FROM python:3.11-slim as builder

WORKDIR /build

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements from execution-plane directory
COPY apps/execution-plane/requirements.txt .

# Install Python dependencies to user site-packages
RUN pip install --user --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN python -m playwright install --with-deps chromium

# ==================================================
# Stage 2: Production Runtime
# ==================================================
FROM python:3.11-slim

WORKDIR /app

# Install minimal runtime dependencies for Playwright + tini for proper process management
RUN apt-get update && apt-get install -y \
    ca-certificates \
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
    tini \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder
COPY --from=builder /root/.local /root/.local
COPY --from=builder /root/.cache/ms-playwright /root/.cache/ms-playwright

# Copy application code from execution-plane directory
COPY apps/execution-plane/src/ ./src/

# Copy proto gen files
COPY api/gen/python/ ./api/gen/python/

# Set environment variables
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check (verify Python can import main modules)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import sys; sys.path.insert(0, 'src'); from workflows import BrowserWorkflow; sys.exit(0)" || exit 1

# CRITICAL: Use tini as init process to reap zombie processes
# This prevents PID exhaustion from orphaned Playwright browser children
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "src/worker.py"]
