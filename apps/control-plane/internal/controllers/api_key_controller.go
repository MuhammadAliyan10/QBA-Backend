// internal/controllers/api_key_controller.go
package controllers

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log"
	"net/http"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

type ApiKeyController struct {
	db       *gorm.DB
	identity *services.IdentityService
}

func NewApiKeyController(db *gorm.DB, identity *services.IdentityService) *ApiKeyController {
	return &ApiKeyController{
		db:       db,
		identity: identity,
	}
}

// ─── DTOS ───────────────────────────────────────────────────────────────────

type CreateApiKeyRequest struct {
	Name string `json:"name" binding:"required"`
}

type ApiKeyResponse struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	KeyPrefix  string `json:"key_prefix"`
	Status     string `json:"status"`
	CreatedAt  string `json:"created_at"`
	LastUsedAt string `json:"last_used_at,omitempty"`
}

type CreateApiKeyResponse struct {
	ID      string `json:"id"`
	Key     string `json:"key"` // Raw key returned exactly once
	Name    string `json:"name"`
}

// ─── HANDLERS ───────────────────────────────────────────────────────────────

func (c *ApiKeyController) HandleList(ctx *gin.Context) {
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

	var keys []models.ApiKey
	if err := c.db.Where("user_id = ?", tenantID).Order("created_at DESC").Find(&keys).Error; err != nil {
		log.Printf("[ApiKeyController] HandleList error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch API keys"})
		return
	}

	var response []ApiKeyResponse
	for _, k := range keys {
		status := "revoked"
		if k.IsActive {
			status = "active"
		}
		var lastUsed string
		if k.LastUsedAt != nil {
			lastUsed = k.LastUsedAt.Format("2006-01-02T15:04:05Z07:00")
		}
		response = append(response, ApiKeyResponse{
			ID:         k.ID,
			Name:       k.Name,
			KeyPrefix:  k.KeyPrefix,
			Status:     status,
			CreatedAt:  k.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
			LastUsedAt: lastUsed,
		})
	}

	if response == nil {
		response = make([]ApiKeyResponse, 0)
	}

	ctx.JSON(http.StatusOK, gin.H{"data": response})
}

func (c *ApiKeyController) HandleCreate(ctx *gin.Context) {
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

	var req CreateApiKeyRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request payload"})
		return
	}

	// Cryptographic Chokepoint: One-way hash for API keys
	rawBytes := make([]byte, 16) // 16 bytes = 32 hex chars
	if _, err := rand.Read(rawBytes); err != nil {
		log.Printf("[ApiKeyController] Rand read error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to generate key"})
		return
	}

	rawKey := fmt.Sprintf("sk_live_%s", hex.EncodeToString(rawBytes))
	keyPrefix := rawKey[:20] // "sk_live_" + 12 chars

	hash := sha256.Sum256([]byte(rawKey))
	keyHash := hex.EncodeToString(hash[:])

	apiKey := models.ApiKey{
		UserID:    tenantID,
		Name:      req.Name,
		KeyPrefix: keyPrefix,
		KeyHash:   keyHash,
		IsActive:  true,
	}

	if err := c.db.Create(&apiKey).Error; err != nil {
		log.Printf("[ApiKeyController] Create error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to save API key"})
		return
	}

	// Return raw key EXACTLY ONCE
	ctx.JSON(http.StatusOK, gin.H{
		"data": CreateApiKeyResponse{
			ID:   apiKey.ID,
			Key:  rawKey,
			Name: apiKey.Name,
		},
	})
}

func (c *ApiKeyController) HandleRevoke(ctx *gin.Context) {
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

	keyID := ctx.Param("id")
	if keyID == "" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Missing key ID"})
		return
	}

	result := c.db.Model(&models.ApiKey{}).
		Where("id = ? AND user_id = ?", keyID, tenantID).
		Update("is_active", false)

	if result.Error != nil {
		log.Printf("[ApiKeyController] Revoke error: %v", result.Error)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to revoke API key"})
		return
	}

	if result.RowsAffected == 0 {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "API key not found"})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{"status": "revoked"})
}

// HandleRotate atomically revokes the current key and issues a new one with the
// same name. The new raw key is returned exactly once — it cannot be retrieved again.
func (c *ApiKeyController) HandleRotate(ctx *gin.Context) {
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

	keyID := ctx.Param("id")
	if keyID == "" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Missing key ID"})
		return
	}

	// Fetch the existing key to get its name
	var existingKey models.ApiKey
	if err := c.db.Where("id = ? AND user_id = ? AND is_active = ?", keyID, tenantID, true).
		First(&existingKey).Error; err != nil {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "API key not found or already revoked"})
		return
	}

	// Generate new key material
	rawBytes := make([]byte, 16)
	if _, err := rand.Read(rawBytes); err != nil {
		log.Printf("[ApiKeyController] Rotate rand error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to generate new key"})
		return
	}

	rawKey := fmt.Sprintf("sk_live_%s", hex.EncodeToString(rawBytes))
	keyPrefix := rawKey[:20]
	hash := sha256.Sum256([]byte(rawKey))
	keyHash := hex.EncodeToString(hash[:])

	// Atomic rotation: revoke old + create new in a single transaction
	newKey := models.ApiKey{
		UserID:    tenantID,
		Name:      existingKey.Name,
		KeyPrefix: keyPrefix,
		KeyHash:   keyHash,
		IsActive:  true,
	}

	txErr := c.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&models.ApiKey{}).
			Where("id = ?", existingKey.ID).
			Update("is_active", false).Error; err != nil {
			return err
		}
		return tx.Create(&newKey).Error
	})

	if txErr != nil {
		log.Printf("[ApiKeyController] Rotate transaction error: %v", txErr)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to rotate API key"})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{
		"data": CreateApiKeyResponse{
			ID:   newKey.ID,
			Key:  rawKey,
			Name: newKey.Name,
		},
		"revoked_id": existingKey.ID,
	})
}

