package controllers

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"
	"e2e-platform/apps/control-plane/internal/temporal"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/gorm"
)

// ─── REQUEST / RESPONSE ─────────────────────────────────────────────────────

// SightedRequest is the JSON body for POST /v1/sighted.
type SightedRequest struct {
	TargetURL string                 `json:"target_url" binding:"required"`
	Objective string                 `json:"objective"  binding:"required"`
	Config    map[string]interface{} `json:"config,omitempty"`
}

// SightedAsyncResponse is the HTTP 202 response for the async endpoint.
type SightedAsyncResponse struct {
	JobID  string `json:"job_id"`
	RunID  string `json:"run_id"`
	Status string `json:"status"`
}

// SightedSyncResponse is the complete result for the sync endpoint.
type SightedSyncResponse struct {
	Success           bool                   `json:"success"`
	JobID             string                 `json:"job_id"`
	Status            string                 `json:"status"`
	RejectionReason   string                 `json:"rejection_reason,omitempty"`
	GoalsPlanned      int                    `json:"goals_planned"`
	GoalsCompleted    int                    `json:"goals_completed"`
	FeasibilityMap    map[string]bool        `json:"feasibility_map,omitempty"`
	ExtractedData     map[string]interface{} `json:"extracted_data,omitempty"`
	HarvestDurationMs int                    `json:"harvest_duration_ms"`
	PlanDurationMs    int                    `json:"planning_duration_ms"`
	ExecDurationMs    int                    `json:"execution_duration_ms"`
	TotalDurationMs   int                    `json:"total_duration_ms"`
	Error             string                 `json:"error,omitempty"`
}

// ─── CONTROLLER ──────────────────────────────────────────────────────────────

// SightedController handles the sighted pipeline API.
type SightedController struct {
	tm       *temporal.TemporalManager
	db       *gorm.DB
	identity *services.IdentityService
}

// NewSightedController creates a controller backed by the shared TemporalManager.
func NewSightedController(db *gorm.DB, tm *temporal.TemporalManager, identity *services.IdentityService) *SightedController {
	return &SightedController{db: db, tm: tm, identity: identity}
}

