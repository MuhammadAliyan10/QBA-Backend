-- 0002_credentials_table.up.sql
-- BYOS encrypted Playwright session storage (managed by Go CredentialController).

CREATE TABLE IF NOT EXISTS credentials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL,
    name            TEXT NOT NULL,
    encrypted_data  BYTEA NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS credentials_client_id_idx ON credentials(client_id);
