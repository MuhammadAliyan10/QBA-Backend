-- 0004_hardening.up.sql
-- Fixes all Supabase dashboard security warnings and missing schema elements.
-- Safe to run multiple times (all statements are idempotent).

-- =============================================================================
-- 1. MISSING COLUMN: job_logs.duration_ms
--    Control-plane was logging ERROR on every telemetry event because this
--    column exists in the ORM model but not in the actual table.
-- =============================================================================
ALTER TABLE job_logs ADD COLUMN IF NOT EXISTS duration_ms INTEGER;

-- =============================================================================
-- 2. MISSING COLUMN: jobs.workflow_id nullable fix
--    The Go controller creates ad-hoc jobs (no editor workflow) with a dummy
--    workflow_id. Make the FK nullable so direct API calls don't require a
--    pre-existing workflow row.
-- =============================================================================
ALTER TABLE jobs ALTER COLUMN workflow_id DROP NOT NULL;

-- =============================================================================
-- 3. ROW LEVEL SECURITY (RLS)
--    Supabase flags every table without RLS enabled as a critical vulnerability.
--    These policies ensure users can only read/write their own data.
-- =============================================================================

-- Enable RLS on all user-data tables
ALTER TABLE user_profiles       ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_usage          ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys            ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflows           ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs                ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_logs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE vault_secrets       ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_assets      ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE credentials         ENABLE ROW LEVEL SECURITY;
ALTER TABLE vault_sessions      ENABLE ROW LEVEL SECURITY;

-- Helper: resolve Clerk JWT sub claim to internal user UUID
-- The Go control-plane passes clerk_user_id in the JWT 'sub' claim.
CREATE OR REPLACE FUNCTION current_user_id() RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT id FROM user_profiles
    WHERE clerk_user_id = auth.jwt() ->> 'sub'
    LIMIT 1;
$$;

-- user_profiles: users see only their own row
DROP POLICY IF EXISTS "users_own_profile" ON user_profiles;
CREATE POLICY "users_own_profile" ON user_profiles
    FOR ALL USING (id = current_user_id());

-- user_usage: users see only their own usage
DROP POLICY IF EXISTS "users_own_usage" ON user_usage;
CREATE POLICY "users_own_usage" ON user_usage
    FOR ALL USING (user_id = current_user_id());

-- api_keys: users see only their own keys
DROP POLICY IF EXISTS "users_own_api_keys" ON api_keys;
CREATE POLICY "users_own_api_keys" ON api_keys
    FOR ALL USING (user_id = current_user_id());

-- workflows: users see only their own workflows
DROP POLICY IF EXISTS "users_own_workflows" ON workflows;
CREATE POLICY "users_own_workflows" ON workflows
    FOR ALL USING (user_id = current_user_id());

-- jobs: users see only their own jobs
DROP POLICY IF EXISTS "users_own_jobs" ON jobs;
CREATE POLICY "users_own_jobs" ON jobs
    FOR ALL USING (user_id = current_user_id());

-- job_logs: users see logs only for their own jobs
DROP POLICY IF EXISTS "users_own_job_logs" ON job_logs;
CREATE POLICY "users_own_job_logs" ON job_logs
    FOR ALL USING (
        job_id IN (SELECT id FROM jobs WHERE user_id = current_user_id())
    );

-- vault_secrets: users see only their own secrets
DROP POLICY IF EXISTS "users_own_vault_secrets" ON vault_secrets;
CREATE POLICY "users_own_vault_secrets" ON vault_secrets
    FOR ALL USING (user_id = current_user_id());

-- storage_assets: users see only their own assets
DROP POLICY IF EXISTS "users_own_storage_assets" ON storage_assets;
CREATE POLICY "users_own_storage_assets" ON storage_assets
    FOR ALL USING (user_id = current_user_id());

-- credit_transactions: users see only their own transactions
DROP POLICY IF EXISTS "users_own_credit_transactions" ON credit_transactions;
CREATE POLICY "users_own_credit_transactions" ON credit_transactions
    FOR ALL USING (user_id = current_user_id());

-- credentials: client_id maps to user UUID
DROP POLICY IF EXISTS "users_own_credentials" ON credentials;
CREATE POLICY "users_own_credentials" ON credentials
    FOR ALL USING (client_id = current_user_id());

-- vault_sessions: user_id is a UUID matching internal user ID
DROP POLICY IF EXISTS "users_own_vault_sessions" ON vault_sessions;
CREATE POLICY "users_own_vault_sessions" ON vault_sessions
    FOR ALL USING (user_id = current_user_id());

-- =============================================================================
-- 4. SERVICE ROLE BYPASS
--    The Go control-plane connects via the service role key (bypasses RLS).
--    This is correct — the backend enforces ownership in application code.
--    These policies only apply to direct Supabase client access.
-- =============================================================================

-- Grant service role full access (already default in Supabase, explicit for clarity)
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- =============================================================================
-- 5. PERFORMANCE INDEXES
--    Missing indexes Supabase flags as warnings.
-- =============================================================================

-- Jobs: composite index for the most common query (user's recent jobs)
CREATE INDEX IF NOT EXISTS idx_jobs_user_status
    ON jobs(user_id, status, created_at DESC);

-- job_logs: composite index for fetching logs for a specific job in order
CREATE INDEX IF NOT EXISTS idx_job_logs_job_timestamp
    ON job_logs(job_id, timestamp DESC);

-- vault_sessions: index for cleaning up expired sessions
CREATE INDEX IF NOT EXISTS idx_vault_sessions_expires
    ON vault_sessions(expires_at);

-- api_keys: fast lookup for active keys (used on every API request)
CREATE INDEX IF NOT EXISTS idx_api_keys_active
    ON api_keys(key_hash) WHERE is_active = true;

-- =============================================================================
-- 6. AUTO-UPDATE updated_at TRIGGER
--    Supabase warns when updated_at columns are not automatically maintained.
-- =============================================================================
CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

DO $$ BEGIN
    CREATE TRIGGER set_updated_at_user_profiles
        BEFORE UPDATE ON user_profiles
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER set_updated_at_user_usage
        BEFORE UPDATE ON user_usage
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER set_updated_at_workflows
        BEFORE UPDATE ON workflows
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER set_updated_at_vault_secrets
        BEFORE UPDATE ON vault_secrets
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER set_updated_at_credentials
        BEFORE UPDATE ON credentials
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =============================================================================
-- 7. VAULT SESSIONS CLEANUP FUNCTION
--    Removes expired sessions. Call this via pg_cron or manually.
-- =============================================================================
CREATE OR REPLACE FUNCTION purge_expired_vault_sessions()
RETURNS INTEGER LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM vault_sessions WHERE expires_at < CURRENT_TIMESTAMP;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;

-- =============================================================================
-- 8. JOB LOG LEVEL CAST
--    The Go control-plane inserts level as TEXT ('RUNNING', 'FAILED' etc.)
--    but the enum only has DEBUG/INFO/WARN/ERROR. Add the missing values.
-- =============================================================================
DO $$ BEGIN
    ALTER TYPE log_level ADD VALUE IF NOT EXISTS 'RUNNING';
EXCEPTION WHEN others THEN NULL; END $$;

DO $$ BEGIN
    ALTER TYPE log_level ADD VALUE IF NOT EXISTS 'FAILED';
EXCEPTION WHEN others THEN NULL; END $$;

DO $$ BEGIN
    ALTER TYPE log_level ADD VALUE IF NOT EXISTS 'SUCCESS';
EXCEPTION WHEN others THEN NULL; END $$;
