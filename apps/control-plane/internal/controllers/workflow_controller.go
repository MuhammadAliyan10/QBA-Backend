package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"time"

	"e2e-platform/apps/control-plane/internal/authz"
	"e2e-platform/apps/control-plane/internal/db"
	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"go.temporal.io/sdk/client"
)

type WorkflowController struct {
	temporalClient client.Client
}

// NewWorkflowController creates a controller using a shared Temporal client.
// The Temporal client is created once in main.go and passed here.
func NewWorkflowController(tc client.Client) *WorkflowController {
	return &WorkflowController{temporalClient: tc}
}

// --- Request/Response Types ---

type ExecuteWorkflowRequest struct {
	WorkflowID string `json:"workflowId" binding:"required"`
}

// HandleExecute — Starts a workflow execution via Temporal.
// Route: POST /api/v1/workflows/:id/run (matches frontend's runWorkflow URL)
// Also supports POST /api/v1/workflow/execute (legacy compatibility)
func (ctrl *WorkflowController) HandleExecute(c *gin.Context) {
	// Support both URL patterns:
	// POST /api/v1/workflows/:id/run → workflowId from URL param
	// POST /api/v1/workflow/execute  → workflowId from JSON body
	workflowID := c.Param("id")

	if workflowID == "" {
		var req ExecuteWorkflowRequest
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "workflowId is required"})
			return
		}
		workflowID = req.WorkflowID
	}

	if ctrl.temporalClient == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "Temporal client not available"})
		return
	}

	callerID, ok := middleware.GetUserID(c)
	if !ok || callerID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Authentication required"})
		return
	}

	var workflow models.Workflow
	if err := db.DB.Where("id = ?", workflowID).First(&workflow).Error; err != nil {
		log.Printf("[Workflow] Workflow %s not found: %v", workflowID, err)
		c.JSON(http.StatusNotFound, gin.H{"error": "Workflow not found"})
		return
	}

	if workflow.UserID != callerID {
		c.JSON(http.StatusNotFound, gin.H{"error": "Workflow not found"})
		return
	}

	// Unmarshal recipe_json to pass through Temporal
	var recipeData map[string]interface{}
	if len(workflow.RecipeJSON) > 0 {
		if err := json.Unmarshal(workflow.RecipeJSON, &recipeData); err != nil {
			log.Printf("[Workflow] Failed to parse recipe_json for workflow %s: %v", workflowID, err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "Invalid workflow recipe data"})
			return
		}
	}

	// Generate proper UUID for the job
	jobUUID := uuid.New().String()

	// Create Job record using GORM model (matches Prisma schema)
	job := models.Job{
		ID:         jobUUID,
		UserID:     workflow.UserID,
		WorkflowID: workflowID,
		Status:     "QUEUED",
	}

	if err := db.DB.Create(&job).Error; err != nil {
		log.Printf("[Workflow] Failed to create job record: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create job record"})
		return
	}

	// Start the BrowserWorkflow via Temporal
	workflowOptions := client.StartWorkflowOptions{
		ID:        "workflow-" + jobUUID,
		TaskQueue: "e2e-browser-tasks",
	}

	// Pass the full recipe graph so the Python worker can execute it
	payload := map[string]interface{}{
		"workflow_id": workflowID,
		"job_id":      jobUUID,
		"user_id":     workflow.UserID,
		"recipe":      recipeData, // Full {nodes, edges, viewport} from the editor
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	run, err := ctrl.temporalClient.ExecuteWorkflow(
		ctx,
		workflowOptions,
		"BrowserWorkflow",
		payload,
	)
	if err != nil {
		log.Printf("[Workflow] Failed to start execution: %v", err)
		// Mark job as FAILED in DB
		db.DB.Model(&job).Update("status", "FAILED")
		c.JSON(http.StatusInternalServerError, gin.H{"error": fmt.Sprintf("Failed to start workflow: %v", err)})
		return
	}

	// Update job status to RUNNING
	now := time.Now()
	db.DB.Model(&job).Updates(map[string]interface{}{
		"status":     "RUNNING",
		"started_at": &now,
	})

	log.Printf("[Workflow] Started execution %s for workflow %s", jobUUID, workflowID)

	c.JSON(http.StatusOK, gin.H{
		"success": true,
		"jobId":   jobUUID,
		"status":  "RUNNING",
		"runId":   run.GetRunID(),
	})
}

