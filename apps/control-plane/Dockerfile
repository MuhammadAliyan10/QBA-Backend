# =============================================================================
# QUANTA CONTROL PLANE - Production Dockerfile
# Go Multi-Stage Build for Azure Container Apps
# =============================================================================
#
# Build: docker build -t control-plane:latest .
# Run:   docker run -p 8080:8080 control-plane:latest
#
# =============================================================================

# ==================================================
# Stage 1: Builder
# ==================================================
FROM golang:1.21-alpine AS builder

WORKDIR /build

# Install build dependencies
RUN apk add --no-cache git ca-certificates tzdata

# Copy go mod files first (for layer caching)
COPY go.mod go.sum ./

# Download dependencies
RUN go mod download && go mod verify

# Copy source code
COPY . .

# Note: Proto gen files should be in the build context
# The CI/CD workflow sets the correct context that includes api/gen/go/

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
COPY --from=builder /build/server .

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
