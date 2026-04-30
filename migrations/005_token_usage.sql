-- migrations/005_token_usage.sql
-- Migration 005: Add Token Telemetry Columns to Jobs + Create UserUsage Table
-- Purpose: Track per-job and per-user LLM token consumption for billing
-- Author: Lead QA & Systems Integration
-- Date: 2026-04-30

-- =============================================================================
-- 1. EXTEND JOBS TABLE WITH TOKEN COLUMNS
-- =============================================================================

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS prompt_tokens    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS completion_tokens INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_tokens     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS model_used       VARCHAR(255) DEFAULT '',
    ADD COLUMN IF NOT EXISTS llm_calls        INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN jobs.prompt_tokens IS 'Cumulative prompt tokens consumed across all epochs';
COMMENT ON COLUMN jobs.completion_tokens IS 'Cumulative completion tokens consumed across all epochs';
COMMENT ON COLUMN jobs.total_tokens IS 'prompt_tokens + completion_tokens';
COMMENT ON COLUMN jobs.model_used IS 'Primary LLM model used for planning';
COMMENT ON COLUMN jobs.llm_calls IS 'Total number of LLM API calls made';

-- =============================================================================
-- 2. USER-LEVEL AGGREGATED USAGE TABLE
-- =============================================================================

CREATE TABLE IF NOT EXISTS user_usage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         VARCHAR(255) NOT NULL,
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    prompt_tokens   BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens    BIGINT NOT NULL DEFAULT 0,
    llm_calls       INTEGER NOT NULL DEFAULT 0,
    jobs_run        INTEGER NOT NULL DEFAULT 0,
    stripe_meter_event_id VARCHAR(255),
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, period_start)
);

CREATE INDEX IF NOT EXISTS idx_user_usage_user_period ON user_usage(user_id, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_user_usage_period ON user_usage(period_start);

COMMENT ON TABLE user_usage IS 'Rolling usage aggregation per user per billing period for Stripe metered billing';
COMMENT ON COLUMN user_usage.stripe_meter_event_id IS 'Last Stripe MeterEvent ID for idempotency';