// HandleListJobs lists jobs for the authenticated user only.
// Route: GET /v1/jobs?workflow_id=optional
func (ctrl *WorkflowController) HandleListJobs(c *gin.Context) {
	callerID, ok := middleware.GetUserID(c)
	if !ok || callerID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Authentication required"})
		return
	}

	query := db.DB.Model(&models.Job{}).Where("user_id = ?", callerID).Order("created_at DESC").Limit(50)

	if workflowID := c.Query("workflow_id"); workflowID != "" {
		query = query.Where("workflow_id = ?", workflowID)
	}

	var jobs []models.Job
	if err := query.Find(&jobs).Error; err != nil {
		log.Printf("[Workflow] Failed to list jobs: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to list jobs"})
		return
	}

	// Transform to frontend-compatible format
	var response []map[string]interface{}
	for _, job := range jobs {
		var duration float64
		if job.CompletedAt != nil && job.StartedAt != nil {
			duration = job.CompletedAt.Sub(*job.StartedAt).Seconds()
		}

		item := map[string]interface{}{
			"id":           job.ID,
			"name":         job.WorkflowID,
			"workflowName": job.WorkflowID,
			"status":       job.Status,
			"startTime":    nil,
			"endTime":      nil,
			"duration":     duration,
			"totalCost":    job.CreditsUsed,
			"trigger":      "user",
			"error":        job.ErrorMessage,
		}

		if job.StartedAt != nil {
			item["startTime"] = job.StartedAt.UnixMilli()
		} else {
			item["startTime"] = job.CreatedAt.UnixMilli()
		}
		if job.CompletedAt != nil {
			item["endTime"] = job.CompletedAt.UnixMilli()
		}

		response = append(response, item)
	}

	c.JSON(http.StatusOK, gin.H{"data": response})
}

// HandleGetJob — Get details of a single job.
// Route: GET /v1/jobs/:id
func (ctrl *WorkflowController) HandleGetJob(c *gin.Context) {
	jobID := c.Param("id")
	if jobID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "job ID is required"})
		return
	}

	callerID, ok := middleware.GetUserID(c)
	if !ok || callerID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Authentication required"})
		return
	}

	job, err := authz.LoadJobForUser(db.DB, jobID, callerID)
	if err != nil {
		if errors.Is(err, authz.ErrNotFound) {
			c.JSON(http.StatusNotFound, gin.H{"error": "Job not found"})
			return
		}
		log.Printf("[Jobs] Job lookup failed: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to load job"})
		return
	}

	// Build response
	var duration float64
	if job.CompletedAt != nil && job.StartedAt != nil {
		duration = job.CompletedAt.Sub(*job.StartedAt).Seconds()
	}

	response := gin.H{
		"id":           job.ID,
		"workflowId":   job.WorkflowID,
		"userId":       job.UserID,
		"status":       job.Status,
		"startTime":    nil,
		"endTime":      nil,
		"duration":     duration,
		"creditsUsed":  job.CreditsUsed,
		"currentStep":  job.CurrentStep,
		"errorMessage": job.ErrorMessage,
		"retryCount":   job.RetryCount,
		"resultUrl":    job.ResultURL,
		"createdAt":    job.CreatedAt.UnixMilli(),
	}

	if job.StartedAt != nil {
		response["startTime"] = job.StartedAt.UnixMilli()
	}
	if job.CompletedAt != nil {
		response["endTime"] = job.CompletedAt.UnixMilli()
	}

	c.JSON(http.StatusOK, response)
}