// HandleSightedAsync handles POST /v1/sighted.
// Returns HTTP 202 with a job_id. The client polls /v1/jobs/:id or subscribes
// to the SSE stream at /v1/execute/:job_id/stream for real-time updates.
func (sc *SightedController) HandleSightedAsync(c *gin.Context) {
	clerkID, exists := middleware.GetUserID(c)
	if !exists || clerkID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{
			"error":   "unauthenticated",
			"message": "Authentication required",
		})
		return
	}

	tenantID, err := sc.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	var req SightedRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "invalid_request",
			"message": "Missing required fields: target_url, objective",
			"details": err.Error(),
		})
		return
	}

	if err := validateSightedRequest(&req); err != nil {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"error":   "validation_failed",
			"message": err.Error(),
		})
		return
	}

	jobID := strings.TrimSpace(c.GetHeader("X-Idempotency-Key"))
	if jobID == "" {
		jobID = uuid.New().String()
	}

	// Idempotent retry: job already exists
	var existingJob models.Job
	if err := sc.db.Where("id = ?", jobID).First(&existingJob).Error; err == nil {
		if existingJob.UserID != tenantID {
			c.JSON(http.StatusForbidden, gin.H{
				"error":   "forbidden",
				"message": "Idempotency key belongs to another user",
			})
			return
		}
		runID, terr := sc.tm.GetExistingRunID(c.Request.Context(), jobID)
		if terr != nil {
			c.JSON(http.StatusConflict, gin.H{
				"error":   "duplicate_execution",
				"message": "Job exists but workflow state could not be loaded",
				"job_id":  jobID,
			})
			return
		}
		c.JSON(http.StatusOK, SightedAsyncResponse{
			JobID:  jobID,
			RunID:  runID,
			Status: "already_running",
		})
		return
	}

	// Create parent workflow record
	workflowID := uuid.New().String()
	workflow := &models.Workflow{
		ID:          workflowID,
		UserID:      tenantID,
		Name:        "Sighted: " + req.TargetURL,
		TriggerType: "ON_DEMAND",
		RecipeJSON:  []byte("{}"),
		IsActive:    true,
	}
	if err := sc.db.Create(workflow).Error; err != nil {
		log.Printf("[SightedController] Failed to create workflow: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "workflow_creation_failed",
			"message": "Failed to create workflow record",
		})
		return
	}

	// Create job record
	job := &models.Job{
		ID:         jobID,
		UserID:     tenantID,
		WorkflowID: workflowID,
		Status:     "QUEUED",
	}
	if err := sc.db.Create(job).Error; err != nil {
		log.Printf("[SightedController] Failed to create job: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "database_error",
			"message": "Failed to persist job record",
		})
		return
	}

	// Merge engine settings to force sighted mode
	engineSettings := req.Config
	if engineSettings == nil {
		engineSettings = make(map[string]interface{})
	}
	engineSettings["engine_mode"] = "sighted"

	// Dispatch to Temporal
	runID, err := sc.tm.StartExecution(
		c.Request.Context(),
		jobID,
		workflowID,
		req.TargetURL,
		req.Objective,
		engineSettings,
		nil, // sessionState: sighted pipeline does not use BYOS sessions
		nil, // attachments
	)
	if err != nil {
		if isWorkflowAlreadyStarted(err) {
			existingRunID, descErr := sc.tm.GetExistingRunID(c.Request.Context(), jobID)
			if descErr != nil {
				c.JSON(http.StatusConflict, gin.H{
					"error":   "duplicate_execution",
					"message": "Workflow already exists for this idempotency key",
					"job_id":  jobID,
				})
				return
			}
			c.JSON(http.StatusOK, SightedAsyncResponse{
				JobID:  jobID,
				RunID:  existingRunID,
				Status: "already_running",
			})
			return
		}

		log.Printf("[SightedController] Temporal error: %v", err)
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"error":   "orchestrator_unavailable",
			"message": "Failed to queue automation job. Retry later.",
		})
		return
	}

	log.Printf("[SightedController] Job queued | JobID=%s | RunID=%s", jobID, runID)

	c.JSON(http.StatusAccepted, SightedAsyncResponse{
		JobID:  jobID,
		RunID:  runID,
		Status: "queued",
	})
}

// HandleSightedSync handles POST /v1/sighted/sync.
// Executes the sighted pipeline synchronously and returns the result directly.
// Useful for testing, small tasks, and real-time API consumers.
func (sc *SightedController) HandleSightedSync(c *gin.Context) {
	clerkID, exists := middleware.GetUserID(c)
	if !exists || clerkID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{
			"error":   "unauthenticated",
			"message": "Authentication required",
		})
		return
	}

	tenantID, err := sc.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	var req SightedRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "invalid_request",
			"message": "Missing required fields: target_url, objective",
			"details": err.Error(),
		})
		return
	}

	if err := validateSightedRequest(&req); err != nil {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"error":   "validation_failed",
			"message": err.Error(),
		})
		return
	}

	jobID := uuid.New().String()

	// Create job record
	workflowID := uuid.New().String()
	now := time.Now()
	job := &models.Job{
		ID:         jobID,
		UserID:     tenantID,
		WorkflowID: workflowID,
		Status:     "RUNNING",
		StartedAt:  &now,
	}
	if err := sc.db.Create(job).Error; err != nil {
		log.Printf("[SightedController] Failed to create job: %v", err)
	}

	// Dispatch synchronously with timeout
	engineSettings := req.Config
	if engineSettings == nil {
		engineSettings = make(map[string]interface{})
	}
	engineSettings["engine_mode"] = "sighted"

	ctx, cancel := context.WithTimeout(c.Request.Context(), 2*time.Minute)
	defer cancel()

	runID, err := sc.tm.StartExecution(ctx, jobID, workflowID, req.TargetURL, req.Objective, engineSettings, nil, nil)
	if err != nil {
		log.Printf("[SightedController] Sync execution failed to start: %v", err)
		sc.updateJobStatus(jobID, "FAILED", err.Error())
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"error":   "orchestrator_unavailable",
			"message": "Failed to start automation. Retry later.",
		})
		return
	}

	// Wait for workflow completion
	workflowRun := sc.tm.Client().GetWorkflow(ctx, jobID, runID)
	var rawResult json.RawMessage
	err = workflowRun.Get(ctx, &rawResult)

	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			sc.updateJobStatus(jobID, "TIMEOUT", "Execution timed out (120s)")
			c.JSON(http.StatusGatewayTimeout, gin.H{
				"error":   "execution_timeout",
				"message": "Automation did not complete within the allowed time window.",
				"job_id":  jobID,
			})
			return
		}
		sc.updateJobStatus(jobID, "FAILED", err.Error())
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "execution_failed",
			"message": err.Error(),
			"job_id":  jobID,
		})
		return
	}

	// Parse result and return
	var result map[string]interface{}
	if err := json.Unmarshal(rawResult, &result); err != nil {
		result = map[string]interface{}{"raw": string(rawResult)}
	}

	completedAt := time.Now()
	durationMs := int(completedAt.Sub(now).Milliseconds())
	sc.updateJobCompleted(jobID, &completedAt, &durationMs, rawResult)

	c.JSON(http.StatusOK, result)
}

