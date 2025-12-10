-- Migration 001: Create Jobs Table
-- Purpose: Track job metadata and webhook URLs for the control plane
-- Author: Production Readiness Audit
-- Date: 2025-12-10

CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(255) PRIMARY KEY,
    workflow_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255),
    status VARCHAR(50) NOT NULL DEFAULT 'QUEUED',
    webhook_url VARCHAR(2048),
    params JSONB DEFAULT '{}',
    config JSONB DEFAULT '{}',
    result JSONB,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_workflow ON jobs(workflow_id);

-- Comment
COMMENT ON TABLE jobs IS 'Tracks browser automation job metadata, status, and webhook URLs';
COMMENT ON COLUMN jobs.webhook_url IS 'URL to notify on job completion/failure';
COMMENT ON COLUMN jobs.params IS 'User-provided parameters for the workflow';
COMMENT ON COLUMN jobs.config IS 'Execution config (proxy, captcha settings, etc)';
