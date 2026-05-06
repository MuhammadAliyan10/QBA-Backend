package models

import (
	"time"
)

// VaultSession stores encrypted browser session states for the 'quanta auth' command.
// Each record is strictly anchored to a multi-tenant UserID.
type VaultSession struct {
	ID             string    `gorm:"primaryKey;column:id;type:uuid;default:gen_random_uuid()"`
	UserID         string    `gorm:"column:user_id;type:uuid;not null;index"`
	TargetURL      string    `gorm:"column:target_url;type:text;not null"`
	EncryptedState []byte    `gorm:"column:encrypted_state;type:bytea;not null"`
	CreatedAt      time.Time `gorm:"column:created_at;index"`
	ExpiresAt      time.Time `gorm:"column:expires_at;index"`
}

// TableName specifies the table name for GORM
func (VaultSession) TableName() string {
	return "vault_sessions"
}
