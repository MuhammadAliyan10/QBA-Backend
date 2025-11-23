package billing

import (
	"context"
	"database/sql"
	"encoding/json"
	"log"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
	"github.com/nats-io/nats.go"
)

// LedgerConsumer listens to billing.events from NATS and writes to PostgreSQL.
// This keeps async ledger writes off the critical request path.
type LedgerConsumer struct {
	db   *sql.DB
	nats *nats.Conn
	sub  *nats.Subscription
}

// BillingEvent represents a billing transaction event.
type BillingEvent struct {
	UserID        string `json:"user_id"`
	Amount        int    `json:"amount"`
	BalanceAfter  int    `json:"balance_after"`
	Type          string `json:"type"`
	Timestamp     int64  `json:"timestamp"`
	TransactionID string `json:"transaction_id,omitempty"`
	Metadata      string `json:"metadata,omitempty"`
}

// NewLedgerConsumer creates a new ledger consumer.
func NewLedgerConsumer(db *sql.DB, nc *nats.Conn) *LedgerConsumer {
	return &LedgerConsumer{
		db:   db,
		nats: nc,
	}
}

// Start begins consuming billing events and writing to ledger.
func (lc *LedgerConsumer) Start() error {
	var err error

	// Subscribe to billing.events
	lc.sub, err = lc.nats.Subscribe("billing.events", lc.handleBillingEvent)
	if err != nil {
		return err
	}

	log.Println("📒 Ledger Consumer started. Listening for billing events...")
	return nil
}

// handleBillingEvent processes a single billing event.
func (lc *LedgerConsumer) handleBillingEvent(msg *nats.Msg) {
	var event BillingEvent

	// Parse event
	if err := json.Unmarshal(msg.Data, &event); err != nil {
		log.Printf("❌ Failed to parse billing event: %v", err)
		return
	}

	// Generate transaction ID if not provided
	if event.TransactionID == "" {
		event.TransactionID = uuid.New().String()
	}

	// Write to ledger
	if err := lc.writeLedgerEntry(context.Background(), &event); err != nil {
		log.Printf("❌ Failed to write ledger entry for user %s: %v", event.UserID, err)
		// TODO: Add retry logic or dead letter queue
		return
	}

	log.Printf("📝 Ledger entry written for user %s (amount: %d, balance: %d)",
		event.UserID, event.Amount, event.BalanceAfter)
}

// writeLedgerEntry inserts a transaction into the ledger table.
func (lc *LedgerConsumer) writeLedgerEntry(ctx context.Context, event *BillingEvent) error {
	query := `
		INSERT INTO ledger_transactions 
		(user_id, amount, balance_after, transaction_type, transaction_id, metadata, created_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7)
		ON CONFLICT (transaction_id) DO NOTHING
	`

	createdAt := time.Unix(event.Timestamp, 0)

	_, err := lc.db.ExecContext(ctx, query,
		event.UserID,
		event.Amount,
		event.BalanceAfter,
		event.Type,
		event.TransactionID,
		event.Metadata,
		createdAt,
	)

	return err
}

// Stop gracefully stops the ledger consumer.
func (lc *LedgerConsumer) Stop() error {
	if lc.sub != nil {
		if err := lc.sub.Unsubscribe(); err != nil {
			return err
		}
	}

	log.Println("📒 Ledger Consumer stopped.")
	return nil
}

// GetUserTransactions retrieves transaction history for a user.
func (lc *LedgerConsumer) GetUserTransactions(ctx context.Context, userID string, limit int) ([]BillingEvent, error) {
	query := `
		SELECT user_id, amount, balance_after, transaction_type, 
		       transaction_id, COALESCE(metadata, '{}'), 
		       EXTRACT(EPOCH FROM created_at)::bigint
		FROM ledger_transactions
		WHERE user_id = $1
		ORDER BY created_at DESC
		LIMIT $2
	`

	rows, err := lc.db.QueryContext(ctx, query, userID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var transactions []BillingEvent
	for rows.Next() {
		var event BillingEvent
		if err := rows.Scan(
			&event.UserID,
			&event.Amount,
			&event.BalanceAfter,
			&event.Type,
			&event.TransactionID,
			&event.Metadata,
			&event.Timestamp,
		); err != nil {
			return nil, err
		}
		transactions = append(transactions, event)
	}

	return transactions, nil
}
