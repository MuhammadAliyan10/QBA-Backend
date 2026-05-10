// internal/models/vault_secret.go
package models

import "time"

// VaultSecret stores encrypted key-value secrets for a user.
// The encrypted_value column contains AES-256-GCM ciphertext with the
// 12-byte nonce prepended: base64(nonce || ciphertext || tag).
// Plaintext NEVER touches the database.
type VaultSecret struct {
	ID             string     `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()" json:"id"`
	UserID         string     `gorm:"column:user_id;type:uuid;index;not null"                  json:"-"`
	KeyName        string     `gorm:"column:key_name;type:text;not null"                       json:"key_name"`
	EncryptedValue string     `gorm:"column:encrypted_value;type:text;not null"                json:"-"`
	RequiresPin    bool       `gorm:"column:requires_pin;default:false"                        json:"requires_pin"`
	PinHash        *string    `gorm:"column:pin_hash;type:text"                                json:"-"`
	LastAccessedAt *time.Time `gorm:"column:last_accessed_at"                                  json:"last_accessed_at"`
	AccessCount    int        `gorm:"column:access_count;default:0"                            json:"access_count"`
	CreatedAt      time.Time  `gorm:"column:created_at"                                        json:"created_at"`
	UpdatedAt      time.Time  `gorm:"column:updated_at"                                        json:"updated_at"`
}

// TableName specifies the table name for GORM.
func (VaultSecret) TableName() string {
	return "vault_secrets"
}
