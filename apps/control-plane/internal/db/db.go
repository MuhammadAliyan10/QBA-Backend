// internal/db/db.go
package db

import (
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"github.com/golang-migrate/migrate/v4"
	_ "github.com/golang-migrate/migrate/v4/database/postgres"
	_ "github.com/golang-migrate/migrate/v4/source/file"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

// DB is the global GORM database instance.
var DB *gorm.DB

// Init connects to PostgreSQL (Supabase) using GORM and runs versioned SQL migrations.
// CRITICAL: PrepareStmt is disabled for Supabase PgBouncer compatibility.
func Init() {
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		dsn = "postgresql://postgres:postgres@localhost:5433/quanta?sslmode=disable"
		log.Println("[Database] Using default local PostgreSQL DSN")
	}

	if !strings.HasPrefix(dsn, "postgresql://") && !strings.HasPrefix(dsn, "postgres://") {
		log.Fatalf("[ERROR] Invalid DATABASE_URL: must start with postgresql:// or postgres://")
	}

	var err error
	DB, err = gorm.Open(postgres.New(postgres.Config{
		DSN:                  dsn,
		PreferSimpleProtocol: true, // CRITICAL: Fix for Supabase PgBouncer
	}), &gorm.Config{
		Logger: logger.Default.LogMode(logger.Info),
	})

	if err != nil {
		log.Fatalf("[ERROR] Failed to connect to PostgreSQL: %v", err)
	}

	sqlDB, err := DB.DB()
	if err != nil {
		log.Fatalf("[ERROR] Failed to get underlying SQL.DB: %v", err)
	}

	// CRITICAL: Configure connection pool to prevent exhaustion
	sqlDB.SetMaxOpenConns(20)
	sqlDB.SetMaxIdleConns(5)
	sqlDB.SetConnMaxLifetime(5 * time.Minute)

	log.Println("[Database] ✓ Connected to PostgreSQL (GORM with PrepareStmt=false)")

	// Run versioned SQL migrations
	runMigrations(dsn)
}

// runMigrations executes pending SQL migrations using golang-migrate.
// Migrations are tracked in the schema_migrations table.
// Fails fast if migration state is dirty.
func runMigrations(dsn string) {
	migrationsPath := os.Getenv("MIGRATIONS_PATH")
	if migrationsPath == "" {
		migrationsPath = "file://migrations"
	}

	// golang-migrate requires postgres:// prefix, not postgresql://
	migrateDSN := dsn
	if strings.HasPrefix(migrateDSN, "postgresql://") {
		migrateDSN = "postgres://" + strings.TrimPrefix(migrateDSN, "postgresql://")
	}

	m, err := migrate.New(migrationsPath, migrateDSN)
	if err != nil {
		log.Printf("[Database] ⚠ Migration setup failed: %v", err)
		log.Println("[Database] Continuing without migrations — ensure schema is up to date manually")
		return
	}
	defer m.Close()

	// Check for dirty state
	version, dirty, _ := m.Version()
	if dirty {
		log.Fatalf("[Database] FATAL: Migration state is dirty at version %d. Manual intervention required.", version)
	}

	err = m.Up()
	switch {
	case err == nil:
		newVersion, _, _ := m.Version()
		log.Printf("[Database] ✓ Migrations applied successfully (now at version %d)", newVersion)
	case err == migrate.ErrNoChange:
		log.Printf("[Database] ✓ Migrations up to date (version %d)", version)
	default:
		log.Printf("[Database] ⚠ Migration error: %v", err)
		log.Println("[Database] The server will continue, but schema may be incomplete")
	}
}

// Ping checks if the database connection is healthy.
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

// GetDB returns the global GORM database instance.
func GetDB() *gorm.DB {
	return DB
}
