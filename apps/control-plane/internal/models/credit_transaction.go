// internal/models/credit_transaction.go
package models

import (
	"time"

	"gorm.io/datatypes"
)

// CreditTransaction records individual credit movements (debits, credits, refunds).
type CreditTransaction struct {
	ID           string         `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()" json:"id"`
	UserID       string         `gorm:"column:user_id;type:uuid;index;not null"                  json:"user_id"`
	Type         string         `gorm:"column:type;type:transaction_type;not null"               json:"type"`
	Amount       int            `gorm:"column:amount;not null"                                   json:"amount"`
	BalanceAfter int            `gorm:"column:balance_after;not null"                             json:"balance_after"`
	Description  string         `gorm:"column:description;type:text;not null"                    json:"description"`
	JobID        *string        `gorm:"column:job_id;type:uuid;index"                            json:"job_id,omitempty"`
	Metadata     datatypes.JSON `gorm:"column:metadata;type:jsonb"                               json:"metadata,omitempty"`
	CreatedAt    time.Time      `gorm:"column:created_at"                                        json:"created_at"`
}

// TableName specifies the table name for GORM.
func (CreditTransaction) TableName() string {
	return "credit_transactions"
}
