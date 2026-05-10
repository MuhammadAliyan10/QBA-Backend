// internal/controllers/workflow_controller.go
package controllers

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"go.temporal.io/sdk/client"
	"gorm.io/gorm"
)

type WorkflowController struct {
	db             *gorm.DB
	temporalClient client.Client
	identity       *services.IdentityService
}

func NewWorkflowController(db *gorm.DB, tc client.Client, identity *services.IdentityService) *WorkflowController {
	return &WorkflowController{
		db:             db,
		temporalClient: tc,
		identity:       identity,
	}
}

// ─── DTOS ───────────────────────────────────────────────────────────────────

type CreateWorkflowRequest struct {
	Name        string                 `json:"name" binding:"required"`
	Description *string                `json:"description"`
	RecipeJSON  map[string]interface{} `json:"recipe_json" binding:"required"`
}

type UpdateWorkflowRequest struct {
	Name        *string                 `json:"name"`
	Description *string                 `json:"description"`
	RecipeJSON  *map[string]interface{} `json:"recipe_json"`
	IsActive    *bool                   `json:"is_active"`
}

type WorkflowResponse struct {
	ID          string                 `json:"id"`
	Name        string                 `json:"name"`
	Description *string                `json:"description"`
	TriggerType string                 `json:"trigger_type"`
	RecipeJSON  map[string]interface{} `json:"recipe_json"`
	IsActive    bool                   `json:"is_active"`
	LastRunAt   *string                `json:"last_run_at"`
	RunCount    int                    `json:"run_count"`
	CreatedAt   string                 `json:"created_at"`
	UpdatedAt   string                 `json:"updated_at"`
}

// ─── WORKFLOW CRUD ──────────────────────────────────────────────────────────

func (c *WorkflowController) HandleListWorkflows(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	var workflows []models.Workflow
	if err := c.db.Where("user_id = ?", tenantID).Order("created_at DESC").Find(&workflows).Error; err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch workflows"})
		return
	}

	var response []WorkflowResponse
	for _, w := range workflows {
		var lastRun string
		if w.LastRunAt != nil {
			lastRun = w.LastRunAt.Format("2006-01-02T15:04:05Z07:00")
		}

		var recipe map[string]interface{}
		if len(w.RecipeJSON) > 0 {
			json.Unmarshal(w.RecipeJSON, &recipe)
		}

		response = append(response, WorkflowResponse{
			ID:          w.ID,
			Name:        w.Name,
			Description: w.Description,
			TriggerType: w.TriggerType,
			RecipeJSON:  recipe,
			IsActive:    w.IsActive,
			LastRunAt:   &lastRun,
			RunCount:    w.RunCount,
			CreatedAt:   w.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
			UpdatedAt:   w.UpdatedAt.Format("2006-01-02T15:04:05Z07:00"),
		})
	}

	if response == nil {
		response = make([]WorkflowResponse, 0)
	}

	ctx.JSON(http.StatusOK, gin.H{"data": response})
}

func (c *WorkflowController) HandleGetWorkflow(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	id := ctx.Param("id")

	var w models.Workflow
	if err := c.db.Where("id = ? AND user_id = ?", id, tenantID).First(&w).Error; err != nil {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Workflow not found"})
		return
	}

	var recipe map[string]interface{}
	if len(w.RecipeJSON) > 0 {
		json.Unmarshal(w.RecipeJSON, &recipe)
	}

	var lastRun string
	if w.LastRunAt != nil {
		lastRun = w.LastRunAt.Format("2006-01-02T15:04:05Z07:00")
	}

	ctx.JSON(http.StatusOK, gin.H{"data": WorkflowResponse{
		ID:          w.ID,
		Name:        w.Name,
		Description: w.Description,
		TriggerType: w.TriggerType,
		RecipeJSON:  recipe,
		IsActive:    w.IsActive,
		LastRunAt:   &lastRun,
		RunCount:    w.RunCount,
		CreatedAt:   w.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
		UpdatedAt:   w.UpdatedAt.Format("2006-01-02T15:04:05Z07:00"),
	}})
}

func (c *WorkflowController) HandleCreateWorkflow(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	var req CreateWorkflowRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request payload"})
		return
	}

	recipeBytes, _ := json.Marshal(req.RecipeJSON)

	w := models.Workflow{
		UserID:      tenantID,
		Name:        req.Name,
		Description: req.Description,
		TriggerType: "ON_DEMAND",
		RecipeJSON:  recipeBytes,
		IsActive:    true,
	}

	if err := c.db.Create(&w).Error; err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create workflow"})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{"data": map[string]string{"id": w.ID}})
}

