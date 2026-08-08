-- Migration 007: Performance Indexes
-- Purpose: Add composite indexes for common query patterns identified in production
-- Author: P3 Code Quality Pass
-- Date: 2026-08-08

-- ─── JOBS ─────────────────────────────────────────────────────────────────────
-- The HandleListJobs query pattern:
--   WHERE user_id = ? ORDER BY created_at DESC LIMIT 50
-- Currently uses idx_jobs_user_id for the WHERE and idx_jobs_created for the sort —
-- two separate index scans. A composite index eliminates the sort step entirely
-- because Postgres can satisfy ORDER BY using the index's natural order.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_user_created
    ON jobs (user_id, created_at DESC);

-- Status filter composite: HandleListJobs with ?status= query param
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_user_status_created
    ON jobs (user_id, status, created_at DESC);

-- ─── API KEYS ─────────────────────────────────────────────────────────────────
-- Auth middleware executes: WHERE key_hash = ? AND is_active = ?
-- This is the hot path — every authenticated API request hits it.
-- A partial index (WHERE is_active = true) halves the index size since revoked
-- keys are never used for auth.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_api_keys_hash_active
    ON api_keys (key_hash)
    WHERE is_active = true;

-- HandleList controller: WHERE user_id = ? ORDER BY created_at DESC
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_api_keys_user_created
    ON api_keys (user_id, created_at DESC);

-- ─── LEDGER TRANSACTIONS ─────────────────────────────────────────────────────
-- HandleGetTransactions: WHERE user_id = ? ORDER BY created_at DESC LIMIT N
-- 003_ledger.sql created separate idx_ledger_user_created and idx_ledger_created.
-- Verify the composite exists; IF NOT EXISTS guards against duplicate errors.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ledger_user_type_created
    ON ledger_transactions (user_id, transaction_type, created_at DESC);

-- ─── JOB LOGS ─────────────────────────────────────────────────────────────────
-- HandleGetJobLogs: WHERE job_id = ? ORDER BY timestamp ASC LIMIT 100
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_job_logs_job_ts
    ON job_logs (job_id, timestamp ASC);
