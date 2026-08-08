-- Migration 008: Auto-refresh trigger for user_balance_summary
-- Purpose: Replace the manual refresh_balance_summary() call pattern with an
--          automatic trigger that fires on every ledger_transactions INSERT/UPDATE.
--          Without this, the materialized view goes stale between cron refreshes.
-- Author: P3 Code Quality Pass
-- Date: 2026-08-08

-- ─── TRIGGER FUNCTION ────────────────────────────────────────────────────────
-- Uses REFRESH MATERIALIZED VIEW CONCURRENTLY so the view is readable during refresh.
-- CONCURRENTLY requires the unique index on user_id (created in 003_ledger.sql).
CREATE OR REPLACE FUNCTION _auto_refresh_balance_summary()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    -- CONCURRENTLY: allows concurrent reads during refresh; no table lock.
    -- Does NOT block the INSERT that triggered it because we run via AFTER trigger.
    REFRESH MATERIALIZED VIEW CONCURRENTLY user_balance_summary;
    RETURN NULL;
END;
$$;

-- ─── TRIGGER ─────────────────────────────────────────────────────────────────
-- Fires AFTER each INSERT or UPDATE on ledger_transactions.
-- FOR EACH STATEMENT (not FOR EACH ROW) means one refresh per transaction,
-- not one per row — critical for bulk inserts.
DROP TRIGGER IF EXISTS trg_refresh_balance_summary ON ledger_transactions;

CREATE TRIGGER trg_refresh_balance_summary
    AFTER INSERT OR UPDATE
    ON ledger_transactions
    FOR EACH STATEMENT
    EXECUTE FUNCTION _auto_refresh_balance_summary();

COMMENT ON FUNCTION _auto_refresh_balance_summary() IS
    'Auto-refreshes user_balance_summary materialized view after any ledger write. '
    'Replaces manual refresh_balance_summary() call from the application layer.';

COMMENT ON TRIGGER trg_refresh_balance_summary ON ledger_transactions IS
    'Fires AFTER INSERT/UPDATE, FOR EACH STATEMENT. '
    'Uses CONCURRENTLY to avoid read locks during refresh.';