func (c *WorkflowController) HandleUpdateWorkflow(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	id := ctx.Param("id")
	var req UpdateWorkflowRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request payload"})
		return
	}

	updates := make(map[string]interface{})
	if req.Name != nil {
		updates["name"] = *req.Name
	}
	if req.Description != nil {
		updates["description"] = *req.Description
	}
	if req.IsActive != nil {
		updates["is_active"] = *req.IsActive
	}
	if req.RecipeJSON != nil {
		recipeBytes, _ := json.Marshal(*req.RecipeJSON)
		updates["recipe_json"] = recipeBytes
	}

	if len(updates) == 0 {
		ctx.JSON(http.StatusOK, gin.H{"status": "no changes"})
		return
	}

	res := c.db.Model(&models.Workflow{}).Where("id = ? AND user_id = ?", id, tenantID).Updates(updates)
	if res.Error != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to update workflow"})
		return
	}
	if res.RowsAffected == 0 {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Workflow not found"})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{"status": "updated"})
}

func (c *WorkflowController) HandleDeleteWorkflow(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	id := ctx.Param("id")

	res := c.db.Where("id = ? AND user_id = ?", id, tenantID).Delete(&models.Workflow{})
	if res.Error != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to delete workflow"})
		return
	}
	if res.RowsAffected == 0 {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Workflow not found"})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{"status": "deleted"})
}

// ─── EXECUTION HANDLERS ─────────────────────────────────────────────────────

type ExecuteWorkflowRequest struct {
	WorkflowID string `json:"workflowId"`
}

func (c *WorkflowController) HandleExecute(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	workflowID := ctx.Param("id")
	if workflowID == "" {
		var req ExecuteWorkflowRequest
		if err := ctx.ShouldBindJSON(&req); err != nil {
			ctx.JSON(http.StatusBadRequest, gin.H{"error": "workflowId is required"})
			return
		}
		workflowID = req.WorkflowID
	}

	if c.temporalClient == nil {
		ctx.JSON(http.StatusServiceUnavailable, gin.H{"error": "Temporal client not available"})
		return
	}

	var workflow models.Workflow
	if err := c.db.Where("id = ? AND user_id = ?", workflowID, tenantID).First(&workflow).Error; err != nil {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Workflow not found"})
		return
	}

	var recipeData map[string]interface{}
	if len(workflow.RecipeJSON) > 0 {
		json.Unmarshal(workflow.RecipeJSON, &recipeData)
	}

	jobUUID := uuid.New().String()
	job := models.Job{
		ID:         jobUUID,
		UserID:     tenantID,
		WorkflowID: workflowID,
		Status:     "QUEUED",
	}

	if err := c.db.Create(&job).Error; err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create job record"})
		return
	}

	workflowOptions := client.StartWorkflowOptions{
		ID:        "workflow-" + jobUUID,
		TaskQueue: "e2e-browser-tasks",
	}

	payload := map[string]interface{}{
		"workflow_id": workflowID,
		"job_id":      jobUUID,
		"user_id":     tenantID,
		"recipe":      recipeData,
	}

	tCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	run, err := c.temporalClient.ExecuteWorkflow(tCtx, workflowOptions, "BrowserWorkflow", payload)
	if err != nil {
		c.db.Model(&job).Update("status", "FAILED")
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to start Temporal workflow"})
		return
	}

	now := time.Now()
	c.db.Model(&job).Updates(map[string]interface{}{
		"status":     "RUNNING",
		"started_at": &now,
	})
	c.db.Model(&workflow).Updates(map[string]interface{}{
		"run_count":   gorm.Expr("run_count + 1"),
		"last_run_at": &now,
	})

	ctx.JSON(http.StatusOK, gin.H{
		"success": true,
		"jobId":   jobUUID,
		"status":  "RUNNING",
		"runId":   run.GetRunID(),
	})
}

// ─── JOB HANDLERS ───────────────────────────────────────────────────────────

func (c *WorkflowController) HandleListJobs(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	query := c.db.Where("user_id = ?", tenantID).Order("created_at DESC").Limit(50)
	if wID := ctx.Query("workflow_id"); wID != "" {
		query = query.Where("workflow_id = ?", wID)
	}

	var jobs []models.Job
	if err := query.Find(&jobs).Error; err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to list jobs"})
		return
	}

	var response []map[string]interface{}
	for _, j := range jobs {
		var duration float64
		if j.CompletedAt != nil && j.StartedAt != nil {
			duration = j.CompletedAt.Sub(*j.StartedAt).Seconds()
		}
		item := map[string]interface{}{
			"id":           j.ID,
			"name":         j.WorkflowID,
			"workflowName": j.WorkflowID,
			"status":       j.Status,
			"duration":     duration,
			"totalCost":    j.CreditsUsed,
			"error":        j.ErrorMessage,
		}
		if j.StartedAt != nil {
			item["startTime"] = j.StartedAt.UnixMilli()
		} else {
			item["startTime"] = j.CreatedAt.UnixMilli()
		}
		if j.CompletedAt != nil {
			item["endTime"] = j.CompletedAt.UnixMilli()
		}
		response = append(response, item)
	}
	if response == nil {
		response = make([]map[string]interface{}, 0)
	}
	ctx.JSON(http.StatusOK, gin.H{"data": response})
}

