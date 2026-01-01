package models

import (
	"time"

	"gorm.io/datatypes"
	"gorm.io/gorm"
)

// Workflow represents a saved automation workflow
// Mapped from Prisma: model Workflow
type Workflow struct {
	ID          string `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()"`
	UserID      string `gorm:"column:user_id;type:uuid;index"`
	Name        string
	Description *string

	// Trigger configuration
	TriggerType  string  `gorm:"column:trigger_type;type:trigger_type;index"`
	CronSchedule *string `gorm:"column:cron_schedule"`

	// The DAG structure (nodes, edges, step configs)
	// This is the core recipe that defines what the bot does
	RecipeJSON datatypes.JSON `gorm:"column:recipe_json;type:jsonb"`

	// State
	IsActive bool `gorm:"column:is_active;index"`

	// Stats
	LastRunAt *time.Time `gorm:"column:last_run_at"`
	RunCount  int        `gorm:"column:run_count;default:0"`

	CreatedAt time.Time
	UpdatedAt time.Time
	DeletedAt gorm.DeletedAt `gorm:"index"`
}

// TableName specifies the table name for GORM
func (Workflow) TableName() string {
	return "workflows"
}
