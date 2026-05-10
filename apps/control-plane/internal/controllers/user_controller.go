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
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

type UserController struct {
	db       *gorm.DB
	identity *services.IdentityService
}

func NewUserController(db *gorm.DB, identity *services.IdentityService) *UserController {
	return &UserController{
		db:       db,
		identity: identity,
	}
}

// ─── DTOS ───────────────────────────────────────────────────────────────────

type UpdateWebhookRequest struct {
	WebhookUrl string `json:"webhook_url"`
}

type TestWebhookRequest struct {
	WebhookUrl string `json:"webhook_url" binding:"required"`
}

type DeveloperStatsResponse struct {
	TotalApiKeys   int64 `json:"total_api_keys"`
	ActiveApiKeys  int64 `json:"active_api_keys"`
	TotalJobs      int64 `json:"total_jobs"`
	FailedJobs     int64 `json:"failed_jobs"`
	CreditsUsed    int64 `json:"credits_used"`
	CreditsBalance int64 `json:"credits_balance"`
}

type ApiLogResponse struct {
	ID        string `json:"id"`
	JobID     string `json:"job_id"`
	Workflow  string `json:"workflow"`
	Level     string `json:"level"`
	Message   string `json:"message"`
	Timestamp string `json:"timestamp"`
}

// ─── STATS HANDLERS ─────────────────────────────────────────────────────────

func (c *UserController) HandleGetStats(ctx *gin.Context) {
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

	var stats DeveloperStatsResponse

	c.db.Model(&models.ApiKey{}).Where("user_id = ?", tenantID).Count(&stats.TotalApiKeys)
	c.db.Model(&models.ApiKey{}).Where("user_id = ? AND is_active = ?", tenantID, true).Count(&stats.ActiveApiKeys)
	c.db.Model(&models.Job{}).Where("user_id = ?", tenantID).Count(&stats.TotalJobs)
	c.db.Model(&models.Job{}).Where("user_id = ? AND status = ?", tenantID, "FAILED").Count(&stats.FailedJobs)

	var usage models.UserUsage
	if err := c.db.Where("user_id = ?", tenantID).First(&usage).Error; err == nil {
		stats.CreditsUsed = int64(usage.TotalCreditsUsed)
		stats.CreditsBalance = int64(usage.CreditsBalance)
	}

	ctx.JSON(http.StatusOK, gin.H{"data": stats})
}

func (c *UserController) HandleGetLogs(ctx *gin.Context) {
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

	// Fetch logs joined with jobs to get workflow name, filtered by tenantID
	// GORM raw query to easily join and map to DTO
	var logs []struct {
		ID           string
		JobID        string
		WorkflowName string
		Level        string
		Message      string
		Timestamp    time.Time
	}

	query := `
		SELECT l.id, l.job_id, w.name as workflow_name, l.level, l.message, l.timestamp
		FROM job_logs l
		JOIN jobs j ON l.job_id = j.id
		JOIN workflows w ON j.workflow_id = w.id
		WHERE j.user_id = ?
		ORDER BY l.timestamp DESC
		LIMIT 50
	`

	if err := c.db.Raw(query, tenantID).Scan(&logs).Error; err != nil {
		log.Printf("[UserController] HandleGetLogs error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch logs"})
		return
	}

	var response []ApiLogResponse
	for _, l := range logs {
		response = append(response, ApiLogResponse{
			ID:        l.ID,
			JobID:     l.JobID,
			Workflow:  l.WorkflowName,
			Level:     l.Level,
			Message:   l.Message,
			Timestamp: l.Timestamp.Format("2006-01-02T15:04:05Z07:00"),
		})
	}

	if response == nil {
		response = make([]ApiLogResponse, 0)
	}

	ctx.JSON(http.StatusOK, gin.H{"data": response})
}

// ─── WEBHOOK HANDLERS ───────────────────────────────────────────────────────

func (c *UserController) HandleGetWebhook(ctx *gin.Context) {
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

	var profile models.UserProfile
	if err := c.db.Select("webhook_url", "webhook_secret").Where("id = ?", tenantID).First(&profile).Error; err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch webhook settings"})
		return
	}

	webhookSecret := ""
	if profile.WebhookSecret != nil {
		webhookSecret = *profile.WebhookSecret
	} else {
		// Auto-generate if missing
		webhookSecret = generateWebhookSecret()
		c.db.Model(&profile).Where("id = ?", tenantID).Update("webhook_secret", webhookSecret)
	}

	webhookUrl := ""
	if profile.WebhookURL != nil {
		webhookUrl = *profile.WebhookURL
	}

	ctx.JSON(http.StatusOK, gin.H{
		"data": map[string]string{
			"webhook_url":    webhookUrl,
			"webhook_secret": webhookSecret,
		},
	})
}

