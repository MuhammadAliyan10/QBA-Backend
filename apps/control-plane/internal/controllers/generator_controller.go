package controllers

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"time"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"go.temporal.io/sdk/client"
)

type GeneratorController struct {
	temporalClient client.Client
	lv             *services.LogicValidator
}

// NewGeneratorController creates a controller using a shared Temporal client and LogicValidator.
func NewGeneratorController(tc client.Client, lv *services.LogicValidator) *GeneratorController {
	if tc == nil {
		log.Println("[Generator] Warning: Temporal client is nil — generation will be unavailable")
	}
	return &GeneratorController{temporalClient: tc, lv: lv}
}

type GenerateRequest struct {
	Prompt  string                   `json:"prompt" binding:"required"`
	URL     string                   `json:"url" binding:"required"`
	Cookies []map[string]interface{} `json:"cookies"`
}

// Async Response - returns job_id for WebSocket streaming
type GenerateAsyncResponse struct {
	Status    string `json:"status"`
	JobID     string `json:"job_id,omitempty"`
	WSChannel string `json:"ws_channel,omitempty"`
	Error     string `json:"error,omitempty"`
}

// Sync Response - returns full workflow (fallback)
type GenerateSyncResponse struct {
	Status string                  `json:"status"`
	Data   *GenerateSyncData       `json:"data,omitempty"`
	Error  string                  `json:"error,omitempty"`
}

type GenerateSyncData struct {
	Nodes []map[string]interface{} `json:"nodes"`
	Edges []map[string]interface{} `json:"edges"`
	Stats map[string]interface{}   `json:"stats,omitempty"`
}

func (ctrl *GeneratorController) HandleGenerate(c *gin.Context) {
	userID, ok := middleware.GetUserID(c)
	if !ok || userID == "" {
		c.JSON(http.StatusUnauthorized, GenerateAsyncResponse{Status: "error", Error: "Authentication required"})
		return
	}

	var req GenerateRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, GenerateAsyncResponse{
			Status: "error",
			Error:  "Both 'prompt' and 'url' are required",
		})
		return
	}

	log.Printf("[Generator] Received request: prompt='%s', url='%s'", req.Prompt, req.URL)

	// Wrap request context with strict pre-flight timeout
	preflightCtx, cancel := context.WithTimeout(c.Request.Context(), 7*time.Second)
	defer cancel()

	// Step 1: Validate URL
	urlResult, err := services.ValidateURL(preflightCtx, req.URL)
	if err != nil {
		switch {
		case errors.Is(err, services.ErrWAFBlocked):
			c.JSON(http.StatusUnprocessableEntity, GenerateAsyncResponse{
				Status: "error",
				Error:  "Target site is actively blocking automation (WAF/Cloudflare)",
			})
			return
		case errors.Is(err, services.ErrSSRFBlocked):
			c.JSON(http.StatusBadRequest, GenerateAsyncResponse{
				Status: "error",
				Error:  "Target URL is not allowed",
			})
			return
		default:
			c.JSON(http.StatusBadRequest, GenerateAsyncResponse{
				Status: "error",
				Error:  fmt.Sprintf("URL validation error: %v", err),
			})
			return
		}
	}
	if !urlResult.Valid {
		c.JSON(http.StatusBadRequest, GenerateAsyncResponse{
			Status: "error",
			Error:  fmt.Sprintf("URL Validation Failed: %s", urlResult.Error),
		})
		return
	}

	// Step 2: Validate Logic
	logicResult, err := ctrl.lv.ValidateLogic(preflightCtx, req.Prompt, urlResult.Domain)
	if err != nil {
		if preflightCtx.Err() == context.DeadlineExceeded {
			c.JSON(http.StatusGatewayTimeout, GenerateAsyncResponse{
				Status: "error",
				Error:  "Cognitive validation timed out",
			})
			return
		}
		c.JSON(http.StatusInternalServerError, GenerateAsyncResponse{
			Status: "error",
			Error:  fmt.Sprintf("Logic validation error: %v", err),
		})
		return
	}
	if !logicResult.IsPossible {
		c.JSON(http.StatusBadRequest, GenerateAsyncResponse{
			Status: "error",
			Error:  fmt.Sprintf("This action is not possible: %s", logicResult.Reason),
		})
		return
	}

	// Check if Temporal is available
	if ctrl.temporalClient == nil {
		log.Println("[Generator] Temporal not available, using fallback mode")
		c.JSON(http.StatusServiceUnavailable, GenerateAsyncResponse{
			Status: "error",
			Error:  "Workflow engine not available. Please start Temporal.",
		})
		return
	}

	// Generate unique job ID
	jobID := uuid.New().String()

	// Start Temporal workflow asynchronously
	workflowOptions := client.StartWorkflowOptions{
		ID:        jobID,
		TaskQueue: "e2e-browser-tasks",
	}

	// Workflow payload
	payload := map[string]interface{}{
		"job_id":   jobID,
		"user_id":  userID,
		"prompt":   req.Prompt,
		"url":      req.URL,
		"cookies":  req.Cookies,
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	// Start workflow
	workflowRun, err := ctrl.temporalClient.ExecuteWorkflow(
		ctx,
		workflowOptions,
		"GenerateWorkflowRecipe",  // Workflow name as registered in Python
		payload,
	)
	if err != nil {
		log.Printf("[Generator] Failed to start workflow: %v", err)
		c.JSON(http.StatusInternalServerError, GenerateAsyncResponse{
			Status: "error",
			Error:  fmt.Sprintf("Failed to start workflow: %v", err),
		})
		return
	}

	log.Printf("[Generator] Started workflow: %s (run: %s)", jobID, workflowRun.GetRunID())

	// Return immediately - frontend will subscribe to WebSocket for updates
	c.JSON(http.StatusAccepted, GenerateAsyncResponse{
		Status:    "generating",
		JobID:     jobID,
		WSChannel: fmt.Sprintf("job.update.%s", jobID),
	})
}

