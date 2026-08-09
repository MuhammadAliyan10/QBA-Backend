// internal/controllers/user_controller.go
package controllers

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// ─── CONSTANTS ───────────────────────────────────────────────────────────────

const (
	logsDefaultLimit = 50
	logsMaxLimit     = 200
)

// ─── CONTROLLER ──────────────────────────────────────────────────────────────

type UserController struct {
	db       *gorm.DB
	identity *services.IdentityService
}

func NewUserController(db *gorm.DB, identity *services.IdentityService) *UserController {
	return &UserController{db: db, identity: identity}
}

// ─── DTOS ───────────────────────────────────────────────────────────────────

type UpdateWebhookRequest struct {
	WebhookUrl string `json:"webhook_url"`
}

type TestWebhookRequest struct {
	WebhookUrl string `json:"webhook_url" binding:"required"`
}

// DeveloperStatsResponse aggregates user usage data in one response.
type DeveloperStatsResponse struct {
	TotalApiKeys   int64 `json:"total_api_keys"`
	ActiveApiKeys  int64 `json:"active_api_keys"`
	TotalJobs      int64 `json:"total_jobs"`
	FailedJobs     int64 `json:"failed_jobs"`
	CreditsUsed    int64 `json:"credits_used"`
	CreditsBalance int64 `json:"credits_balance"`
}

// ApiLogResponse is a single log entry enriched with workflow metadata.
type ApiLogResponse struct {
	ID        string `json:"id"`
	JobID     string `json:"job_id"`
	Workflow  string `json:"workflow"`
	Level     string `json:"level"`
	Message   string `json:"message"`
	Timestamp string `json:"timestamp"`
}

// ─── STATS HANDLER ──────────────────────────────────────────────────────────

