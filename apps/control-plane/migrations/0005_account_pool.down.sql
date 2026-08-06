-- 0005_account_pool.down.sql
-- Rolls back migration 0005 (account_pool table).

DROP TABLE IF EXISTS account_pool CASCADE;

DO $$ BEGIN
    DROP TYPE account_status;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;