func (c *WorkflowController) HandleGetJob(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	jobID := ctx.Param("id")
	var job models.Job
	if err := c.db.Where("id = ? AND user_id = ?", jobID, tenantID).First(&job).Error; err != nil {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Job not found"})
		return
	}

	var duration float64
	if job.CompletedAt != nil && job.StartedAt != nil {
		duration = job.CompletedAt.Sub(*job.StartedAt).Seconds()
	}

	resp := gin.H{
		"id":           job.ID,
		"workflowId":   job.WorkflowID,
		"status":       job.Status,
		"duration":     duration,
		"creditsUsed":  job.CreditsUsed,
		"errorMessage": job.ErrorMessage,
		"resultUrl":    job.ResultURL,
		"createdAt":    job.CreatedAt.UnixMilli(),
	}
	if job.StartedAt != nil {
		resp["startTime"] = job.StartedAt.UnixMilli()
	}
	if job.CompletedAt != nil {
		resp["endTime"] = job.CompletedAt.UnixMilli()
	}

	ctx.JSON(http.StatusOK, resp)
}

func (c *WorkflowController) HandleGetJobLogs(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	jobID := ctx.Param("id")
	// Verify job ownership
	var count int64
	c.db.Model(&models.Job{}).Where("id = ? AND user_id = ?", jobID, tenantID).Count(&count)
	if count == 0 {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Job not found"})
		return
	}

	var logs []models.JobLog
	c.db.Where("job_id = ?", jobID).Order("timestamp ASC").Limit(100).Find(&logs)

	var response []map[string]interface{}
	for _, l := range logs {
		item := map[string]interface{}{
			"id":        l.ID,
			"level":     l.Level,
			"message":   l.Message,
			"nodeId":    l.NodeID,
			"stepIndex": l.StepIndex,
			"timestamp": l.Timestamp.UnixMilli(),
		}
		response = append(response, item)
	}
	if response == nil {
		response = make([]map[string]interface{}, 0)
	}

	ctx.JSON(http.StatusOK, gin.H{"data": response})
}

func (c *WorkflowController) HandleCancelJob(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	jobID := ctx.Param("id")
	var job models.Job
	if err := c.db.Where("id = ? AND user_id = ?", jobID, tenantID).First(&job).Error; err != nil {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Job not found"})
		return
	}

	if job.Status != "RUNNING" && job.Status != "QUEUED" {
		ctx.JSON(http.StatusConflict, gin.H{"error": "Job is not in a cancellable state"})
		return
	}

	if c.temporalClient != nil {
		c.temporalClient.CancelWorkflow(context.Background(), "workflow-"+jobID, "")
	}

	now := time.Now()
	c.db.Model(&job).Updates(map[string]interface{}{
		"status":       "CANCELLED",
		"completed_at": &now,
	})

	ctx.JSON(http.StatusOK, gin.H{"status": "CANCELLED"})
}

func (c *WorkflowController) HandleResumeJob(ctx *gin.Context) {
	clerkID, exists := middleware.GetUserID(ctx)
	if !exists {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Unauthorized"})
		return
	}
	tenantID, err := c.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		ctx.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	jobID := ctx.Param("id")
	var job models.Job
	if err := c.db.Where("id = ? AND user_id = ?", jobID, tenantID).First(&job).Error; err != nil {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Job not found"})
		return
	}

	if job.Status != "PAUSED" && job.Status != "RUNNING" {
		ctx.JSON(http.StatusConflict, gin.H{"error": "Job is not in a resumable state"})
		return
	}

	var body struct {
		Data map[string]interface{} `json:"data" binding:"required"`
	}
	if err := ctx.ShouldBindJSON(&body); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Request body must contain 'data'"})
		return
	}

	if c.temporalClient != nil {
		c.temporalClient.SignalWorkflow(context.Background(), "workflow-"+jobID, "", "USER_INTERACTION", body.Data)
	}

	c.db.Model(&job).Update("status", "RUNNING")

	ctx.JSON(http.StatusOK, gin.H{"success": true, "jobId": jobID, "status": "RUNNING"})
}