// ─── HELPERS ─────────────────────────────────────────────────────────────────

func validateSightedRequest(req *SightedRequest) error {
	req.TargetURL = strings.TrimSpace(req.TargetURL)
	req.Objective = strings.TrimSpace(req.Objective)

	if len(req.TargetURL) < 10 {
		return errInvalidURL
	}
	if !strings.HasPrefix(req.TargetURL, "http://") && !strings.HasPrefix(req.TargetURL, "https://") {
		return errInvalidURL
	}
	if len(req.Objective) < 5 {
		return errObjectiveTooShort
	}
	if len(req.Objective) > 2000 {
		return errObjectiveTooLong
	}
	return nil
}

func (sc *SightedController) updateJobStatus(jobID, status, errMsg string) {
	sc.db.Model(&models.Job{}).Where("id = ?", jobID).Updates(map[string]interface{}{
		"status":        status,
		"error_message": errMsg,
		"updated_at":    time.Now(),
	})
}

func (sc *SightedController) updateJobCompleted(jobID string, completedAt *time.Time, durationMs *int, resultJSON json.RawMessage) {
	sc.db.Model(&models.Job{}).Where("id = ?", jobID).Updates(map[string]interface{}{
		"status":       "COMPLETED",
		"completed_at": completedAt,
		"duration_ms":  durationMs,
		"result_json":  resultJSON,
		"updated_at":   time.Now(),
	})

	// Persist result to disk for academic defense evidence
	writeResultToDisk(jobID, resultJSON)
}

var (
	errInvalidURL        = &validationError{"Invalid target_url. Must be a valid HTTP(S) URL."}
	errObjectiveTooShort = &validationError{"Objective must be at least 5 characters."}
	errObjectiveTooLong  = &validationError{"Objective must be at most 2000 characters."}
)

type validationError struct{ msg string }

func (e *validationError) Error() string { return e.msg }

// writeResultToDisk persists the raw workflow result JSON to the local filesystem.
// Output directory: ./workflow_results/
// Filename format:  workflow_result_<jobID>.json
func writeResultToDisk(jobID string, rawJSON json.RawMessage) {
	outputDir := "workflow_results"
	if err := os.MkdirAll(outputDir, 0755); err != nil {
		log.Printf("[ResultWriter] Failed to create output directory: %v", err)
		return
	}

	// Pretty-print the JSON for human readability
	var formatted json.RawMessage
	jsonData, err := json.MarshalIndent(json.RawMessage(rawJSON), "", "  ")
	if err != nil {
		// Fallback: write raw bytes if formatting fails
		jsonData = rawJSON
		_ = formatted // suppress unused warning
	}

	fileName := fmt.Sprintf("workflow_result_%s.json", jobID)
	filePath := filepath.Join(outputDir, fileName)

	if err := os.WriteFile(filePath, jsonData, 0644); err != nil {
		log.Printf("[ResultWriter] Failed to write result file: %v", err)
		return
	}

	log.Printf("[ResultWriter] Result persisted | JobID=%s | Path=%s | Size=%d bytes", jobID, filePath, len(jsonData))
}
