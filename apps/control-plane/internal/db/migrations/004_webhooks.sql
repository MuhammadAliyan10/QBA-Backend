-- Migration: Add Webhook Support to Jobs
-- Purpose: Enable async notifications when workflows complete

-- Add webhook_url column to jobs table
ALTER TABLE jobs ADD COLUMN webhook_url TEXT;

-- Index for fast webhook lookups (partial index for non-null values)
CREATE INDEX IF NOT EXISTS idx_jobs_webhook
    ON jobs(webhook_url)
    WHERE webhook_url IS NOT NULL;

COMMENT ON COLUMN jobs.webhook_url IS 'User-provided URL for async webhook notifications on job completion';