func (c *UserController) HandleUpdateWebhookUrl(ctx *gin.Context) {
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

	var req UpdateWebhookRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request payload"})
		return
	}

	if req.WebhookUrl != "" {
		if !strings.HasPrefix(req.WebhookUrl, "https://") {
			ctx.JSON(http.StatusBadRequest, gin.H{"error": "Webhook URL must use HTTPS"})
			return
		}
		if _, err := url.ParseRequestURI(req.WebhookUrl); err != nil {
			ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid URL format"})
			return
		}
	}

	var val interface{} = req.WebhookUrl
	if req.WebhookUrl == "" {
		val = nil
	}

	if err := c.db.Model(&models.UserProfile{}).Where("id = ?", tenantID).Update("webhook_url", val).Error; err != nil {
		log.Printf("[UserController] Update webhook url error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to update webhook URL"})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{"status": "updated"})
}

func (c *UserController) HandleRegenerateWebhookSecret(ctx *gin.Context) {
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

	newSecret := generateWebhookSecret()

	if err := c.db.Model(&models.UserProfile{}).Where("id = ?", tenantID).Update("webhook_secret", newSecret).Error; err != nil {
		log.Printf("[UserController] Regenerate secret error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to regenerate secret"})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{
		"data": map[string]string{
			"webhook_secret": newSecret,
		},
	})
}

// HandleTestWebhook endpoint fires the HTTP request from the Go process,
// ensuring IP consistency with production Temporal activity workers.
func (c *UserController) HandleTestWebhook(ctx *gin.Context) {
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

	var req TestWebhookRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request payload"})
		return
	}

	if !strings.HasPrefix(req.WebhookUrl, "https://") {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Webhook URL must use HTTPS"})
		return
	}

	// Fetch secret to sign the request
	var profile models.UserProfile
	if err := c.db.Select("webhook_secret").Where("id = ?", tenantID).First(&profile).Error; err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch profile for signing"})
		return
	}

	testPayload := map[string]interface{}{
		"event":     "ping",
		"timestamp": time.Now().UTC().Format(time.RFC3339),
		"data": map[string]interface{}{
			"source":  "quanta",
			"user_id": tenantID,
			"message": "This is a test webhook event from the Go control plane.",
		},
	}

	payloadBytes, _ := json.Marshal(testPayload)

	// Strict Outbound Network Client
	// Mitigates hanging servers / tarpits that would exhaust goroutines
	client := &http.Client{
		Timeout: 5 * time.Second,
	}

	reqHttp, err := http.NewRequest("POST", req.WebhookUrl, bytes.NewBuffer(payloadBytes))
	if err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create HTTP request"})
		return
	}

	reqHttp.Header.Set("Content-Type", "application/json")
	reqHttp.Header.Set("User-Agent", "Quanta-Webhook/1.0")

	// Sign payload if secret exists
	if profile.WebhookSecret != nil && *profile.WebhookSecret != "" {
		mac := hmac.New(sha256.New, []byte(*profile.WebhookSecret))
		mac.Write(payloadBytes)
		signature := hex.EncodeToString(mac.Sum(nil))
		reqHttp.Header.Set("X-Quanta-Signature", signature)
	}

	start := time.Now()
	resp, err := client.Do(reqHttp)
	duration := time.Since(start).Milliseconds()

	if err != nil {
		errMsg := err.Error()
		if errors.Is(err, context.DeadlineExceeded) || strings.Contains(errMsg, "Client.Timeout") {
			errMsg = "Connection timed out (5s)"
		}
		ctx.JSON(http.StatusOK, gin.H{
			"data": map[string]interface{}{
				"success":     false,
				"status_code": nil,
				"duration":    duration,
				"body":        "",
				"error":       errMsg,
			},
		})
		return
	}
	defer resp.Body.Close()

	bodyBytes, _ := io.ReadAll(resp.Body)
	bodyStr := string(bodyBytes)
	if len(bodyStr) > 1000 {
		bodyStr = bodyStr[:1000] // Truncate long bodies
	}

	ctx.JSON(http.StatusOK, gin.H{
		"data": map[string]interface{}{
			"success":     resp.StatusCode >= 200 && resp.StatusCode < 300,
			"status_code": resp.StatusCode,
			"duration":    duration,
			"body":        bodyStr,
		},
	})
}

// ─── UTILS ──────────────────────────────────────────────────────────────────

func generateWebhookSecret() string {
	b := make([]byte, 16)
	rand.Read(b)
	return "whsec_" + hex.EncodeToString(b)
}
