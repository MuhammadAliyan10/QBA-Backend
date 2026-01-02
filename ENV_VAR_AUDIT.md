# 🛡️ Environment Variable Audit Report

**Date**: January 1, 2026
**Target**: `backend/.env` vs Codebase Usage
**Status**: ⚠️ **MISMATCHES FOUND**

---

## 🚨 Critical Mismatches

### 1. Storage Configuration (R2 vs S3)

The codebase expects standard AWS/S3 variable names, but `.env` uses Cloudflare R2 specific names. This will cause storage connections to fail or fall back to local MinIO defaults.

| Codebase Expects        | `.env` Has             | Status          |
| ----------------------- | ---------------------- | --------------- |
| `S3_BUCKET`             | `R2_BUCKET_NAME`       | ❌ **MISMATCH** |
| `S3_ENDPOINT_URL`       | `R2_ENDPOINT_URL`      | ❌ **MISMATCH** |
| `AWS_ACCESS_KEY_ID`     | `R2_ACCESS_KEY_ID`     | ❌ **MISMATCH** |
| `AWS_SECRET_ACCESS_KEY` | `R2_SECRET_ACCESS_KEY` | ❌ **MISMATCH** |

**Recommendation**: Rename `R2_` variables to `S3_` and `AWS_` prefixes in `.env` to match the code (or update code to read `R2_` vars).

### 2. Production Mode Warning

The `.env` file is set to `ENVIRONMENT=production`.

> [!WARNING] > **"not production in the main .env file"**
> The user indicated this file should not be production. Using `production` locally hides debug logs and may connect to live services.
> **Recommendation**: Change to `ENVIRONMENT=development`.

---

## ❌ Missing Variables

The following variables are used in the codebase (Python/Go) but are **missing** from `backend/.env`.

### Feature Flags

- `ENABLE_BILLING` (Used in `config.py`, `config.go`)
- `ENABLE_NOTIFICATIONS` (Used in `dispatcher.go`)
- `ENABLE_TRACING` (Used in `telemetry.go`)
- `ENABLE_METRICS` (Used in `telemetry.go`)

### Centralized Timeouts (New)

- `TIMEOUT_ACTIVITY_SEC`
- `TIMEOUT_HUMAN_WAIT_SEC`
- `TIMEOUT_LLM_SEC`
- `TIMEOUT_VECTOR_DB_SEC`
- `TIMEOUT_WORKFLOW_START_SEC`
- `TIMEOUT_SIGNAL_WORKFLOW_SEC`
- `TIMEOUT_HTTP_REQUEST_SEC`
- `TIMEOUT_WS_PING_SEC`
- `TIMEOUT_GRACEFUL_SHUTDOWN_SEC`
- `TIMEOUT_CLICK_MS`
- `TIMEOUT_NAVIGATION_MS`

### Retry Logic

- `MAX_RETRY_ATTEMPTS`
- `INITIAL_RETRY_INTERVAL_SEC`
- `RETRY_BACKOFF_COEFFICIENT`

### Rate Limiting & Billing

- `RATE_LIMIT_REQUESTS`
- `RATE_LIMIT_WINDOW`
- `CREDIT_PER_JOB`
- `DEFAULT_CREDITS`

### Notifications (Optional)

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`
- `SENDGRID_API_KEY`
- `SLACK_WEBHOOK_URL`

---

## ⚠️ Duplicates & Anomalies

- **Duplicate Key**: `POLAR_ACCESS_TOKEN` appears twice in `.env`.
  ```env
  POLAR_ACCESS_TOKEN="polar_at_..."
  POLAR_ACCESS_TOKEN=XXX  <-- Duplicate
  ```
- **Unused/Legacy**: `GOOGLE_API_KEY` is in `.env` but `GEMINI_API_KEY` might be preferred (check usage).

---

## ✅ Present & Correct

The following core variables are correctly present:

- `NATS_URL`
- `REDIS_URL`
- `TEMPORAL_HOST`
- `DATABASE_URL`
- `OPENAI_API_KEY`
- `CLERK_PUBLISHABLE_KEY`
- `CLERK_SECRET_KEY`
- `FERNET_KEY`
