-- Migration 006: Credentials Vault
-- Purpose: Store AES-256-GCM encrypted Playwright storage_state blobs for BYOS execution
-- Author: Credentials Vault Implementation
-- Date: 2026-05-02

CREATE TABLE IF NOT EXISTS credentials (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id      UUID NOT NULL,
    name           TEXT NOT NULL,
    encrypted_data BYTEA NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_credentials_client_id ON credentials(client_id);

COMMENT ON TABLE credentials IS 'Encrypted Playwright session storage for BYOS (Bring Your Own Session) execution';
COMMENT ON COLUMN credentials.encrypted_data IS 'AES-256-GCM encrypted JSON blob of the Playwright storage_state. Nonce is prepended to the ciphertext.';
COMMENT ON COLUMN credentials.client_id IS 'Tenant owner UUID — matches the user_id from the auth middleware';
