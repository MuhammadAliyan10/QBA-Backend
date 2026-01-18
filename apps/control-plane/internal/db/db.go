package db

import (
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

var DB *gorm.DB

// Init connects to PostgreSQL (Supabase) using GORM
// CRITICAL: PrepareStmt is disabled for Supabase PgBouncer compatibility
func Init() {
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		// Fallback for local development (matches docker-compose PostgreSQL)
		dsn = "postgresql://postgres:postgres@localhost:5433/quanta?sslmode=disable"
		log.Println("[Database] Using default local PostgreSQL DSN")
	}

	// Validate DSN format (must be PostgreSQL)
	if !strings.HasPrefix(dsn, "postgresql://") && !strings.HasPrefix(dsn, "postgres://") {
		log.Fatalf("[ERROR] Invalid DATABASE_URL: must start with postgresql:// or postgres://")
	}

	var err error
	DB, err = gorm.Open(postgres.Open(dsn), &gorm.Config{
		// CRITICAL: Disable prepared statements for Supabase Transaction Pooler (Port 6543)
		// PgBouncer in Transaction Mode does NOT support prepared statements
		PrepareStmt: false,

		Logger: logger.Default.LogMode(logger.Info),
	})

	if err != nil {
		log.Fatalf("[ERROR] Failed to connect to PostgreSQL: %v", err)
	}

	// Configure connection pool
	sqlDB, err := DB.DB()
	if err != nil {
		log.Fatalf("[ERROR] Failed to get underlying SQL.DB: %v", err)
	}

	// CRITICAL: Configure connection pool to prevent exhaustion
	// Supabase free tier has ~60 connections, pro has ~500
	sqlDB.SetMaxOpenConns(20)                 // Prevent exhaustion
	sqlDB.SetMaxIdleConns(5)                  // Keep some warm connections
	sqlDB.SetConnMaxLifetime(5 * time.Minute) // Recycle connections

	log.Println("[Database] ✓ Connected to PostgreSQL (GORM with PrepareStmt=false)")

	// Run Auto-Migration
	if err := createTables(); err != nil {
		log.Fatalf("[ERROR] Migration Failed: %v", err)
	}
}

// Ping checks if the database connection is healthy.
// Used by health check endpoints.
func Ping() error {
	if DB == nil {
		return fmt.Errorf("database connection not initialized")
	}
	sqlDB, err := DB.DB()
	if err != nil {
		return err
	}
	return sqlDB.Ping()
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
        run_id TEXT,
        created_at TIMESTAMP DEFAULT now(),
        updated_at TIMESTAMP DEFAULT now(),
        completed_at TIMESTAMP
    );

    -- INDEX: Optimize status polling and webhook queries
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
    CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);
    CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

    -- MIGRATION: Ensure run_id exists for existing tables
    ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_id TEXT;

    -- 4. SEED USER (For Local Dev only)
    INSERT INTO users (id, email, api_key)
    VALUES ('00000000-0000-0000-0000-000000000000', 'dev@local', 'sk_test_123')
    ON CONFLICT (id) DO NOTHING;
    `
	err := DB.Exec(schema).Error
	if err != nil {
		return err
	}

	// 4. SEED CREDIT (Separate insert to avoid FK issues)
	// Only insert if balance is 0 (idempotent)
	err = DB.Exec(`
		INSERT INTO ledger (user_id, amount, description)
		SELECT '00000000-0000-0000-0000-000000000000', 10.00, 'Initial Grant'
		WHERE NOT EXISTS (
			SELECT 1 FROM ledger
			WHERE user_id = '00000000-0000-0000-0000-000000000000'
		);
	`).Error
	return err
}

// --- PUBLIC HELPERS ---

// GetBalance calculates the current balance for a user
func GetBalance(userID string) (float64, error) {
	var balance float64
	// In Double-Entry, Balance = Sum of all transactions
	err := DB.Raw("SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE user_id = ?", userID).Scan(&balance).Error
	return balance, err
}

// ChargeUser deducts money (inserts a negative record)
func ChargeUser(userID string, amount float64, jobID string) error {
	if amount <= 0 {
		return fmt.Errorf("invalid charge amount: %f", amount)
	}
	// We insert a NEGATIVE amount for a charge
	err := DB.Exec(
		"INSERT INTO ledger (user_id, amount, job_id, description) VALUES (?, ?, ?, 'Compute Cost')",
		userID, -amount, jobID,
	).Error
	return err
}

// GetDB returns the global GORM database instance
func GetDB() *gorm.DB {
	return DB
}