// HandleGetStats returns aggregated account stats for the authenticated user.
// GET /v1/user/stats
//
// All counts are derived from a single SQL query with sub-selects to avoid
// N+1 query patterns and connection pool churn.
func (ctrl *UserController) HandleGetStats(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	type statsRow struct {
		TotalApiKeys  int64
		ActiveApiKeys int64
		TotalJobs     int64
		FailedJobs    int64
	}

	var row statsRow
	query := `
		SELECT
			(SELECT COUNT(*) FROM api_keys   WHERE user_id = ?)                        AS total_api_keys,
			(SELECT COUNT(*) FROM api_keys   WHERE user_id = ? AND is_active = true)   AS active_api_keys,
			(SELECT COUNT(*) FROM jobs       WHERE user_id = ?)                        AS total_jobs,
			(SELECT COUNT(*) FROM jobs       WHERE user_id = ? AND status = 'FAILED')  AS failed_jobs
	`
	if err := ctrl.db.Raw(query, tenantID, tenantID, tenantID, tenantID).Scan(&row).Error; err != nil {
		log.Printf("[UserController] HandleGetStats DB error | TenantID=%s | Err=%v", tenantID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to retrieve account statistics",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	var usage models.UserUsage
	var creditsUsed, creditsBalance int64
	if err := ctrl.db.Where("user_id = ?", tenantID).First(&usage).Error; err == nil {
		creditsUsed = int64(usage.TotalCreditsUsed)
		creditsBalance = int64(usage.CreditsBalance)
	}

	c.JSON(http.StatusOK, gin.H{
		"data": DeveloperStatsResponse{
			TotalApiKeys:   row.TotalApiKeys,
			ActiveApiKeys:  row.ActiveApiKeys,
			TotalJobs:      row.TotalJobs,
			FailedJobs:     row.FailedJobs,
			CreditsUsed:    creditsUsed,
			CreditsBalance: creditsBalance,
		},
	})
}

// ─── LOGS HANDLER ───────────────────────────────────────────────────────────

// HandleGetLogs returns paginated job logs for the authenticated user.
// GET /v1/user/logs?limit=50&before=<RFC3339 timestamp>
//
// Pagination uses a cursor (timestamp of the oldest record in the previous page)
// rather than OFFSET to avoid O(n) scans on large datasets.
func (ctrl *UserController) HandleGetLogs(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	// Parse and clamp limit
	limit := logsDefaultLimit
	if s := strings.TrimSpace(c.Query("limit")); s != "" {
		if n, err := strconv.Atoi(s); err == nil && n > 0 {
			limit = n
		} else if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{
				"error":      "invalid_parameter",
				"message":    "'limit' must be a positive integer",
				"request_id": middleware.GetRequestID(c),
			})
			return
		}
	}
	if limit > logsMaxLimit {
		limit = logsMaxLimit
	}

	// Optional cursor: fetch logs BEFORE this timestamp (exclusive)
	var beforeTime *time.Time
	if s := strings.TrimSpace(c.Query("before")); s != "" {
		t, err := time.Parse(time.RFC3339, s)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{
				"error":      "invalid_parameter",
				"message":    "'before' must be an RFC3339 timestamp (e.g. 2026-08-09T10:00:00Z)",
				"request_id": middleware.GetRequestID(c),
			})
			return
		}
		beforeTime = &t
	}

	type logRow struct {
		ID           string
		JobID        string
		WorkflowName string
		Level        string
		Message      string
		Timestamp    time.Time
	}

	baseQuery := `
		SELECT l.id, l.job_id, w.name AS workflow_name, l.level, l.message, l.timestamp
		FROM job_logs l
		JOIN jobs j ON l.job_id = j.id
		JOIN workflows w ON j.workflow_id = w.id
		WHERE j.user_id = ?
	`
	args := []interface{}{tenantID}

	if beforeTime != nil {
		baseQuery += " AND l.timestamp < ?"
		args = append(args, *beforeTime)
	}

	baseQuery += " ORDER BY l.timestamp DESC LIMIT ?"
	args = append(args, limit)

	var logs []logRow
	if err := ctrl.db.Raw(baseQuery, args...).Scan(&logs).Error; err != nil {
		log.Printf("[UserController] HandleGetLogs DB error | TenantID=%s | Err=%v", tenantID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to retrieve logs",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	response := make([]ApiLogResponse, 0, len(logs))
	for _, l := range logs {
		response = append(response, ApiLogResponse{
			ID:        l.ID,
			JobID:     l.JobID,
			Workflow:  l.WorkflowName,
			Level:     l.Level,
			Message:   l.Message,
			Timestamp: l.Timestamp.UTC().Format(time.RFC3339),
		})
	}

	// Provide next cursor for the client if there are more records
	var nextCursor *string
	if len(logs) == limit {
		cursor := logs[len(logs)-1].Timestamp.UTC().Format(time.RFC3339)
		nextCursor = &cursor
	}

	c.JSON(http.StatusOK, gin.H{
		"data": response,
		"pagination": gin.H{
			"limit":       limit,
			"next_cursor": nextCursor,
			"has_more":    nextCursor != nil,
		},
	})
}

// ─── WEBHOOK HANDLERS ───────────────────────────────────────────────────────

// HandleGetWebhook returns the current webhook URL and signing secret.
// GET /v1/user/webhook
func (ctrl *UserController) HandleGetWebhook(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	var profile models.UserProfile
	if err := ctrl.db.Select("webhook_url", "webhook_secret").
		Where("id = ?", tenantID).First(&profile).Error; err != nil {
		log.Printf("[UserController] HandleGetWebhook DB error | TenantID=%s | Err=%v", tenantID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to retrieve webhook settings",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	webhookSecret := ""
	if profile.WebhookSecret != nil {
		webhookSecret = *profile.WebhookSecret
	} else {
		// Auto-generate and persist a signing secret on first fetch
		webhookSecret = generateWebhookSecret()
		if err := ctrl.db.Model(&profile).
			Where("id = ?", tenantID).
			Update("webhook_secret", webhookSecret).Error; err != nil {
			log.Printf("[UserController] HandleGetWebhook secret generation error | TenantID=%s | Err=%v", tenantID, err)
		}
	}

	webhookUrl := ""
	if profile.WebhookURL != nil {
		webhookUrl = *profile.WebhookURL
	}

	c.JSON(http.StatusOK, gin.H{
		"data": map[string]string{
			"webhook_url":    webhookUrl,
			"webhook_secret": webhookSecret,
		},
	})
}

// HandleUpdateWebhookUrl sets or clears the user's webhook delivery URL.
// POST /v1/user/webhook/url
func (ctrl *UserController) HandleUpdateWebhookUrl(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	var req UpdateWebhookRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":      "invalid_request",
			"message":    "Invalid request body",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	if req.WebhookUrl != "" {
		if !strings.HasPrefix(req.WebhookUrl, "https://") {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"error":      "validation_error",
				"message":    "Webhook URL must use HTTPS",
				"field":      "webhook_url",
				"request_id": middleware.GetRequestID(c),
			})
			return
		}
		if _, err := url.ParseRequestURI(req.WebhookUrl); err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"error":      "validation_error",
				"message":    "Webhook URL is not a valid URL",
				"field":      "webhook_url",
				"request_id": middleware.GetRequestID(c),
			})
			return
		}
	}

	var val interface{} = req.WebhookUrl
	if req.WebhookUrl == "" {
		val = nil
	}

	if err := ctrl.db.Model(&models.UserProfile{}).
		Where("id = ?", tenantID).
		Update("webhook_url", val).Error; err != nil {
		log.Printf("[UserController] HandleUpdateWebhookUrl DB error | TenantID=%s | Err=%v", tenantID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to update webhook URL",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"status":     "updated",
		"request_id": middleware.GetRequestID(c),
	})
}

