package models

import (
	"time"

	"gorm.io/gorm"
)

// UserProfile represents a user profile linked to Clerk authentication
// Mapped from Prisma: model UserProfile
type UserProfile struct {
	ID          string `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()"`
	ClerkUserID string `gorm:"column:clerk_user_id;uniqueIndex"`
	Email       string `gorm:"column:email;uniqueIndex"`
	FirstName   *string `gorm:"column:first_name"`
	LastName    *string `gorm:"column:last_name"`
	AvatarURL   *string `gorm:"column:avatar_url"`
	Tier        string `gorm:"column:tier;type:user_tier"`

	// Webhook configuration
	WebhookURL    *string `gorm:"column:webhook_url"`
	WebhookSecret *string `gorm:"column:webhook_secret"`

	CreatedAt time.Time
	UpdatedAt time.Time
	DeletedAt gorm.DeletedAt `gorm:"index"`
}

// TableName specifies the table name for GORM
func (UserProfile) TableName() string {
	return "user_profiles"
}
