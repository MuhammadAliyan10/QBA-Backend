package models

import (
	"time"

	"gorm.io/datatypes"
)

// Job represents a single execution of a workflow
// Mapped from Prisma: model Job
type Job struct {
	ID         string `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()"`
	UserID     string `gorm:"column:user_id;type:uuid;index"`
	WorkflowID string `gorm:"column:workflow_id;type:uuid;index"`
	Status     string `gorm:"column:status;index"`

	// Timing
	ScheduledAt *time.Time `gorm:"column:scheduled_at;index"`
	StartedAt   *time.Time `gorm:"column:started_at"`
	CompletedAt *time.Time `gorm:"column:completed_at"`
	DurationMs  *int       `gorm:"column:duration_ms"`

	// Checkpointing for resumability
	CurrentStep  *int            `gorm:"column:current_step"`
	CurrentState *datatypes.JSON `gorm:"column:current_state;type:jsonb"`

	// Cost tracking
	CreditsUsed int `gorm:"column:credits_used;default:0"`

	// Token telemetry
	PromptTokens     int    `gorm:"column:prompt_tokens;default:0"`
	CompletionTokens int    `gorm:"column:completion_tokens;default:0"`
	TotalTokens      int    `gorm:"column:total_tokens;default:0"`
	ModelUsed        string `gorm:"column:model_used;default:''"`
	LLMCalls         int    `gorm:"column:llm_calls;default:0"`

	// Error handling
	ErrorMessage *string `gorm:"column:error_message"`
	ErrorStack   *string `gorm:"column:error_stack"`
	RetryCount   int     `gorm:"column:retry_count;default:0"`

	// Result
	ResultJSON *datatypes.JSON `gorm:"column:result_json;type:jsonb"`
	ResultURL  *string         `gorm:"column:result_url"`

	CreatedAt time.Time `gorm:"index"`
	UpdatedAt time.Time
}

// TableName specifies the table name for GORM
func (Job) TableName() string {
	return "jobs"
}
