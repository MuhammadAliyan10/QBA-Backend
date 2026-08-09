// internal/controllers/api_key_controller.go
package controllers

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log"
	"net/http"
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
	apiKeyNameMaxLen = 100
	apiKeyNameMinLen = 3
)

// ─── TYPES ───────────────────────────────────────────────────────────────────

type ApiKeyController struct {
	db       *gorm.DB
	identity *services.IdentityService
}

func NewApiKeyController(db *gorm.DB, identity *services.IdentityService) *ApiKeyController {
	return &ApiKeyController{db: db, identity: identity}
}

// ─── DTOS ───────────────────────────────────────────────────────────────────

type CreateApiKeyRequest struct {
	Name string `json:"name" binding:"required"`
}

type ApiKeyResponse struct {
	ID         string  `json:"id"`
	Name       string  `json:"name"`
	KeyPrefix  string  `json:"key_prefix"`
	Status     string  `json:"status"` // "active" | "revoked" | "expired"
	CreatedAt  string  `json:"created_at"`
	LastUsedAt *string `json:"last_used_at,omitempty"`
	ExpiresAt  *string `json:"expires_at,omitempty"`
}

type CreateApiKeyResponse struct {
	ID   string `json:"id"`
	Key  string `json:"key"` // Raw key — returned EXACTLY ONCE, never stored
	Name string `json:"name"`
}

// ─── HELPERS ─────────────────────────────────────────────────────────────────

// resolveKeyStatus returns the display status for an API key.
// Priority: revoked → expired → active.
func resolveKeyStatus(k models.ApiKey) string {
	if !k.IsActive {
		return "revoked"
	}
	if k.ExpiresAt != nil && k.ExpiresAt.Before(time.Now()) {
		return "expired"
	}
	return "active"
}

// generateAPIKeyMaterial creates a new sk_live_ key and returns (rawKey, keyPrefix, keyHash, error).
// Uses crypto/rand for CSPRNG-backed entropy.
func generateAPIKeyMaterial() (rawKey, keyPrefix, keyHash string, err error) {
	rawBytes := make([]byte, 24) // 192 bits of entropy (24 bytes raw → 48 hex chars)
	if _, err = rand.Read(rawBytes); err != nil {
		return
	}
	rawKey = fmt.Sprintf("sk_live_%s", hex.EncodeToString(rawBytes))
	keyPrefix = rawKey[:20] // "sk_live_" (8) + 12 hex chars
	hash := sha256.Sum256([]byte(rawKey))
	keyHash = hex.EncodeToString(hash[:])
	return
}

// ─── HANDLERS ───────────────────────────────────────────────────────────────

