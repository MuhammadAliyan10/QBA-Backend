// internal/controllers/clerk_webhook_controller.go
package controllers

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// ─── CLERK WEBHOOK PAYLOADS ─────────────────────────────────────────────────

type clerkWebhookPayload struct {
	Type string          `json:"type"`
	Data json.RawMessage `json:"data"`
}

type clerkUserData struct {
	ID             string             `json:"id"`
	FirstName      *string            `json:"first_name"`
	LastName       *string            `json:"last_name"`
	ImageURL       *string            `json:"image_url"`
	EmailAddresses []clerkEmailEntry  `json:"email_addresses"`
	CreatedAt      int64              `json:"created_at"`
}

type clerkEmailEntry struct {
	EmailAddress string `json:"email_address"`
	ID           string `json:"id"`
}

// ─── CONTROLLER ─────────────────────────────────────────────────────────────

// ClerkWebhookController handles Clerk webhook events for deterministic user provisioning.
// This controller is registered on the PUBLIC (unauthenticated) router group.
// Authentication is handled via Clerk's webhook signature verification (HMAC-SHA256).
type ClerkWebhookController struct {
	db       *gorm.DB
	identity *services.IdentityService
}

// NewClerkWebhookController creates a new ClerkWebhookController.
func NewClerkWebhookController(db *gorm.DB, identity *services.IdentityService) *ClerkWebhookController {
	return &ClerkWebhookController{
		db:       db,
		identity: identity,
	}
}

// HandleWebhook processes incoming Clerk webhook events.
// Supported events:
//   - user.created  → INSERT user_profiles + user_usage (idempotent via ON CONFLICT DO NOTHING)
//   - user.updated  → UPDATE user_profiles
//   - user.deleted  → DELETE user_profiles (cascade)
func (cwc *ClerkWebhookController) HandleWebhook(c *gin.Context) {
	// 1. Read and verify the request body
	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		log.Printf("[ClerkWebhook] Failed to read request body: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{"error": "failed to read body"})
		return
	}

	// 2. Verify webhook signature
	if !cwc.verifySignature(c.Request.Header, body) {
		log.Println("[ClerkWebhook] REJECT: Invalid webhook signature")
		c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid signature"})
		return
	}

	// 3. Parse the event
	var payload clerkWebhookPayload
	if err := json.Unmarshal(body, &payload); err != nil {
		log.Printf("[ClerkWebhook] Failed to parse webhook payload: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid payload"})
		return
	}

	log.Printf("[ClerkWebhook] Received event: %s", payload.Type)

	// 4. Route to handler
	switch payload.Type {
	case "user.created":
		cwc.handleUserCreated(c, payload.Data)
	case "user.updated":
		cwc.handleUserUpdated(c, payload.Data)
	case "user.deleted":
		cwc.handleUserDeleted(c, payload.Data)
	default:
		log.Printf("[ClerkWebhook] Ignoring unhandled event type: %s", payload.Type)
		c.JSON(http.StatusOK, gin.H{"status": "ignored", "type": payload.Type})
	}
}

// ─── EVENT HANDLERS ─────────────────────────────────────────────────────────

func (cwc *ClerkWebhookController) handleUserCreated(c *gin.Context, data json.RawMessage) {
	var userData clerkUserData
	if err := json.Unmarshal(data, &userData); err != nil {
		log.Printf("[ClerkWebhook] Failed to parse user.created data: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid user data"})
		return
	}

	if userData.ID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing user id"})
		return
	}

	email := cwc.extractPrimaryEmail(userData.EmailAddresses)
	if email == "" {
		log.Printf("[ClerkWebhook] user.created for %s has no email — skipping", userData.ID)
		c.JSON(http.StatusBadRequest, gin.H{"error": "no email address"})
		return
	}

	// Idempotent INSERT — ON CONFLICT DO NOTHING handles Clerk's at-least-once delivery
	profile := models.UserProfile{
		ClerkUserID: userData.ID,
		Email:       email,
		FirstName:   userData.FirstName,
		LastName:    userData.LastName,
		AvatarURL:   userData.ImageURL,
		Tier:        "FREE",
	}

	result := cwc.db.
		Where("clerk_user_id = ?", userData.ID).
		FirstOrCreate(&profile)

	if result.Error != nil {
		log.Printf("[ClerkWebhook] Failed to create user profile for %s: %v", userData.ID, result.Error)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "failed to create profile"})
		return
	}

	// Create initial usage record (idempotent)
	usage := models.UserUsage{
		UserID:         profile.ID,
		CreditsBalance: 100, // Initial free credits
	}
	cwc.db.Where("user_id = ?", profile.ID).FirstOrCreate(&usage)

	if result.RowsAffected > 0 {
		log.Printf("[ClerkWebhook] ✓ Created UserProfile for %s (db_id=%s, email=%s)", userData.ID, profile.ID, email)
	} else {
		log.Printf("[ClerkWebhook] ✓ UserProfile already exists for %s (idempotent)", userData.ID)
	}

	// Invalidate identity cache so subsequent requests see the new user
	cwc.identity.InvalidateCache(userData.ID)

	c.JSON(http.StatusOK, gin.H{"status": "created", "user_id": profile.ID})
}

