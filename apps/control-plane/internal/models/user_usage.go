package models

import (
	"time"
)

// UserUsage tracks aggregated token consumption per user per billing period.
// Maps to: migrations/005_token_usage.sql → user_usage table.
type UserUsage struct {
	ID                string    `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()"`
	UserID            string    `gorm:"column:user_id;index"`
	PeriodStart       time.Time `gorm:"column:period_start;type:date;uniqueIndex:idx_user_period"`
	PeriodEnd         time.Time `gorm:"column:period_end;type:date"`
	PromptTokens      int64     `gorm:"column:prompt_tokens;default:0"`
	CompletionTokens  int64     `gorm:"column:completion_tokens;default:0"`
	TotalTokens       int64     `gorm:"column:total_tokens;default:0"`
	LLMCalls          int       `gorm:"column:llm_calls;default:0"`
	JobsRun           int       `gorm:"column:jobs_run;default:0"`
	StripeMeterEventID *string  `gorm:"column:stripe_meter_event_id"`
	CreatedAt         time.Time
	UpdatedAt         time.Time
}

// TableName specifies the table name for GORM
func (UserUsage) TableName() string {
	return "user_usage"
}
