package models

import "time"

// ApiKey represents an API key for programmatic access
// Mapped from Prisma: model ApiKey
type ApiKey struct {
	ID        string `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()"`
	UserID    string `gorm:"column:user_id;type:uuid;index"`
	Name      string
	KeyPrefix string `gorm:"column:key_prefix"`
	KeyHash   string `gorm:"column:key_hash;index"`

	LastUsedAt *time.Time `gorm:"column:last_used_at"`
	ExpiresAt  *time.Time `gorm:"column:expires_at"`
	IsActive   bool       `gorm:"column:is_active;default:true"`

	CreatedAt time.Time
}

// TableName specifies the table name for GORM
func (ApiKey) TableName() string {
	return "api_keys"
}
