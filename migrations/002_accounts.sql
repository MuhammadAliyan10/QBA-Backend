-- Migration 002: Create Accounts Pool Table
-- Purpose: Store browser account credentials for the execution plane
-- Author: Production Readiness Audit
-- Date: 2025-12-10

CREATE TABLE IF NOT EXISTS accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform VARCHAR(100) NOT NULL,
    username VARCHAR(255) NOT NULL,
    encrypted_password TEXT NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'AVAILABLE',
    leased_by VARCHAR(255),
    leased_at TIMESTAMP,
    lease_expires_at TIMESTAMP,
    last_used_at TIMESTAMP,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(platform, username)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_accounts_platform_status ON accounts(platform, status);
CREATE INDEX IF NOT EXISTS idx_accounts_leased_by ON accounts(leased_by);
CREATE INDEX IF NOT EXISTS idx_accounts_lease_expires ON accounts(lease_expires_at);

-- Comment
COMMENT ON TABLE accounts IS 'Pool of browser accounts for automation tasks';
COMMENT ON COLUMN accounts.encrypted_password IS 'Fernet-encrypted password';
COMMENT ON COLUMN accounts.leased_by IS 'Job ID that currently holds the lease';
