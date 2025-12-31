-- Migration: Account Pool for Session Rehydration
-- Purpose: Store shared accounts with encrypted passwords and session cookies
-- Database: PostgreSQL (Supabase)

-- Create account_pool table
CREATE TABLE IF NOT EXISTS account_pool (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain TEXT NOT NULL,
    username TEXT NOT NULL,
    password_encrypted TEXT NOT NULL,
    cookies JSONB,
    status TEXT NOT NULL DEFAULT 'AVAILABLE',
    leased_at TIMESTAMP,
    last_used_at TIMESTAMP,
    success_rate FLOAT DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),

    -- Ensure unique combination of domain + username
    CONSTRAINT uk_account_pool_domain_username UNIQUE(domain, username)
);

-- Index for fast lookups by domain and status
CREATE INDEX IF NOT EXISTS idx_account_pool_domain_status
    ON account_pool(domain, status);

-- Partial index for accounts with cookies (prioritize Fast Path)
CREATE INDEX IF NOT EXISTS idx_account_pool_cookies
    ON account_pool(domain, status)
    WHERE cookies IS NOT NULL;

-- Index for leasing optimization (order by last_used_at)
CREATE INDEX IF NOT EXISTS idx_account_pool_last_used
    ON account_pool(last_used_at)
    WHERE status = 'AVAILABLE';

-- Add comments for documentation
COMMENT ON TABLE account_pool IS 'Shared account pool for session rehydration and fast authentication';
COMMENT ON COLUMN account_pool.password_encrypted IS 'Fernet-encrypted password (never plaintext)';
COMMENT ON COLUMN account_pool.cookies IS 'Browser session cookies for fast path authentication';
COMMENT ON COLUMN account_pool.status IS 'Account status: AVAILABLE, LEASED, COOLDOWN, BANNED, NEEDS_CHECK';
COMMENT ON COLUMN account_pool.success_rate IS 'Success rate (0.0-1.0) for prioritizing reliable accounts';

-- Example status values (for reference, not enforced):
-- AVAILABLE: Ready to be leased
-- LEASED: Currently in use by a worker
-- COOLDOWN: Temporarily unavailable (rate limit protection)
-- BANNED: Account detected as banned/blocked
-- NEEDS_CHECK: Last operation failed, needs manual verification
