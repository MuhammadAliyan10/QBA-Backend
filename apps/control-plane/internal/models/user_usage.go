package models

import (
	"time"
)

// UserUsage tracks aggregated token consumption per user per billing period.
// Maps to: migrations/005_token_usage.sql → user_usage table.
type UserUsage struct {
	ID               string    `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()" json:"id"`
	UserID           string    `gorm:"column:user_id;type:uuid;index;not null;uniqueIndex"      json:"user_id"`
	CreditsBalance   int       `gorm:"column:credits_balance;default:0;not null"                json:"credits_balance"`
	TotalJobsRun     int       `gorm:"column:total_jobs_run;default:0;not null"                 json:"total_jobs_run"`
	TotalCreditsUsed int       `gorm:"column:total_credits_used;default:0;not null"             json:"total_credits_used"`
	CreatedAt        time.Time `gorm:"column:created_at;autoCreateTime"                         json:"created_at"`
	UpdatedAt        time.Time `gorm:"column:updated_at;autoUpdateTime"                         json:"updated_at"`
}

// TableName specifies the table name for GORM
func (UserUsage) TableName() string {
	return "user_usage"
}
