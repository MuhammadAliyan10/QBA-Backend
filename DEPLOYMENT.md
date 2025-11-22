# E2E Backend - Production Deployment Guide

## Overview

This guide covers deploying the e2e-Backend platform to production with Level 5 enterprise features including observability, security scanning, and graceful shutdown.

---

## Infrastructure Requirements

### Minimum Requirements
- **CPU**: 4 cores
- **RAM**: 8GB
- **Storage**: 50GB SSD
- **Network**: 100 Mbps

### Services Required
- **NATS JetStream** (Message Bus)
- **Temporal** (Workflow Orchestration)
- **PostgreSQL** (Temporal Database)
- **CockroachDB** (Transactional Data)
- **Redis** (Caching)
- **OpenTelemetry Collector** (Optional, for observability)

---

## Quick Start (Development)

```bash
# 1. Clone and navigate to project
cd e2e-Backend

# 2. Install dependencies
make install-deps

# 3. Start infrastructure
make up

# 4. Run control plane (Terminal 1 )
make run-go

# 5. Run execution plane (Terminal 2)
make run-python
```

---

## Production Deployment

### Step 1: Environment Configuration

Create `.env` files from templates:

```bash
# Control Plane
cp apps/control-plane/env.example apps/control-plane/.env
# Edit and set:
# - PORT, NATS_URL, TEMPORAL_HOST
# - OTEL_EXPORTER_OTLP_ENDPOINT (if using)
# - JWT_SECRET, API_KEY

# Execution Plane
cp apps/execution-plane/env.example apps/execution-plane/.env
# Edit and set:
# - OPENAI_API_KEY (required)
# - AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# - TEMPORAL_HOST, NATS_URL
# - Database credentials
```

### Step 2: Security Audit

**CRITICAL**: Run security scan before deployment:

```bash
make audit
```

Fix any HIGH severity findings before proceeding.

### Step 3: Run Tests

```bash
make test
```

Ensure all tests pass.

### Step 4: Build Production Docker Images

```bash
make docker-build
```

Expected image sizes:
- Control Plane: < 50MB
- Execution Plane: < 2GB

### Step 5: Deploy with Docker Compose

Update `docker-compose.yml` to use production images:

```yaml
services:
  control-plane:
    image: e2e-control-plane:prod
    environment:
      # Load from .env file
    depends_on:
      - nats
      - temporal
    healthcheck:
      test: ["CMD", "wget", "--quiet", "--tries=1", "--spider", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  execution-plane:
    image: e2e-execution-plane:prod
    environment:
      # Load from .env file
    depends_on:
      - temporal
      - nats
      - redis
```

Deploy:

```bash
docker-compose up -d
docker-compose ps  # Verify all services are healthy
```

---

## Observability Setup

### OpenTelemetry Collector (Optional)

Add to `docker-compose.yml`:

```yaml
  otel-collector:
    image: otel/opentelemetry-collector:latest
    command: ["--config=/etc/otel-collector-config.yaml"]
    volumes:
      - ./config/otel-collector-config.yaml:/etc/otel-collector-config.yaml
    ports:
      - "4318:4318"  # OTLP HTTP
      - "4317:4317"  # OTLP gRPC
```

### Prometheus Metrics

Access metrics at:
- Control Plane: `http://localhost:8080/metrics`
- Configure Prometheus to scrape these endpoints

### Distributed Tracing

Set in `.env`:
```
ENABLE_TRACING=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

View traces in Jaeger/Zipkin connected to OTLP collector.

---

## Security

### Pre-Deployment Checklist

- [ ] Run `make audit` and fix HIGH severity issues
- [ ] Rotate all API keys and secrets
- [ ] Enable HTTPS/TLS for production
- [ ] Set strong JWT_SECRET
- [ ] Limit CORS_ALLOWED_ORIGINS to production domains
- [ ] Review database credentials
- [ ] Enable firewall rules

### Continuous Security

Add to CI/CD pipeline:

```yaml
- name: Security Scan
  run: make audit
```

---

## Graceful Deployment (Zero Downtime)

### Rolling Update Strategy

1. Deploy new version:
   ```bash
   docker-compose up -d --no-deps --build execution-plane
   ```

2. Worker receives SIGTERM and finishes current task

3 Old container stops gracefully

4. New container starts

### Blue-Green Deployment

1. Deploy to "green" environment
2. Run smoke tests
3. Switch traffic from "blue" to "green"
4. Keep "blue" running for quick rollback

---

## Monitoring

### Health Checks

```bash
# Control Plane
curl http://localhost:8080/health
curl http://localhost:8080/ready

# Check logs
docker-compose logs -f control-plane
docker-compose logs -f execution-plane
```

### Key Metrics to Monitor

- **Workflow Success Rate**: Temporal dashboard
- **Response Time**: OpenTelemetry traces
- **Error Rate**: Application logs
- **Resource Usage**: Docker stats
- **Message Queue Depth**: NATS monitoring

### Alerts to Configure

- High error rate (> 5%)
- Slow workflow execution (> 60s)
- Database connection failures
- Memory usage > 80%

---

## Troubleshooting

### Worker Not Picking Up Jobs

```bash
# Check Temporal connection
docker-compose logs execution-plane | grep "Connected to Temporal"

# Check task queue
# Visit Temporal UI: http://localhost:8088
```

### NATS Connection Issues

```bash
docker-compose logs nats
docker-compose logs control-plane | grep NATS
```

### OpenAI API Errors

```bash
# Check API key
docker-compose exec execution-plane env | grep OPENAI_API_KEY

# Check rate limits
docker-compose logs execution-plane | grep "Rate limit"
```

### Graceful Shutdown Not Working

```bash
# Test shutdown
docker-compose kill -s SIGTERM execution-plane
# Should see: "🛑 Shutdown signal received. Finishing current tasks..."
```

---

## Backup & Disaster Recovery

### Critical Data to Backup

1. **Temporal Database** (PostgreSQL)
   ```bash
   docker-compose exec temporal-db pg_dump -U temporal > backup.sql
   ```

2. **CockroachDB Data**
   ```bash
   docker-compose exec cockroach cockroach dump --insecure
   ```

3. **NATS JetStream State**
   ```bash
   docker cp e2e-backend_nats_1:/data ./nats-backup
   ```

### Recovery Procedure

1. Stop all services
2. Restore databases from backup
3. Restart infrastructure
4. Verify health checks
5. Resume workers

---

## Performance Tuning

### Scaling Workers

Increase workers for higher throughput:

```yaml
execution-plane:
  deploy:
    replicas: 3  # Run 3 worker instances
```

### Temporal Tuning

Increase concurrent workflow executions in worker:

```python
worker = Worker(
    client,
    task_queue="e2e-browser-tasks",
    max_concurrent_workflow_tasks=100,
    max_concurrent_activities=50
)
```

### Resource Limits

Set in `docker-compose.yml`:

```yaml
execution-plane:
  deploy:
    resources:
      limits:
        cpus: '2'
        memory: 4G
      reservations:
        cpus: '1'
        memory: 2G
```

---

## Support

For issues:
1. Check logs: `docker-compose logs`
2. Run security audit: `make audit`
3. Verify tests pass: `make test`
4. Review this deployment guide

**Production-Ready**: This setup supports 10,000+ concurrent automation tasks with proper scaling.
