-- 0005_account_pool.up.sql
-- Account credential pool for multi-account automation.
-- Referenced by account_manager.py (execution-plane).
-- Uses FOR UPDATE SKIP LOCKED for atomic lease semantics.

-- ============================================================
-- ENUM: account status lifecycle
-- ============================================================
DO $$ BEGIN
    CREATE TYPE account_status AS ENUM (
        'AVAILABLE',
        'LEASED',
        'NEEDS_CHECK',
        'DISABLED'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- TABLE: account_pool
-- ============================================================
CREATE TABLE IF NOT EXISTS account_pool (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Target site identification
    domain              TEXT NOT NULL,
    username            TEXT NOT NULL,

    -- Credentials (execution-plane encrypts with Fernet key)
    password_encrypted  TEXT NOT NULL,

    -- Playwright storage_state (JSON array of cookie objects, may be NULL)
    cookies             JSONB,

    -- Lease lifecycle
    status              account_status NOT NULL DEFAULT 'AVAILABLE',
    leased_at           TIMESTAMP,

    -- Usage tracking
    last_used_at        TIMESTAMP,
    success_rate        DOUBLE PRECISION NOT NULL DEFAULT 1.0
        CONSTRAINT success_rate_range CHECK (success_rate >= 0.0 AND success_rate <= 1.0),

    -- Record timestamps
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Optional expiry (NULL = never expires)
    expires_at          TIMESTAMP,

    -- Multi-tenancy: which Quanta user owns this account
    user_id             UUID REFERENCES user_profiles(id) ON DELETE CASCADE,

    -- Prevent duplicate username per domain per user
    CONSTRAINT account_pool_unique_domain_username UNIQUE (domain, username, user_id)
);

-- ============================================================
-- INDEXES
-- ============================================================

-- Primary lookup used by lease_account():
--   WHERE domain = ? AND status = 'AVAILABLE' AND (expires_at IS NULL OR expires_at > NOW())
--   ORDER BY (cookies IS NOT NULL) DESC, last_used_at ASC NULLS FIRST
--   LIMIT 1 FOR UPDATE SKIP LOCKED
CREATE INDEX IF NOT EXISTS idx_account_pool_domain_status
    ON account_pool (domain, status)
    WHERE status = 'AVAILABLE';

-- For expiry cleanup
CREATE INDEX IF NOT EXISTS idx_account_pool_expires_at
    ON account_pool (expires_at)
    WHERE expires_at IS NOT NULL;

-- For multi-tenant listing
CREATE INDEX IF NOT EXISTS idx_account_pool_user_id
    ON account_pool (user_id);

-- ============================================================
-- AUTO-UPDATE updated_at TRIGGER
-- ============================================================
DO $$ BEGIN
    CREATE TRIGGER set_updated_at_account_pool
        BEFORE UPDATE ON account_pool
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================
ALTER TABLE account_pool ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "users_own_account_pool" ON account_pool;
CREATE POLICY "users_own_account_pool" ON account_pool
    FOR ALL
    USING (user_id = current_user_id());

-- ============================================================
-- COMMENTS
-- ============================================================
COMMENT ON TABLE account_pool IS
    'Credential pool for multi-account browser automation. '
    'Leased atomically via FOR UPDATE SKIP LOCKED. '
    'Passwords are Fernet-encrypted by the execution-plane.';
COMMENT ON COLUMN account_pool.cookies IS
    'Playwright storage_state cookie array in JSONB. NULL = not yet authenticated.';
COMMENT ON COLUMN account_pool.success_rate IS
    'Exponential moving average: new = 0.9 * old + 0.1 * current (1=success, 0=failure).';
COMMENT ON COLUMN account_pool.status IS
    'AVAILABLE=ready, LEASED=in-use by a job, NEEDS_CHECK=failed, DISABLED=admin-blocked.';