// HandleGenerateSync - Blocking version that waits for workflow completion
// Use this for simpler integration or testing
func (ctrl *GeneratorController) HandleGenerateSync(c *gin.Context) {
	userID, ok := middleware.GetUserID(c)
	if !ok || userID == "" {
		c.JSON(http.StatusUnauthorized, GenerateSyncResponse{Status: "error", Error: "Authentication required"})
		return
	}

	var req GenerateRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, GenerateSyncResponse{
			Status: "error",
			Error:  "Both 'prompt' and 'url' are required",
		})
		return
	}

	log.Printf("[Generator] Sync request: prompt='%s', url='%s'", req.Prompt, req.URL)

	// Wrap request context with strict pre-flight timeout
	preflightCtx, cancel := context.WithTimeout(c.Request.Context(), 7*time.Second)
	defer cancel()

	// Step 1: Validate URL
	urlResult, err := services.ValidateURL(preflightCtx, req.URL)
	if err != nil {
		switch {
		case errors.Is(err, services.ErrWAFBlocked):
			c.JSON(http.StatusUnprocessableEntity, GenerateSyncResponse{
				Status: "error",
				Error:  "Target site is actively blocking automation (WAF/Cloudflare)",
			})
			return
		case errors.Is(err, services.ErrSSRFBlocked):
			c.JSON(http.StatusBadRequest, GenerateSyncResponse{
				Status: "error",
				Error:  "Target URL is not allowed",
			})
			return
		default:
			c.JSON(http.StatusBadRequest, GenerateSyncResponse{
				Status: "error",
				Error:  fmt.Sprintf("URL validation error: %v", err),
			})
			return
		}
	}
	if !urlResult.Valid {
		c.JSON(http.StatusBadRequest, GenerateSyncResponse{
			Status: "error",
			Error:  fmt.Sprintf("URL Validation Failed: %s", urlResult.Error),
		})
		return
	}

	// Step 2: Validate Logic
	logicResult, err := ctrl.lv.ValidateLogic(preflightCtx, req.Prompt, urlResult.Domain)
	if err != nil {
		c.JSON(http.StatusInternalServerError, GenerateSyncResponse{
			Status: "error",
			Error:  fmt.Sprintf("Logic validation error: %v", err),
		})
		return
	}
	if !logicResult.IsPossible {
		c.JSON(http.StatusBadRequest, GenerateSyncResponse{
			Status: "error",
			Error:  fmt.Sprintf("This action is not possible: %s", logicResult.Reason),
		})
		return
	}

	if ctrl.temporalClient == nil {
		c.JSON(http.StatusServiceUnavailable, GenerateSyncResponse{
			Status: "error",
			Error:  "Workflow engine not available",
		})
		return
	}

	jobID := uuid.New().String()

	workflowOptions := client.StartWorkflowOptions{
		ID:        jobID,
		TaskQueue: "e2e-browser-tasks",
	}

	payload := map[string]interface{}{
		"job_id":   jobID,
		"user_id":  userID,
		"prompt":   req.Prompt,
		"url":      req.URL,
		"cookies":  req.Cookies,
	}

	// Start and wait for workflow (up to 5 minutes)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	workflowRun, err := ctrl.temporalClient.ExecuteWorkflow(
		ctx,
		workflowOptions,
		"GenerateWorkflowRecipe",
		payload,
	)
	if err != nil {
		log.Printf("[Generator] Failed to start workflow: %v", err)
		c.JSON(http.StatusInternalServerError, GenerateSyncResponse{
			Status: "error",
			Error:  fmt.Sprintf("Failed to start workflow: %v", err),
		})
		return
	}

	// Wait for result
	var result map[string]interface{}
	if err := workflowRun.Get(ctx, &result); err != nil {
		log.Printf("[Generator] Workflow failed: %v", err)
		c.JSON(http.StatusInternalServerError, GenerateSyncResponse{
			Status: "error",
			Error:  fmt.Sprintf("Workflow failed: %v", err),
		})
		return
	}

	log.Printf("[Generator] Workflow completed: %s", jobID)

	// Extract nodes and edges
	nodes, _ := result["nodes"].([]interface{})
	edges, _ := result["edges"].([]interface{})
	stats, _ := result["stats"].(map[string]interface{})

	// Convert to expected format
	nodesMap := make([]map[string]interface{}, len(nodes))
	for i, n := range nodes {
		if m, ok := n.(map[string]interface{}); ok {
			nodesMap[i] = m
		}
	}

	edgesMap := make([]map[string]interface{}, len(edges))
	for i, e := range edges {
		if m, ok := e.(map[string]interface{}); ok {
			edgesMap[i] = m
		}
	}

	c.JSON(http.StatusOK, GenerateSyncResponse{
		Status: "success",
		Data: &GenerateSyncData{
			Nodes: nodesMap,
			Edges: edgesMap,
			Stats: stats,
		},
	})
}
