package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strings"
	"time"

	"e2e-platform/apps/control-plane/internal/metrics"
	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"
	"e2e-platform/apps/control-plane/internal/temporal"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/gorm"
)

// ─── REQUEST / RESPONSE ─────────────────────────────────────────────────────

// ExecuteRequest is the JSON body for POST /v1/execute.
type ExecuteRequest struct {
	TargetURL      string                 `json:"target_url" binding:"required"`
	Objective      string                 `json:"objective" binding:"required"`
	EngineSettings map[string]interface{} `json:"engine_settings,omitempty"`
	CallbackURL    string                 `json:"callback_url,omitempty"`
	// SessionState is an optional pre-authenticated Playwright storage_state dictionary.
	// When provided, the execution worker injects it directly into the browser context,
	// bypassing WAF/Risk Engine challenges on sessions that were manually established.
	SessionState map[string]interface{} `json:"sessionState,omitempty"`
	// CredentialID references an encrypted credential stored via POST /v1/credentials.
	// When set, the handler decrypts the stored session_data and injects it as session_state,
	// taking priority over an inline sessionState value.
	CredentialID string `json:"credential_id,omitempty"`
}

// ExecuteResponse is the HTTP 202 response body.
type ExecuteResponse struct {
	JobID  string `json:"job_id"`
	RunID  string `json:"run_id"`
	Status string `json:"status"`
}

// ─── CONTROLLER ──────────────────────────────────────────────────────────────

// ExecuteController handles ad-hoc async automation execution.
type ExecuteController struct {
	tm          *temporal.TemporalManager
	lv          *services.LogicValidator
	db          *gorm.DB
	crypto      *services.CryptoService
	vaultCrypto *services.VaultCryptoService
	identity    *services.IdentityService
}

func NewExecuteController(db *gorm.DB, tm *temporal.TemporalManager, lv *services.LogicValidator, identity *services.IdentityService) *ExecuteController {
	return &ExecuteController{
		db:          db,
		tm:          tm,
		lv:          lv,
		crypto:      services.GetCryptoService(),
		vaultCrypto: services.GetVaultCryptoService(),
		identity:    identity,
	}
}

