package db

import (
	"database/sql"
	"fmt"
	"log"
	"os"

	// Import Postgres Driver
	_ "github.com/lib/pq"
)

var Conn *sql.DB

// Init connects to CockroachDB and runs migrations
func Init() {
	dsn := os.Getenv("DB_DSN")
	if dsn == "" {
		// Fallback for local development (matches docker-compose)
		dsn = "postgresql://root@localhost:26257/defaultdb?sslmode=disable"
	}

	var err error
	Conn, err = sql.Open("postgres", dsn)
	if err != nil {
		log.Fatalf("[ERROR] Failed to open DB connection: %v", err)
	}

	if err = Conn.Ping(); err != nil {
		log.Fatalf("[ERROR] DB Unreachable: %v", err)
	}

	log.Println("[Database] Successfully connected to CockroachDB")

	// Run Auto-Migration
	if err := createTables(); err != nil {
		log.Fatalf("[ERROR] Migration Failed: %v", err)
	}
}

// createTables ensures the schema exists (Idempotent)
func createTables() error {
	schema := `
    -- 1. USERS TABLE
    CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email TEXT UNIQUE NOT NULL,
        api_key TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT now()
    );

    -- 2. LEDGER TABLE (Double-Entry Accounting)
    CREATE TABLE IF NOT EXISTS ledger (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID REFERENCES users(id),
        amount DECIMAL(20, 8) NOT NULL,
        job_id TEXT,
        description TEXT,
        created_at TIMESTAMP DEFAULT now()
    );

    -- 3. JOBS TABLE (Workflow Execution Tracking)
    -- CRITICAL: This table is required for webhook dispatch in main.go
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        user_id UUID REFERENCES users(id),
        status TEXT DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'PAUSED')),
        webhook_url TEXT,
        params JSONB DEFAULT '{}',
        result JSONB DEFAULT '{}',
        error_message TEXT,
        created_at TIMESTAMP DEFAULT now(),
        updated_at TIMESTAMP DEFAULT now(),
        completed_at TIMESTAMP
    );

    -- INDEX: Optimize status polling and webhook queries
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
    CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);
    CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

    -- 4. SEED USER (For Local Dev only)
    INSERT INTO users (id, email, api_key)
    VALUES ('00000000-0000-0000-0000-000000000000', 'dev@local', 'sk_test_123')
    ON CONFLICT (id) DO NOTHING;
    `
	_, err := Conn.Exec(schema)
	if err != nil {
		return err
	}

	// 4. SEED CREDIT (Separate insert to avoid FK issues)
	// Only insert if balance is 0 (idempotent)
	_, err = Conn.Exec(`
		INSERT INTO ledger (user_id, amount, description)
		SELECT '00000000-0000-0000-0000-000000000000', 10.00, 'Initial Grant'
		WHERE NOT EXISTS (
			SELECT 1 FROM ledger
			WHERE user_id = '00000000-0000-0000-0000-000000000000'
		);
	`)
	return err
}

// --- PUBLIC HELPERS ---

// GetBalance calculates the current balance for a user
func GetBalance(userID string) (float64, error) {
	var balance float64
	// In Double-Entry, Balance = Sum of all transactions
	err := Conn.QueryRow("SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE user_id = $1", userID).Scan(&balance)
	return balance, err
}

// ChargeUser deducts money (inserts a negative record)
func ChargeUser(userID string, amount float64, jobID string) error {
	if amount <= 0 {
		return fmt.Errorf("invalid charge amount: %f", amount)
	}
	// We insert a NEGATIVE amount for a charge
	_, err := Conn.Exec(
		"INSERT INTO ledger (user_id, amount, job_id, description) VALUES ($1, $2, $3, 'Compute Cost')",
		userID, -amount, jobID,
	)
	return err
}
