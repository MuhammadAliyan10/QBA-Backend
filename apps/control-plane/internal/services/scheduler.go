package services

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	"e2e-platform/apps/control-plane/internal/db"
	"e2e-platform/apps/control-plane/internal/models"

	"github.com/google/uuid"
	"github.com/robfig/cron/v3"
	"go.temporal.io/sdk/client"
)

// SchedulerService manages the cron schedules for workflows.
type SchedulerService struct {
	cron           *cron.Cron
	temporalClient client.Client
	entries        map[string]cron.EntryID // workflowID -> EntryID
}

// NewSchedulerService creates a new scheduler service.
func NewSchedulerService(tc client.Client) *SchedulerService {
	return &SchedulerService{
		cron:           cron.New(),
		temporalClient: tc,
		entries:        make(map[string]cron.EntryID),
	}
}

// Start initializes the scheduler with active workflow schedules from the database.
func (s *SchedulerService) Start() {
	var workflows []models.Workflow
	// Only load active workflows that have a cron schedule
	err := db.DB.Where("is_active = ? AND cron_schedule IS NOT NULL AND cron_schedule != ''", true).Find(&workflows).Error
	if err != nil {
		log.Printf("[Scheduler] Failed to load workflows: %v", err)
		return
	}

	for _, wf := range workflows {
		s.ScheduleWorkflow(wf)
	}

	s.cron.Start()
	log.Printf("[Scheduler] Started with %d active schedules", len(workflows))
}

// ScheduleWorkflow adds or updates a workflow schedule.
func (s *SchedulerService) ScheduleWorkflow(wf models.Workflow) {
	// Remove existing entry if any
	if entryID, ok := s.entries[wf.ID]; ok {
		s.cron.Remove(entryID)
		delete(s.entries, wf.ID)
	}

	if !wf.IsActive || wf.CronSchedule == nil || *wf.CronSchedule == "" {
		return
	}

	// Add the function to the cron scheduler
	entryID, err := s.cron.AddFunc(*wf.CronSchedule, func() {
		s.triggerWorkflow(wf.ID)
	})

	if err != nil {
		log.Printf("[Scheduler] Failed to schedule workflow %s (%s): %v", wf.ID, *wf.CronSchedule, err)
		return
	}

	s.entries[wf.ID] = entryID
	log.Printf("[Scheduler] Scheduled workflow %s | Cron: %s", wf.ID, *wf.CronSchedule)
}

// triggerWorkflow starts a Temporal execution for the given workflow.
func (s *SchedulerService) triggerWorkflow(workflowID string) {
	log.Printf("[Scheduler] Triggering scheduled run for workflow %s", workflowID)

	// 1. Fetch workflow details
	var workflow models.Workflow
	if err := db.DB.Where("id = ?", workflowID).First(&workflow).Error; err != nil {
		log.Printf("[Scheduler] Error fetching workflow %s: %v", workflowID, err)
		return
	}

	// Double check if it's still active
	if !workflow.IsActive {
		log.Printf("[Scheduler] Workflow %s is no longer active, skipping", workflowID)
		return
	}

	// INDUSTRIAL: Prevent overlapping runs
	// If a job for this workflow is already RUNNING, skip this scheduled run.
	var runningJob models.Job
	err := db.DB.Where("workflow_id = ? AND status = ?", workflowID, "RUNNING").First(&runningJob).Error
	if err == nil {
		log.Printf("[Scheduler] Workflow %s is already running (Job %s), skipping this trigger to avoid overlap", workflowID, runningJob.ID)
		return
	}

	// 2. Create Job record
	jobUUID := uuid.New().String()
	job := models.Job{
		ID:         jobUUID,
		UserID:     workflow.UserID,
		WorkflowID: workflowID,
		Status:     "QUEUED",
	}

	if err := db.DB.Create(&job).Error; err != nil {
		log.Printf("[Scheduler] Failed to create job record: %v", err)
		return
	}

	// 3. Start Temporal Workflow
	var recipeData map[string]interface{}
	if len(workflow.RecipeJSON) > 0 {
		_ = json.Unmarshal(workflow.RecipeJSON, &recipeData)
	}

	// Create a unique workflow ID for Temporal
	// We include the timestamp to prevent collisions if quickly rescheduled
	temporalWorkflowID := fmt.Sprintf("scheduled-%s-%d", workflowID, time.Now().Unix())

	workflowOptions := client.StartWorkflowOptions{
		ID:        temporalWorkflowID,
		TaskQueue: "e2e-browser-tasks",
	}

	payload := map[string]interface{}{
		"workflow_id": workflowID,
		"job_id":      jobUUID,
		"user_id":     workflow.UserID,
		"recipe":      recipeData,
		"trigger":     "SCHEDULED",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	_, err = s.temporalClient.ExecuteWorkflow(ctx, workflowOptions, "BrowserWorkflow", payload)
	if err != nil {
		log.Printf("[Scheduler] Failed to start Temporal workflow: %v", err)
		db.DB.Model(&job).Update("status", "FAILED")
		return
	}

	// Update status to RUNNING
	now := time.Now()
	db.DB.Model(&job).Updates(map[string]interface{}{
		"status":     "RUNNING",
		"started_at": &now,
	})

	// 4. Update stats on workflow
	db.DB.Model(&workflow).Update("run_count", workflow.RunCount+1)

	log.Printf("[Scheduler] Successfully triggered job %s", jobUUID)
}

// Stop stops the scheduler.
func (s *SchedulerService) Stop() {
	s.cron.Stop()
}
