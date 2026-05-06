// internal/models/credential.go
package models

import "time"

// Credential stores an encrypted Playwright storage_state blob for a given client.
// EncryptedData is excluded from JSON serialization via `json:"-"` to prevent
// accidental leakage of sensitive session material in API responses.
type Credential struct {
	ID            string    `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()" json:"id"`
	ClientID      string    `gorm:"column:client_id;type:uuid;index;not null"               json:"client_id"`
	Name          string    `gorm:"column:name;type:text;not null"                          json:"name"`
	EncryptedData []byte    `gorm:"column:encrypted_data;type:bytea;not null"               json:"-"`
	CreatedAt     time.Time `gorm:"column:created_at;autoCreateTime"                        json:"created_at"`
	UpdatedAt     time.Time `gorm:"column:updated_at;autoUpdateTime"                        json:"updated_at"`
}

// TableName specifies the table name for GORM.
func (Credential) TableName() string {
	return "credentials"
}
