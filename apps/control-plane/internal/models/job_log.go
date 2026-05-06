package models

import (
	"time"

	"gorm.io/datatypes"
)

// JobLog represents a single log entry for a job execution.
// Mapped from Prisma: model JobLog
type JobLog struct {
	ID    string `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()"`
	JobID string `gorm:"column:job_id;type:uuid;index"`
	Level string `gorm:"column:level;type:log_level;default:INFO;index"`

	// Log content
	Message   string  `gorm:"column:message"`
	NodeID    *string `gorm:"column:node_id"`
	StepIndex *int    `gorm:"column:step_index"`

	// Performance data
	DurationMs *int `gorm:"column:duration_ms"`

	// Additional context
	Metadata *datatypes.JSON `gorm:"column:metadata;type:jsonb"`

	Timestamp time.Time `gorm:"column:timestamp;index;default:now()"`
}

// TableName specifies the table name for GORM
func (JobLog) TableName() string {
	return "job_logs"
}
