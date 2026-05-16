package models

import (
	"time"

	"gorm.io/datatypes"
)

// Job represents a single execution of a workflow
type Job struct {
	ID         string `gorm:"primaryKey;column:id;type:text"`
	WorkflowID string `gorm:"column:workflow_id;type:text"`
	UserID     string `gorm:"column:user_id;type:text;index"`
	Status     string `gorm:"column:status;type:text;index"`

	WebhookURL *string        `gorm:"column:webhook_url;type:text"`
	Params     datatypes.JSON `gorm:"column:params;type:jsonb"`
	Result     datatypes.JSON `gorm:"column:result;type:jsonb"`
	ResultJSON datatypes.JSON `gorm:"column:result_json;type:jsonb"`

	ErrorMessage *string `gorm:"column:error_message;type:text"`

	RunID       *string    `gorm:"column:run_id;type:text"`
	CreatedAt   time.Time  `gorm:"column:created_at;index"`
	UpdatedAt   time.Time  `gorm:"column:updated_at"`
	CompletedAt *time.Time `gorm:"column:completed_at"`

	// Compatibility fields
	ScheduledAt *time.Time      `gorm:"-"`
	StartedAt   *time.Time      `gorm:"-"`
	DurationMs  *int            `gorm:"-"`
	CurrentStep *int            `gorm:"-"`
	CurrentState *datatypes.JSON `gorm:"-"`

	CreditsUsed       int    `gorm:"-"`
	PromptTokens      int    `gorm:"-"`
	CompletionTokens  int    `gorm:"-"`
	TotalTokens       int    `gorm:"-"`
	ModelUsed         string `gorm:"-"`
	LLMCalls          int    `gorm:"-"`
	ErrorStack        *string `gorm:"-"`
	RetryCount        int     `gorm:"-"`
	ResultURL         *string         `gorm:"-"`
}

// TableName specifies the table name for GORM
func (Job) TableName() string {
	return "jobs"
}