// HandleList returns all API keys for the authenticated user.
// GET /v1/api-keys
func (ctrl *ApiKeyController) HandleList(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	var keys []models.ApiKey
	if err := ctrl.db.
		Where("user_id = ?", tenantID).
		Order("created_at DESC").
		Find(&keys).Error; err != nil {
		log.Printf("[ApiKeyController] HandleList DB error | TenantID=%s | Err=%v", tenantID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to retrieve API keys",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	response := make([]ApiKeyResponse, 0, len(keys))
	for _, k := range keys {
		r := ApiKeyResponse{
			ID:        k.ID,
			Name:      k.Name,
			KeyPrefix: k.KeyPrefix,
			Status:    resolveKeyStatus(k),
			CreatedAt: k.CreatedAt.UTC().Format(time.RFC3339),
		}
		if k.LastUsedAt != nil {
			s := k.LastUsedAt.UTC().Format(time.RFC3339)
			r.LastUsedAt = &s
		}
		if k.ExpiresAt != nil {
			s := k.ExpiresAt.UTC().Format(time.RFC3339)
			r.ExpiresAt = &s
		}
		response = append(response, r)
	}

	c.JSON(http.StatusOK, gin.H{"data": response})
}

// HandleCreate creates a new API key for the authenticated user.
// POST /v1/api-keys → 201 Created
func (ctrl *ApiKeyController) HandleCreate(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	var req CreateApiKeyRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":      "invalid_request",
			"message":    "Request body is required and must contain a 'name' field",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	// Sanitize and validate name
	req.Name = strings.TrimSpace(req.Name)
	if len(req.Name) < apiKeyNameMinLen {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"error":      "validation_error",
			"message":    fmt.Sprintf("Key name must be at least %d characters", apiKeyNameMinLen),
			"field":      "name",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}
	if len(req.Name) > apiKeyNameMaxLen {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"error":      "validation_error",
			"message":    fmt.Sprintf("Key name must not exceed %d characters", apiKeyNameMaxLen),
			"field":      "name",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	rawKey, keyPrefix, keyHash, err := generateAPIKeyMaterial()
	if err != nil {
		log.Printf("[ApiKeyController] CSPRNG failure: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "key_generation_failed",
			"message":    "Failed to generate cryptographic key material",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	apiKey := models.ApiKey{
		UserID:    tenantID,
		Name:      req.Name,
		KeyPrefix: keyPrefix,
		KeyHash:   keyHash,
		IsActive:  true,
	}

	if err := ctrl.db.Create(&apiKey).Error; err != nil {
		log.Printf("[ApiKeyController] Create DB error | TenantID=%s | Err=%v", tenantID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to persist API key",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	log.Printf("[ApiKeyController] Created API key | ID=%s | TenantID=%s | Prefix=%s", apiKey.ID, tenantID, keyPrefix)

	// Return raw key EXACTLY ONCE — it is NOT stored and CANNOT be recovered.
	// HTTP 201 Created is the correct status code for a successful resource creation.
	c.JSON(http.StatusCreated, gin.H{
		"data": CreateApiKeyResponse{
			ID:   apiKey.ID,
			Key:  rawKey,
			Name: apiKey.Name,
		},
	})
}

// HandleRevoke permanently revokes an active API key.
// DELETE /v1/api-keys/:id
func (ctrl *ApiKeyController) HandleRevoke(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	keyID := strings.TrimSpace(c.Param("id"))
	if keyID == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":      "missing_parameter",
			"message":    "API key ID is required in the URL path",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	// Fetch first so we can differentiate "not found" from "already revoked"
	var existing models.ApiKey
	if err := ctrl.db.
		Where("id = ? AND user_id = ?", keyID, tenantID).
		First(&existing).Error; err != nil {
		if err == gorm.ErrRecordNotFound {
			c.JSON(http.StatusNotFound, gin.H{
				"error":      "api_key_not_found",
				"message":    "No API key found with the given ID for your account",
				"request_id": middleware.GetRequestID(c),
			})
			return
		}
		log.Printf("[ApiKeyController] HandleRevoke DB fetch error | KeyID=%s | Err=%v", keyID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to look up API key",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	if !existing.IsActive {
		c.JSON(http.StatusConflict, gin.H{
			"error":      "api_key_already_revoked",
			"message":    "This API key has already been revoked",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	if err := ctrl.db.Model(&models.ApiKey{}).
		Where("id = ?", keyID).
		Update("is_active", false).Error; err != nil {
		log.Printf("[ApiKeyController] HandleRevoke update error | KeyID=%s | Err=%v", keyID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to revoke API key",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	log.Printf("[ApiKeyController] Revoked API key | ID=%s | TenantID=%s", keyID, tenantID)
	c.JSON(http.StatusOK, gin.H{
		"status":     "revoked",
		"id":         keyID,
		"request_id": middleware.GetRequestID(c),
	})
}

// HandleRotate atomically revokes the current key and issues a new one.
// The new raw key is returned exactly once — it cannot be retrieved again.
// POST /v1/api-keys/:id/rotate
func (ctrl *ApiKeyController) HandleRotate(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, ctrl.identity)
	if !ok {
		return
	}

	keyID := strings.TrimSpace(c.Param("id"))
	if keyID == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":      "missing_parameter",
			"message":    "API key ID is required in the URL path",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	var existingKey models.ApiKey
	if err := ctrl.db.
		Where("id = ? AND user_id = ? AND is_active = ?", keyID, tenantID, true).
		First(&existingKey).Error; err != nil {
		if err == gorm.ErrRecordNotFound {
			c.JSON(http.StatusNotFound, gin.H{
				"error":      "api_key_not_found",
				"message":    "No active API key found with the given ID for your account",
				"request_id": middleware.GetRequestID(c),
			})
			return
		}
		log.Printf("[ApiKeyController] HandleRotate DB fetch error | KeyID=%s | Err=%v", keyID, err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "database_error",
			"message":    "Failed to look up API key for rotation",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	rawKey, keyPrefix, keyHash, err := generateAPIKeyMaterial()
	if err != nil {
		log.Printf("[ApiKeyController] Rotate CSPRNG failure: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "key_generation_failed",
			"message":    "Failed to generate new cryptographic key material",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	newKey := models.ApiKey{
		UserID:    tenantID,
		Name:      existingKey.Name,
		KeyPrefix: keyPrefix,
		KeyHash:   keyHash,
		IsActive:  true,
	}

	// Atomic rotation: revoke old + insert new in a single DB transaction.
	// If either operation fails, both are rolled back — no key is lost or double-created.
	txErr := ctrl.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&models.ApiKey{}).
			Where("id = ?", existingKey.ID).
			Update("is_active", false).Error; err != nil {
			return fmt.Errorf("revoke old key: %w", err)
		}
		return tx.Create(&newKey).Error
	})

	if txErr != nil {
		log.Printf("[ApiKeyController] Rotate transaction error | OldKeyID=%s | Err=%v", keyID, txErr)
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":      "rotation_failed",
			"message":    "Failed to rotate API key. The old key remains active. Please try again.",
			"request_id": middleware.GetRequestID(c),
		})
		return
	}

	log.Printf("[ApiKeyController] Rotated API key | OldID=%s | NewID=%s | TenantID=%s", existingKey.ID, newKey.ID, tenantID)

	c.JSON(http.StatusOK, gin.H{
		"data": CreateApiKeyResponse{
			ID:   newKey.ID,
			Key:  rawKey,
			Name: newKey.Name,
		},
		"revoked_id": existingKey.ID,
		"request_id": middleware.GetRequestID(c),
	})
}
