// internal/controllers/vault_secret_controller.go
package controllers

import (
	"encoding/base64"
	"errors"
	"log"
	"net/http"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// VaultSecretController handles CRUD operations for Vault Secrets.
// Ensures strict zero-trust tenant isolation and AES-256-GCM encryption.
type VaultSecretController struct {
	db       *gorm.DB
	identity *services.IdentityService
}

func NewVaultSecretController(db *gorm.DB, identity *services.IdentityService) *VaultSecretController {
	return &VaultSecretController{
		db:       db,
		identity: identity,
	}
}

// ─── DTOS ───────────────────────────────────────────────────────────────────

// CreateSecretRequest is the DTO for incoming plaintext secrets.
type CreateSecretRequest struct {
	KeyName     string  `json:"key_name" binding:"required"`
	Value       string  `json:"value" binding:"required"`
	RequiresPin bool    `json:"requires_pin"`
	PinHash     *string `json:"pin_hash"`
}

// VaultSecretMetadata is the DTO for outbound secrets (omits ciphertext).
type VaultSecretMetadata struct {
	ID             string `json:"id"`
	KeyName        string `json:"key_name"`
	RequiresPin    bool   `json:"requires_pin"`
	LastAccessedAt string `json:"last_accessed_at,omitempty"`
	AccessCount    int    `json:"access_count"`
	CreatedAt      string `json:"created_at"`
	UpdatedAt      string `json:"updated_at"`
}

// ─── HANDLERS ───────────────────────────────────────────────────────────────

// HandleList returns metadata for all vault secrets owned by the tenant.
func (c *VaultSecretController) HandleList(ctx *gin.Context) {
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

	// Select only metadata columns to ensure ciphertext never leaves the DB
	var secrets []models.VaultSecret
	if err := c.db.Select("id", "key_name", "requires_pin", "last_accessed_at", "access_count", "created_at", "updated_at").
		Where("user_id = ?", tenantID).
		Order("created_at DESC").
		Find(&secrets).Error; err != nil {
		log.Printf("[VaultSecretController] HandleList error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch vault secrets"})
		return
	}

	var metadata []VaultSecretMetadata
	for _, s := range secrets {
		var lastAccess string
		if s.LastAccessedAt != nil {
			lastAccess = s.LastAccessedAt.Format("2006-01-02T15:04:05Z07:00")
		}
		metadata = append(metadata, VaultSecretMetadata{
			ID:             s.ID,
			KeyName:        s.KeyName,
			RequiresPin:    s.RequiresPin,
			LastAccessedAt: lastAccess,
			AccessCount:    s.AccessCount,
			CreatedAt:      s.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
			UpdatedAt:      s.UpdatedAt.Format("2006-01-02T15:04:05Z07:00"),
		})
	}

	// Return empty array instead of null for empty results
	if metadata == nil {
		metadata = make([]VaultSecretMetadata, 0)
	}

	ctx.JSON(http.StatusOK, gin.H{"data": metadata})
}

// HandleCreate encrypts and stores a new vault secret.
func (c *VaultSecretController) HandleCreate(ctx *gin.Context) {
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

	var req CreateSecretRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request payload"})
		return
	}

	// Cryptographic Chokepoint: Encrypt the plaintext value in-memory
	cryptoSvc := services.GetCryptoService() // AES-256-GCM with nonce prepend
	ciphertext, err := cryptoSvc.Encrypt([]byte(req.Value))
	if err != nil {
		log.Printf("[VaultSecretController] Encryption failed: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to secure secret"})
		return
	}

	// Base64 encode the resulting [nonce|ciphertext] blob for text column storage
	encodedCiphertext := base64.StdEncoding.EncodeToString(ciphertext)

	secret := models.VaultSecret{
		UserID:         tenantID,
		KeyName:        req.KeyName,
		EncryptedValue: encodedCiphertext,
		RequiresPin:    req.RequiresPin,
		PinHash:        req.PinHash,
	}

	// Use FirstOrCreate / Assign to handle upserts safely
	var existing models.VaultSecret
	result := c.db.Where("user_id = ? AND key_name = ?", tenantID, req.KeyName).First(&existing)

	if errors.Is(result.Error, gorm.ErrRecordNotFound) {
		if err := c.db.Create(&secret).Error; err != nil {
			log.Printf("[VaultSecretController] Create error: %v", err)
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to save secret"})
			return
		}
	} else if result.Error == nil {
		// Upsert logic
		if err := c.db.Model(&existing).Updates(map[string]interface{}{
			"encrypted_value": encodedCiphertext,
			"requires_pin":    req.RequiresPin,
			"pin_hash":        req.PinHash,
		}).Error; err != nil {
			log.Printf("[VaultSecretController] Update error: %v", err)
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to update secret"})
			return
		}
		secret = existing
	} else {
		log.Printf("[VaultSecretController] Lookup error: %v", result.Error)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to process secret"})
		return
	}

	// Return ONLY metadata
	ctx.JSON(http.StatusOK, gin.H{
		"data": VaultSecretMetadata{
			ID:          secret.ID,
			KeyName:     secret.KeyName,
			RequiresPin: secret.RequiresPin,
			AccessCount: secret.AccessCount,
			CreatedAt:   secret.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
			UpdatedAt:   secret.UpdatedAt.Format("2006-01-02T15:04:05Z07:00"),
		},
	})
}

// HandleDelete removes a vault secret.
func (c *VaultSecretController) HandleDelete(ctx *gin.Context) {
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

	secretID := ctx.Param("id")
	if secretID == "" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Missing secret ID"})
		return
	}

	result := c.db.Where("id = ? AND user_id = ?", secretID, tenantID).Delete(&models.VaultSecret{})
	if result.Error != nil {
		log.Printf("[VaultSecretController] Delete error: %v", result.Error)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to delete secret"})
		return
	}

	if result.RowsAffected == 0 {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Secret not found"})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{"status": "deleted"})
}