// HandleCancelJob — Cancel a running workflow via Temporal.
// Route: POST /v1/jobs/:id/cancel
func (ctrl *WorkflowController) HandleCancelJob(c *gin.Context) {
	jobID := c.Param("id")
	if jobID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "job ID is required"})
		return
	}

	if ctrl.temporalClient == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "Temporal client not available"})
		return
	}

	callerID, ok := middleware.GetUserID(c)
	if !ok || callerID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Authentication required"})
		return
	}

	job, err := authz.LoadJobForUser(db.DB, jobID, callerID)
	if err != nil {
		if errors.Is(err, authz.ErrNotFound) {
			c.JSON(http.StatusNotFound, gin.H{"error": "Job not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to load job"})
		return
	}

	if job.Status != "RUNNING" && job.Status != "QUEUED" {
		c.JSON(http.StatusConflict, gin.H{
			"error":  "Job is not in a cancellable state",
			"status": job.Status,
		})
		return
	}

	// Cancel the Temporal workflow
	temporalWorkflowID := "workflow-" + jobID
	err = ctrl.temporalClient.CancelWorkflow(
		context.Background(),
		temporalWorkflowID,
		"", // empty run ID = latest run
	)
	if err != nil {
		log.Printf("[Jobs] Failed to cancel Temporal workflow %s: %v", temporalWorkflowID, err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": fmt.Sprintf("Failed to cancel workflow: %v", err)})
		return
	}

	// Update job status in DB
	now := time.Now()
	db.DB.Model(&job).Updates(map[string]interface{}{
		"status":       "CANCELLED",
		"completed_at": &now,
	})

	log.Printf("[Jobs] Cancelled job %s (Temporal workflow: %s)", jobID, temporalWorkflowID)

	c.JSON(http.StatusOK, gin.H{
		"success": true,
		"jobId":   jobID,
		"status":  "CANCELLED",
	})
}

// HandleGetJobLogs — Get execution logs for a job.
// Route: GET /v1/jobs/:id/logs?limit=100&since=timestamp
func (ctrl *WorkflowController) HandleGetJobLogs(c *gin.Context) {
	jobID := c.Param("id")
	if jobID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "job ID is required"})
		return
	}

	callerID, ok := middleware.GetUserID(c)
	if !ok || callerID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Authentication required"})
		return
	}

	if _, err := authz.LoadJobForUser(db.DB, jobID, callerID); err != nil {
		if errors.Is(err, authz.ErrNotFound) {
			c.JSON(http.StatusNotFound, gin.H{"error": "Job not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to load job"})
		return
	}

	// Build log query
	query := db.DB.Model(&models.JobLog{}).
		Where("job_id = ?", jobID).
		Order("timestamp ASC")

	// Optional: limit
	limit := 100
	if l := c.Query("limit"); l != "" {
		if _, err := fmt.Sscanf(l, "%d", &limit); err == nil && limit > 0 {
			if limit > 1000 {
				limit = 1000
			}
		}
	}
	query = query.Limit(limit)

	// Optional: since timestamp (milliseconds)
	if since := c.Query("since"); since != "" {
		var sinceMs int64
		if _, err := fmt.Sscanf(since, "%d", &sinceMs); err == nil {
			sinceTime := time.UnixMilli(sinceMs)
			query = query.Where("timestamp > ?", sinceTime)
		}
	}

	var logs []models.JobLog
	if err := query.Find(&logs).Error; err != nil {
		log.Printf("[Jobs] Failed to query logs for job %s: %v", jobID, err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to query logs"})
		return
	}

	// Transform to frontend-compatible format
	var response []map[string]interface{}
	for _, entry := range logs {
		item := map[string]interface{}{
			"id":        entry.ID,
			"level":     entry.Level,
			"message":   entry.Message,
			"nodeId":    entry.NodeID,
			"stepIndex": entry.StepIndex,
			"timestamp": entry.Timestamp.UnixMilli(),
		}
		if entry.DurationMs != nil {
			item["durationMs"] = *entry.DurationMs
		}
		response = append(response, item)
	}

	c.JSON(http.StatusOK, gin.H{
		"logs":  response,
		"count": len(response),
	})
}

// HandleResumeJob — Resume a paused workflow via Temporal signal.
// Route: POST /v1/jobs/:id/resume
// Used for human-in-the-loop flows (CAPTCHA, 2FA, manual approval).
func (ctrl *WorkflowController) HandleResumeJob(c *gin.Context) {
	jobID := c.Param("id")
	if jobID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "job ID is required"})
		return
	}

	if ctrl.temporalClient == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "Temporal client not available"})
		return
	}

	// Parse the user input data
	var body struct {
		Data map[string]interface{} `json:"data" binding:"required"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Request body must contain 'data' field"})
		return
	}

	callerID, ok := middleware.GetUserID(c)
	if !ok || callerID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Authentication required"})
		return
	}

	job, err := authz.LoadJobForUser(db.DB, jobID, callerID)
	if err != nil {
		if errors.Is(err, authz.ErrNotFound) {
			c.JSON(http.StatusNotFound, gin.H{"error": "Job not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to load job"})
		return
	}

	if job.Status != "PAUSED" && job.Status != "RUNNING" {
		c.JSON(http.StatusConflict, gin.H{
			"error":  "Job is not in a resumable state",
			"status": job.Status,
		})
		return
	}

	// Send signal to Temporal workflow (matches BrowserWorkflow's signal handler)
	temporalWorkflowID := "workflow-" + jobID
	err = ctrl.temporalClient.SignalWorkflow(
		context.Background(),
		temporalWorkflowID,
		"",                  // empty run ID = latest run
		"USER_INTERACTION",  // must match @workflow.signal(name="USER_INTERACTION")
		body.Data,
	)
	if err != nil {
		log.Printf("[Jobs] Failed to signal Temporal workflow %s: %v", temporalWorkflowID, err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": fmt.Sprintf("Failed to resume workflow: %v", err)})
		return
	}

	// Update job status
	db.DB.Model(&job).Update("status", "RUNNING")

	log.Printf("[Jobs] Resumed job %s with user input", jobID)

	c.JSON(http.StatusOK, gin.H{
		"success": true,
		"jobId":   jobID,
		"status":  "RUNNING",
	})
}