// HandleExecuteAsync handles POST /v1/execute.
func (ec *ExecuteController) HandleExecuteAsync(c *gin.Context) {
	clerkID, exists := middleware.GetUserID(c)
	if !exists || clerkID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{
			"error":   "unauthenticated",
			"message": "Authentication required",
		})
		return
	}

	tenantID, err := ec.identity.ResolveUserProfileID(clerkID)
	if err != nil {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid tenant context"})
		return
	}

	var req ExecuteRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "invalid_request",
			"message": "Missing required fields: target_url, objective",
			"details": err.Error(),
		})
		return
	}

	jobID := strings.TrimSpace(c.GetHeader("X-Idempotency-Key"))
	if jobID == "" {
		jobID = uuid.New().String()
	}
	// Enforce UUID idempotency keys (DB expects uuid)
	if _, err := uuid.Parse(jobID); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "invalid_idempotency_key",
			"message": "X-Idempotency-Key must be a UUID",
		})
		return
	}

	// Idempotent retry: job row already exists
	var existingJob models.Job
	if err := ec.db.Where("id = ?", jobID).First(&existingJob).Error; err == nil {
		if existingJob.UserID != tenantID {
			c.JSON(http.StatusForbidden, gin.H{
				"error":   "forbidden",
				"message": "Idempotency key belongs to another user",
			})
			return
		}
		runID, terr := ec.tm.GetExistingRunID(c.Request.Context(), jobID)
		if terr != nil {
			c.JSON(http.StatusConflict, gin.H{
				"error":   "duplicate_execution",
				"message": "Job exists but workflow state could not be loaded",
				"job_id":  jobID,
			})
			return
		}
		c.JSON(http.StatusOK, ExecuteResponse{
			JobID:  jobID,
			RunID:  runID,
			Status: "already_running",
		})
		return
	}

	preflightCtx, cancel := context.WithTimeout(c.Request.Context(), 15*time.Second)
	defer cancel()

	urlRes, err := services.ValidateURL(preflightCtx, req.TargetURL)
	if err != nil {
		switch {
		case errors.Is(err, services.ErrWAFBlocked):
			// If BYOS is provided, ignore the WAF block since the injected session might bypass it.
			if req.SessionState == nil && strings.TrimSpace(req.CredentialID) == "" {
				c.JSON(http.StatusUnprocessableEntity, gin.H{
					"error":   "waf_blocked",
					"message": "Target site is actively blocking automation (WAF/Cloudflare)",
				})
				return
			}
			log.Printf("[PreFlight] Ignored WAF block due to BYOS session | JobID=%s", jobID)
		case errors.Is(err, services.ErrSSRFBlocked):
			c.JSON(http.StatusBadRequest, gin.H{
				"error":   "ssrf_blocked",
				"message": "Target URL is not allowed",
			})
			return
		default:
			if preflightCtx.Err() == context.DeadlineExceeded {
				c.JSON(http.StatusGatewayTimeout, gin.H{
					"error":   "preflight_timeout",
					"message": "Network validation timed out.",
				})
				return
			}
			c.JSON(http.StatusBadRequest, gin.H{
				"error":   "url_validation_failed",
				"message": err.Error(),
			})
			return
		}
	}

	if urlRes == nil || !urlRes.Valid {
		msg := "Invalid target URL"
		if urlRes != nil && urlRes.Error != "" {
			msg = urlRes.Error
		}
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "invalid_target_url",
			"message": msg,
		})
		return
	}

	logicRes, err := ec.lv.ValidateLogic(preflightCtx, req.Objective, urlRes.Domain)
	if err != nil {
		// Best-effort in local/dev: do not fail the request on validator timeouts/errors.
		// The executor will deterministically fail later if the objective is impossible.
		log.Printf("[PreFlight] Logic Validator Error (non-fatal): %v", err)
	} else if logicRes != nil && !logicRes.IsPossible {
		log.Printf("[PreFlight] Logic Rejected | JobID=%s | Reason=%s", jobID, logicRes.Reason)
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "logic_rejected",
			"message": logicRes.Reason,
		})
		return
	}

	log.Printf("[PreFlight] Checks Passed | JobID=%s", jobID)

	// In local/dev stacks the `workflows` table may not exist.
	// The execution plane only needs a workflow_id value for tracing; no DB row is required.
	workflowID := uuid.New().String()

	job := &models.Job{
		ID:         jobID,
		UserID:     tenantID,
		WorkflowID: workflowID,
		Status:     "PENDING",
	}
	if strings.TrimSpace(req.CallbackURL) != "" {
		u := strings.TrimSpace(req.CallbackURL)
		job.WebhookURL = &u
	}
	if err := ec.db.Create(job).Error; err != nil {
		log.Printf("[ExecuteController] DB Error: failed to create job: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "database_error",
			"message": "Failed to persist job record",
		})
		return
	}

	// Resolve session_state: credential_id takes precedence over inline sessionState.
	resolvedSessionState := req.SessionState
	if strings.TrimSpace(req.CredentialID) != "" {
		var vaultSession models.VaultSession
		// 1. Try to find in the new VaultSessions table (quanta auth flow)
		if err := ec.db.Where("id = ? AND user_id = ?", req.CredentialID, tenantID).First(&vaultSession).Error; err == nil {
			log.Printf("[ExecuteController] Found session in Vault | JobID=%s | VaultID=%s", jobID, req.CredentialID)
			
			plaintext, err := ec.vaultCrypto.Decrypt(vaultSession.EncryptedState)
			if err != nil {
				log.Printf("[ExecuteController] Vault decryption error: %v", err)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "decryption_error", "message": "Failed to decrypt vault session"})
				return
			}

			var sessionMap map[string]interface{}
			if err := json.Unmarshal(plaintext, &sessionMap); err != nil {
				log.Printf("[ExecuteController] Vault unmarshal error: %v", err)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "deserialization_error", "message": "Vault session data is malformed"})
				return
			}
			resolvedSessionState = sessionMap
		} else {
			// 2. Fallback to legacy Credentials table
			var cred models.Credential
			if err := ec.db.Where("id = ? AND client_id = ?", req.CredentialID, tenantID).First(&cred).Error; err != nil {
				if errors.Is(err, gorm.ErrRecordNotFound) {
					c.JSON(http.StatusNotFound, gin.H{
						"error":   "credential_not_found",
						"message": "No credential found for the given credential_id in Vault or legacy storage",
					})
					return
				}
				log.Printf("[ExecuteController] Credential DB error: %v", err)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "database_error", "message": "Failed to load credential"})
				return
			}

			plaintext, err := ec.crypto.Decrypt(cred.EncryptedData)
			if err != nil {
				log.Printf("[ExecuteController] Credential decryption error: %v", err)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "decryption_error", "message": "Failed to decrypt credential"})
				return
			}

			var sessionMap map[string]interface{}
			if err := json.Unmarshal(plaintext, &sessionMap); err != nil {
				log.Printf("[ExecuteController] Session data unmarshal error: %v", err)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "deserialization_error", "message": "Credential data is malformed"})
				return
			}
			resolvedSessionState = sessionMap
			log.Printf("[ExecuteController] Injecting decrypted legacy credential | JobID=%s | CredID=%s", jobID, req.CredentialID)
		}
	}

	runID, err := ec.tm.StartExecution(
		c.Request.Context(),
		jobID,
		workflowID,
		req.TargetURL,
		req.Objective,
		req.EngineSettings,
		resolvedSessionState,
	)

	if err != nil {
		if isWorkflowAlreadyStarted(err) {
			log.Printf("[ExecuteController] Idempotent retry detected | JobID=%s", jobID)

			existingRunID, descErr := ec.tm.GetExistingRunID(c.Request.Context(), jobID)
			if descErr != nil {
				log.Printf("[ExecuteController] Failed to describe existing workflow: %v", descErr)
				c.JSON(http.StatusConflict, gin.H{
					"error":   "duplicate_execution",
					"message": "Workflow already exists for this idempotency key",
					"job_id":  jobID,
				})
				return
			}

			c.JSON(http.StatusOK, ExecuteResponse{
				JobID:  jobID,
				RunID:  existingRunID,
				Status: "already_running",
			})
			return
		}

		log.Printf("[ExecuteController] Temporal error: %v", err)
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"error":   "orchestrator_unavailable",
			"message": "Failed to queue automation job. Retry later.",
		})
		return
	}

	log.Printf("[ExecuteController] Job queued | JobID=%s | RunID=%s", jobID, runID)

	metrics.IncrementJobQueueCount("queued")

	c.JSON(http.StatusAccepted, ExecuteResponse{
		JobID:  jobID,
		RunID:  runID,
		Status: "queued",
	})
}

func isWorkflowAlreadyStarted(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "already started") ||
		strings.Contains(msg, "AlreadyStarted") ||
		strings.Contains(msg, "WorkflowExecutionAlreadyStarted")
}