// HandleRegenerateWebhookSecret issues a new HMAC signing secret for the user's webhook.
// POST /v1/user/webhook/regenerate
func (ctrl *UserController) HandleRegenerateWebhookSecret(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	newSecret := generateWebhookSecret()

	if err := ctrl.db.Model(&models.UserProfile{}).
		Where("id = ?", tenantID).
		Update("webhook_secret", newSecret).Error; err != nil {
		log.Printf("[UserController] HandleRegenerateWebhookSecret DB error | TenantID=%s | Err=%v", tenantID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to regenerate webhook secret",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"data": map[string]string{
			"webhook_secret": newSecret,
		},
	})
}

// HandleTestWebhook fires a signed test event to the caller's endpoint.
// POST /v1/user/webhook/test
//
// The request is fired from the server (not the browser) to ensure IP consistency
// with production Temporal workers, which is what customers whitelist.
func (ctrl *UserController) HandleTestWebhook(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	var req TestWebhookRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":      "invalid_request",
			"message":    "Request body must contain 'webhook_url'",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	if !strings.HasPrefix(req.WebhookUrl, "https://") {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"error":      "validation_error",
			"message":    "Webhook URL must use HTTPS",
			"field":      "webhook_url",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	var profile models.UserProfile
	if err := ctrl.db.Select("webhook_secret").
		Where("id = ?", tenantID).First(&profile).Error; err != nil {
		log.Printf("[UserController] HandleTestWebhook profile fetch error | TenantID=%s | Err=%v", tenantID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to fetch profile for webhook signing",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	testPayload := map[string]interface{}{
		"event":      "ping",
		"request_id": middleware.GetRequestID(c),
		"timestamp":  time.Now().UTC().Format(time.RFC3339),
		"data": map[string]interface{}{
			"source":  "quanta-control-plane",
			"user_id": tenantID,
			"message": "This is a test webhook event from the Quanta control plane.",
		},
	}

	payloadBytes, _ := json.Marshal(testPayload)

	// Strict outbound HTTP client — mitigates tarpit servers that would exhaust goroutines
	client := &http.Client{Timeout: 5 * time.Second}

	reqHttp, err := http.NewRequest("POST", req.WebhookUrl, bytes.NewBuffer(payloadBytes))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "request_creation_failed",
			"message":    fmt.Sprintf("Failed to build HTTP request: %v", err),
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	reqHttp.Header.Set("Content-Type", "application/json")
	reqHttp.Header.Set("User-Agent", "Quanta-Webhook/1.0")
	reqHttp.Header.Set("X-Quanta-Delivery", middleware.GetRequestID(c))

	if profile.WebhookSecret != nil && *profile.WebhookSecret != "" {
		mac := hmac.New(sha256.New, []byte(*profile.WebhookSecret))
		mac.Write(payloadBytes)
		reqHttp.Header.Set("X-Quanta-Signature-256", "sha256="+hex.EncodeToString(mac.Sum(nil)))
	}

	start := time.Now()
	resp, err := client.Do(reqHttp)
	durationMs := time.Since(start).Milliseconds()

	if err != nil {
		errMsg := err.Error()
		if errors.Is(err, context.DeadlineExceeded) || strings.Contains(errMsg, "Client.Timeout") {
			errMsg = "Connection timed out (5s limit)"
		}
		c.JSON(http.StatusOK, gin.H{
			"data": map[string]interface{}{
				"success":     false,
				"status_code": nil,
				"duration_ms": durationMs,
				"body":        "",
				"error":       errMsg,
			},
		})
		return
	}
	defer resp.Body.Close()

	bodyBytes, _ := io.ReadAll(io.LimitReader(resp.Body, 4096)) // Hard cap at 4KB
	bodyStr := string(bodyBytes)

	c.JSON(http.StatusOK, gin.H{
		"data": map[string]interface{}{
			"success":     resp.StatusCode >= 200 && resp.StatusCode < 300,
			"status_code": resp.StatusCode,
			"duration_ms": durationMs,
			"body":        bodyStr,
		},
	})
}

// ─── UTILS ──────────────────────────────────────────────────────────────────

// generateWebhookSecret generates a 256-bit (32 bytes) CSPRNG webhook signing secret.
// Format: "whsec_<64 hex chars>"
func generateWebhookSecret() string {
	b := make([]byte, 32) // 256-bit secret — minimum for HMAC-SHA256 security margin
	if _, err := rand.Read(b); err != nil {
		panic(fmt.Sprintf("FATAL: crypto/rand failure: %v", err))
	}
	return "whsec_" + hex.EncodeToString(b)
}
