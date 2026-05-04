package db

import (
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"e2e-platform/apps/control-plane/internal/models"

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
	DB, err = gorm.Open(postgres.New(postgres.Config{
		DSN:                  dsn,
		PreferSimpleProtocol: true, // CRITICAL: Fix for Supabase PgBouncer (prepared statement already exists)
	}), &gorm.Config{
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

	// 1. New Go-Only tables (not managed by Prisma)
	if err := DB.AutoMigrate(&models.VaultSession{}); err != nil {
		log.Printf("[Database] ⚠ VaultSession AutoMigrate failed: %v", err)
	} else {
		log.Println("[Database] ✓ VaultSession schema synchronized")
	}

	// Verify tables exist (Prisma manages the schema via migrations,
	// we only check connectivity — DO NOT AutoMigrate to avoid conflicts with Prisma)
	if err := verifySchema(); err != nil {
		log.Printf("[Database] ⚠ Schema verification warning: %v", err)
		log.Println("[Database] Make sure Prisma migrations have been run on this database")
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

// verifySchema checks that the expected Prisma-managed tables exist.
// We do NOT create or modify tables — Prisma owns the schema.
func verifySchema() error {
	// Check critical tables exist by doing a lightweight query
	tables := []struct {
		model     interface{}
		tableName string
	}{
		{&models.Job{}, "jobs"},
		{&models.UserProfile{}, "user_profiles"},
		{&models.Workflow{}, "workflows"},
		{&models.VaultSession{}, "vault_sessions"},
	}

	for _, t := range tables {
		if !DB.Migrator().HasTable(t.model) {
			return fmt.Errorf("required table '%s' not found — run Prisma migrations first", t.tableName)
		}
	}

	log.Println("[Database] ✓ Schema verified (jobs, user_profiles, workflows)")
	return nil
}

// GetDB returns the global GORM database instance
func GetDB() *gorm.DB {
	return DB
}
