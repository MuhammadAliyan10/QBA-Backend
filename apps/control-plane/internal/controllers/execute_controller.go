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
	"gorm.io/gorm/clause"
)

// ─── REQUEST / RESPONSE ─────────────────────────────────────────────────────

// ExecuteRequest is the JSON body for POST /v1/execute.
type ExecuteRequest struct {
	// TargetURL is the primary target. Deprecated in favor of TargetUrls but kept for backward compat.
	TargetURL  string   `json:"target_url,omitempty"`
	// TargetUrls is the preferred multi-URL field. If empty, TargetURL is used as a single-element list.
	TargetUrls []string `json:"target_urls,omitempty"`
	// NavigationObjective drives Phase 1 (click/type). If empty, Phase 1 is skipped entirely.
	NavigationObjective string `json:"navigation_objective,omitempty"`
	// ExtractionSchema drives Phase 2 (semantic data extraction). Must be provided for extraction.
	ExtractionSchema map[string]interface{} `json:"extraction_schema,omitempty"`
	// Objective is deprecated. Kept for backward compat — maps to NavigationObjective if set.
	Objective      string                 `json:"objective,omitempty"`
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
	// Attachments carries Base64-encoded files from the CLI.
	Attachments []temporal.Attachment `json:"attachments,omitempty"`
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
			"message": "Invalid request body",
			"details": err.Error(),
		})
		return
	}

	// Normalize TargetUrls: merge deprecated TargetURL into the array.
	if len(req.TargetUrls) == 0 && strings.TrimSpace(req.TargetURL) != "" {
		req.TargetUrls = []string{req.TargetURL}
	}
	if len(req.TargetUrls) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "invalid_request",
			"message": "At least one target URL is required (target_url or target_urls)",
		})
		return
	}
	// Set TargetURL to first element for backward compat (URL validation, etc.)
	req.TargetURL = req.TargetUrls[0]

	// Normalize Objective: map deprecated objective to navigation_objective.
	if strings.TrimSpace(req.NavigationObjective) == "" && strings.TrimSpace(req.Objective) != "" {
		req.NavigationObjective = req.Objective
	}
	if strings.TrimSpace(req.NavigationObjective) == "" && req.ExtractionSchema == nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "invalid_request",
			"message": "At least one of navigation_objective or extraction_schema must be provided",
		})
		return
	}

	// PATCH: 1.5MB Payload Cap (Base64 is ~33% larger, so we allow up to ~2MB of string length)
	const maxBase64Length = 2 * 1024 * 1024 // 2MB
	for _, att := range req.Attachments {
		if len(att.Base64) > maxBase64Length {
			c.JSON(http.StatusRequestEntityTooLarge, gin.H{
				"error":   "payload_too_large",
				"message": "Attachment exceeds the 1.5MB maximum file size limit",
			})
			return
		}
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

	// NOTE: Idempotency is now enforced atomically at the DB layer via
	// clause.OnConflict below. The old First()+Create() pattern had a
	// thundering-herd race condition under concurrent requests.

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

	// Validate logic only if a navigation objective is provided.
	if strings.TrimSpace(req.NavigationObjective) != "" {
		logicRes, err := ec.lv.ValidateLogic(preflightCtx, req.NavigationObjective, urlRes.Domain)
		if err != nil {
			log.Printf("[PreFlight] Logic Validator Error (non-fatal): %v", err)
		} else if logicRes != nil && !logicRes.IsPossible {
			log.Printf("[PreFlight] Logic Rejected | JobID=%s | Reason=%s", jobID, logicRes.Reason)
			c.JSON(http.StatusBadRequest, gin.H{
				"error":   "logic_rejected",
				"message": logicRes.Reason,
			})
			return
		}
	}

	log.Printf("[PreFlight] Checks Passed | JobID=%s", jobID)

	// Ensure a workflow record exists to satisfy foreign key constraints.
	workflowID := uuid.New().String()
	workflow := &models.Workflow{
		ID:          workflowID,
		UserID:      tenantID,
		Name:        "Ad-hoc CLI Mission",
		TriggerType: "ON_DEMAND",
		RecipeJSON:  []byte("{}"),
		IsActive:    true,
		CreatedAt:   time.Now(),
		UpdatedAt:   time.Now(),
	}
	if err := ec.db.Create(workflow).Error; err != nil {
		log.Printf("[ExecuteController] DB Error: failed to create placeholder workflow: %v", err)
	}

	job := &models.Job{
		ID:         jobID,
		UserID:     tenantID,
		WorkflowID: workflowID,
		Status:     "QUEUED",
		CreatedAt:  time.Now(),
		UpdatedAt:  time.Now(),
	}
	if strings.TrimSpace(req.CallbackURL) != "" {
		u := strings.TrimSpace(req.CallbackURL)
		job.WebhookURL = &u
	}

	// PATCH 2: Atomic UPSERT — eliminates thundering-herd race condition.
	// If a row with this ID already exists, DoNothing prevents a duplicate insert.
	// We then check RowsAffected to detect idempotent retries.
	result := ec.db.Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "id"}},
		DoNothing: true,
	}).Create(job)

	if result.Error != nil {
		log.Printf("[ExecuteController] DB Error: failed to create job: %v", result.Error)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "database_error",
			"message": "Failed to persist job record",
		})
		return
	}

	// RowsAffected == 0 means the job already existed (idempotent retry).
	if result.RowsAffected == 0 {
		var existingJob models.Job
		if err := ec.db.Where("id = ?", jobID).First(&existingJob).Error; err != nil {
			c.JSON(http.StatusConflict, gin.H{
				"error":   "duplicate_execution",
				"message": "Job exists but could not be loaded",
				"job_id":  jobID,
			})
			return
		}
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

	// Resolve session_state: credential_id takes precedence over inline sessionState.
	// PATCH 3: Strict Vault Resolution — differentiate "not found" from "DB dead".
	resolvedSessionState := req.SessionState
	if strings.TrimSpace(req.CredentialID) != "" {
		var vaultSession models.VaultSession
		// 1. Try to find in the new VaultSessions table (quanta auth flow)
		vaultErr := ec.db.Where("id = ? AND user_id = ?", req.CredentialID, tenantID).First(&vaultSession).Error

		if vaultErr == nil {
			// Found in Vault — decrypt and use
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

		} else if errors.Is(vaultErr, gorm.ErrRecordNotFound) {
			// Record genuinely does not exist in VaultSessions → safe to fallback to legacy
			var cred models.Credential
			if err := ec.db.Where("id = ? AND client_id = ?", req.CredentialID, tenantID).First(&cred).Error; err != nil {
				if errors.Is(err, gorm.ErrRecordNotFound) {
					c.JSON(http.StatusNotFound, gin.H{
						"error":   "credential_not_found",
						"message": "No credential found for the given credential_id in Vault or legacy storage",
					})
					return
				}
				log.Printf("[ExecuteController] Legacy credential DB error: %v", err)
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

		} else {
			// DB infrastructure error (connection dead, timeout, etc.) — abort immediately.
			log.Printf("[ExecuteController] FATAL: Vault DB query failed (not ErrRecordNotFound): %v", vaultErr)
			c.JSON(http.StatusInternalServerError, gin.H{
				"error":   "database_error",
				"message": "Fatal database error during vault session resolution",
			})
			return
		}
	}

	if ec.tm == nil {
		log.Printf("[ExecuteController] Error: Temporal Manager is nil. Check if Temporal server is running at localhost:7233")
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"error":   "orchestrator_unavailable",
			"message": "Automation orchestrator is currently offline. Please ensure Temporal is running.",
		})
		return
	}

	// PATCH 1: Detach from HTTP request context for Temporal dispatch.
	// If the API client disconnects (TCP reset, browser close, timeout),
	// the HTTP context cancels. Without detachment, this kills the Temporal
	// StartExecution RPC, orphaning the already-persisted job in QUEUED state.
	detachedCtx := context.WithoutCancel(c.Request.Context())

	runID, err := ec.tm.StartExecution(
		detachedCtx,
		jobID,
		workflowID,
		req.TargetUrls,
		req.NavigationObjective,
		req.ExtractionSchema,
		req.EngineSettings,
		resolvedSessionState,
		req.Attachments,
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
