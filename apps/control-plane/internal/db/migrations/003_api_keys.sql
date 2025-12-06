-- Migration: API Keys for Authentication
-- Purpose: Store hashed API keys for user authentication

CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL, -- First 8 chars for display (sk_live_AbCd...)
    name TEXT NOT NULL,        -- User-defined name for the key
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    last_used_at TIMESTAMP,
    expires_at TIMESTAMP,

    -- Foreign key constraint (assuming users table exists)
    -- CONSTRAINT fk_api_keys_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE

    CONSTRAINT uk_api_keys_hash UNIQUE(key_hash)
);

-- Index for fast lookups by key_hash
CREATE INDEX IF NOT EXISTS idx_api_keys_hash_active
    ON api_keys(key_hash)
    WHERE active = true;

-- Index for user's keys
CREATE INDEX IF NOT EXISTS idx_api_keys_user
    ON api_keys(user_id);

COMMENT ON TABLE api_keys IS 'API keys for programmatic access (hashed with SHA-256)';
COMMENT ON COLUMN api_keys.key_hash IS 'SHA-256 hash of the API key (never store plaintext)';
COMMENT ON COLUMN api_keys.key_prefix IS 'First 8 chars for display only (sk_live_xxx...)';
