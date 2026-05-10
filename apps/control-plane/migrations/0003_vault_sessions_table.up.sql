-- 0003_vault_sessions_table.up.sql
-- Encrypted browser session states for the 'quanta auth' CLI command.

CREATE TABLE IF NOT EXISTS vault_sessions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL,
    name             TEXT,
    target_url       TEXT NOT NULL,
    encrypted_state  BYTEA NOT NULL,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at       TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS vault_sessions_user_id_idx ON vault_sessions(user_id);
CREATE INDEX IF NOT EXISTS vault_sessions_created_at_idx ON vault_sessions(created_at);
CREATE INDEX IF NOT EXISTS vault_sessions_expires_at_idx ON vault_sessions(expires_at);
