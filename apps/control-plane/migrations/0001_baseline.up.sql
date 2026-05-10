-- 0001_baseline.up.sql
-- Baseline snapshot of the existing production schema.
-- This migration is a no-op when run against an already-initialized database
-- because all CREATE statements use IF NOT EXISTS.
-- It exists solely to establish migration version tracking.

-- =============================================================================
-- ENUMS
-- =============================================================================
DO $$ BEGIN
    CREATE TYPE user_tier AS ENUM ('FREE', 'PRO', 'ENTERPRISE');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM ('QUEUED', 'RUNNING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE log_level AS ENUM ('DEBUG', 'INFO', 'WARN', 'ERROR');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE trigger_type AS ENUM ('ON_DEMAND', 'SCHEDULED', 'WEBHOOK');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE asset_type AS ENUM ('OUTPUT', 'INPUT', 'CHECKPOINT');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE transaction_type AS ENUM ('CREDIT', 'DEBIT', 'REFUND', 'ADJUSTMENT');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- =============================================================================
-- TABLES
-- =============================================================================

CREATE TABLE IF NOT EXISTS user_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clerk_user_id   TEXT NOT NULL,
    email           TEXT NOT NULL,
    first_name      TEXT,
    last_name       TEXT,
    avatar_url      TEXT,
    tier            user_tier NOT NULL DEFAULT 'FREE',
    webhook_url     TEXT,
    webhook_secret  TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS user_profiles_clerk_user_id_key ON user_profiles(clerk_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS user_profiles_email_key ON user_profiles(email);

CREATE TABLE IF NOT EXISTS user_usage (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES user_profiles(id),
    credits_balance     INTEGER NOT NULL DEFAULT 0,
    total_jobs_run      INTEGER NOT NULL DEFAULT 0,
    total_credits_used  INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS user_usage_user_id_key ON user_usage(user_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES user_profiles(id),
    name        TEXT NOT NULL,
    key_prefix  TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    last_used_at TIMESTAMP,
    expires_at  TIMESTAMP,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS api_keys_user_id_idx ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS api_keys_key_hash_idx ON api_keys(key_hash);

CREATE TABLE IF NOT EXISTS workflows (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES user_profiles(id),
    name          TEXT NOT NULL,
    description   TEXT,
    trigger_type  trigger_type NOT NULL DEFAULT 'ON_DEMAND',
    cron_schedule TEXT,
    recipe_json   JSONB NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT true,
    last_run_at   TIMESTAMP,
    run_count     INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS workflows_user_id_idx ON workflows(user_id);
CREATE INDEX IF NOT EXISTS workflows_trigger_type_idx ON workflows(trigger_type);
CREATE INDEX IF NOT EXISTS workflows_is_active_idx ON workflows(is_active);

CREATE TABLE IF NOT EXISTS jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES user_profiles(id),
    workflow_id     UUID NOT NULL REFERENCES workflows(id),
    status          job_status NOT NULL DEFAULT 'QUEUED',
    scheduled_at    TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    duration_ms     INTEGER,
    current_step    INTEGER,
    current_state   JSONB,
    credits_used    INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    error_stack     TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    result_json     JSONB,
    result_url      TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_workflow_id_idx ON jobs(workflow_id);

CREATE TABLE IF NOT EXISTS job_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id      UUID NOT NULL REFERENCES jobs(id),
    level       log_level NOT NULL DEFAULT 'INFO',
    message     TEXT,
    node_id     TEXT,
    step_index  INTEGER,
    metadata    JSONB,
    timestamp   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS job_logs_job_id_idx ON job_logs(job_id);
CREATE INDEX IF NOT EXISTS job_logs_level_idx ON job_logs(level);
CREATE INDEX IF NOT EXISTS job_logs_timestamp_idx ON job_logs(timestamp);

CREATE TABLE IF NOT EXISTS vault_secrets (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES user_profiles(id),
    key_name         TEXT NOT NULL,
    encrypted_value  TEXT NOT NULL,
    requires_pin     BOOLEAN NOT NULL DEFAULT false,
    pin_hash         TEXT,
    last_accessed_at TIMESTAMP,
    access_count     INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS vault_secrets_user_id_idx ON vault_secrets(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS vault_secrets_user_id_key_name_key ON vault_secrets(user_id, key_name);

CREATE TABLE IF NOT EXISTS storage_assets (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES user_profiles(id),
    job_id         UUID REFERENCES jobs(id),
    type           asset_type NOT NULL,
    filename       TEXT NOT NULL,
    friendly_name  TEXT,
    mime_type      TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    azure_blob_url TEXT NOT NULL,
    azure_blob_id  TEXT NOT NULL,
    is_public      BOOLEAN NOT NULL DEFAULT false,
    expires_at     TIMESTAMP,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS storage_assets_user_id_idx ON storage_assets(user_id);
CREATE INDEX IF NOT EXISTS storage_assets_job_id_idx ON storage_assets(job_id);
CREATE INDEX IF NOT EXISTS storage_assets_type_idx ON storage_assets(type);

CREATE TABLE IF NOT EXISTS credit_transactions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES user_profiles(id),
    type          transaction_type NOT NULL,
    amount        INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    description   TEXT NOT NULL,
    job_id        UUID REFERENCES jobs(id),
    metadata      JSONB,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS credit_transactions_user_id_idx ON credit_transactions(user_id);
CREATE INDEX IF NOT EXISTS credit_transactions_job_id_idx ON credit_transactions(job_id);
CREATE INDEX IF NOT EXISTS credit_transactions_created_at_idx ON credit_transactions(created_at);