func (cwc *ClerkWebhookController) handleUserUpdated(c *gin.Context, data json.RawMessage) {
	var userData clerkUserData
	if err := json.Unmarshal(data, &userData); err != nil {
		log.Printf("[ClerkWebhook] Failed to parse user.updated data: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid user data"})
		return
	}

	if userData.ID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing user id"})
		return
	}

	email := cwc.extractPrimaryEmail(userData.EmailAddresses)

	updates := map[string]interface{}{
		"updated_at": time.Now(),
	}
	if userData.FirstName != nil {
		updates["first_name"] = *userData.FirstName
	}
	if userData.LastName != nil {
		updates["last_name"] = *userData.LastName
	}
	if userData.ImageURL != nil {
		updates["avatar_url"] = *userData.ImageURL
	}
	if email != "" {
		updates["email"] = email
	}

	result := cwc.db.
		Model(&models.UserProfile{}).
		Where("clerk_user_id = ?", userData.ID).
		Updates(updates)

	if result.Error != nil {
		log.Printf("[ClerkWebhook] Failed to update user %s: %v", userData.ID, result.Error)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "failed to update profile"})
		return
	}

	cwc.identity.InvalidateCache(userData.ID)

	log.Printf("[ClerkWebhook] ✓ Updated UserProfile for %s (%d rows affected)", userData.ID, result.RowsAffected)
	c.JSON(http.StatusOK, gin.H{"status": "updated"})
}

func (cwc *ClerkWebhookController) handleUserDeleted(c *gin.Context, data json.RawMessage) {
	// Clerk sends minimal data on delete — just the ID
	var deleteData struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(data, &deleteData); err != nil || deleteData.ID == "" {
		log.Printf("[ClerkWebhook] Failed to parse user.deleted data: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid delete data"})
		return
	}

	// Soft-approach: We don't cascade-delete user data here.
	// Instead, we mark the profile as deleted and let a background job handle cleanup.
	// For now, log the event.
	log.Printf("[ClerkWebhook] ⚠ user.deleted event for %s — manual cleanup may be required", deleteData.ID)

	cwc.identity.InvalidateCache(deleteData.ID)
	c.JSON(http.StatusOK, gin.H{"status": "acknowledged", "clerk_id": deleteData.ID})
}

// ─── HELPERS ────────────────────────────────────────────────────────────────

func (cwc *ClerkWebhookController) extractPrimaryEmail(emails []clerkEmailEntry) string {
	if len(emails) == 0 {
		return ""
	}
	return emails[0].EmailAddress
}

// verifySignature verifies the Clerk webhook signature using HMAC-SHA256.
// Clerk sends the signature in the Svix-Signature header.
// Format: "v1,<base64-hmac>"
func (cwc *ClerkWebhookController) verifySignature(headers http.Header, body []byte) bool {
	webhookSecret := os.Getenv("CLERK_WEBHOOK_SECRET")
	if webhookSecret == "" {
		log.Println("[ClerkWebhook] WARNING: CLERK_WEBHOOK_SECRET not set — skipping signature verification in development")
		return true // Allow in dev; MUST be set in production
	}

	// Clerk uses Svix for webhook delivery
	svixID := headers.Get("Svix-Id")
	svixTimestamp := headers.Get("Svix-Timestamp")
	svixSignature := headers.Get("Svix-Signature")

	if svixID == "" || svixTimestamp == "" || svixSignature == "" {
		return false
	}

	// Construct the signed content: "{svix_id}.{svix_timestamp}.{body}"
	signedContent := fmt.Sprintf("%s.%s.%s", svixID, svixTimestamp, string(body))

	// The secret comes as "whsec_<base64>" — strip the prefix
	secretKey := strings.TrimPrefix(webhookSecret, "whsec_")

	// Compute HMAC-SHA256
	mac := hmac.New(sha256.New, []byte(secretKey))
	mac.Write([]byte(signedContent))
	expectedSig := hex.EncodeToString(mac.Sum(nil))

	// Svix-Signature can contain multiple signatures separated by spaces: "v1,<sig1> v1,<sig2>"
	signatures := strings.Split(svixSignature, " ")
	for _, sig := range signatures {
		parts := strings.SplitN(sig, ",", 2)
		if len(parts) == 2 && parts[0] == "v1" {
			if hmac.Equal([]byte(parts[1]), []byte(expectedSig)) {
				return true
			}
		}
	}

	return false
}
