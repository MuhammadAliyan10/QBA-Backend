-- Migration: Create Ledger Transactions Table
-- Purpose: Store complete audit trail of all billing events
-- Author: Principal Security Engineer
-- Date: 2025-11-23

CREATE TABLE IF NOT EXISTS ledger_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL,
    amount INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    transaction_type VARCHAR(50) NOT NULL CHECK (transaction_type IN ('DEDUCTION', 'TOPUP', 'REFUND', 'ADJUSTMENT')),
    transaction_id VARCHAR(255) UNIQUE,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON ledger_transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_created ON ledger_transactions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_type ON ledger_transactions(transaction_type);

-- User balance summary (materialized view for analytics)
CREATE MATERIALIZED VIEW IF NOT EXISTS user_balance_summary AS
SELECT 
    user_id,
    SUM(amount) as total_credits_added,
    COUNT(*) FILTER (WHERE transaction_type = 'DEDUCTION') as total_deductions,
    COUNT(*) FILTER (WHERE transaction_type = 'TOPUP') as total_topups,
    MAX(created_at) as last_transaction_at
FROM ledger_transactions
GROUP BY user_id;

CREATE UNIQUE INDEX ON user_balance_summary(user_id);

-- Refresh function for materialized view
CREATE OR REPLACE FUNCTION refresh_balance_summary()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY user_balance_summary;
END;
$$ LANGUAGE plpgsql;

-- Comment
COMMENT ON TABLE ledger_transactions IS 'Complete audit trail of billing events from NATS stream';
COMMENT ON COLUMN ledger_transactions.metadata IS 'JSON field for extensibility (e.g., stripe_session_id, refund_reason)';
