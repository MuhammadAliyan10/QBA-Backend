# =============================================================================
# QUANTA CONTROL PLANE - Production Dockerfile
# Go Multi-Stage Build for Azure Container Apps
# =============================================================================
#
# BUILD CONTEXT: Must be run from the backend root (not apps/control-plane)
# Build: docker build -f apps/control-plane/Dockerfile -t control-plane:latest .
# Run:   docker run -p 8080:8080 control-plane:latest
#
# =============================================================================

# ==================================================
# Stage 1: Builder
# ==================================================
FROM golang:1.24-alpine AS builder

# Install build dependencies
RUN apk add --no-cache git ca-certificates tzdata

# Create the EXACT same folder structure as the monorepo
# so that "replace => ../../api" works correctly
WORKDIR /build

# Copy the api folder to the correct relative location
# From apps/control-plane, ../../api points to /build/api
COPY api/ /build/api/

# Copy the control-plane app
COPY apps/control-plane/ /build/apps/control-plane/

# Change to app directory for go mod operations
WORKDIR /build/apps/control-plane

# Download dependencies
# The replace directive "../../api" now correctly resolves to /build/api
RUN go mod download && go mod verify

# Build the binary with optimizations
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -a -installsuffix cgo \
    -ldflags="-w -s" \
    -o server cmd/server/main.go

# ==================================================
# Stage 2: Production Runtime
# ==================================================
FROM alpine:3.19

WORKDIR /app

# Create non-root user for security
RUN addgroup -g 1001 -S quanta && \
    adduser -u 1001 -S quanta -G quanta

# Install runtime dependencies
RUN apk --no-cache add \
    ca-certificates \
    wget \
    curl \
    tzdata

# Copy binary from builder
COPY --from=builder /build/apps/control-plane/server .

# Set ownership
RUN chown -R quanta:quanta /app

# Switch to non-root user
USER quanta

# Environment variables
ENV GIN_MODE=release
ENV PORT_GO_API=8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://localhost:8080/health/live || exit 1

# Expose port
EXPOSE 8080

# Run server
CMD ["./server"]
