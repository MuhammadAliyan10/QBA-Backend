package controllers

import (
	"encoding/json"
	"net/http"
	"time"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/gorm"
)

type VaultUploadRequest struct {
	TargetURL    string                 `json:"target_url" binding:"required"`
	SessionState map[string]interface{} `json:"session_state" binding:"required"`
	Alias        string                 `json:"alias"`
	ExpiresIn    *int                   `json:"expires_in_days,omitempty"`
}

type VaultController struct {
	db     *gorm.DB
	crypto   *services.VaultCryptoService
	identity *services.IdentityService
}

func NewVaultController(db *gorm.DB, identity *services.IdentityService) *VaultController {
	return &VaultController{
		db:       db,
		crypto:   services.GetVaultCryptoService(),
		identity: identity,
	}
}

// HandleUploadSession handles POST /v1/vault/sessions.
// It encrypts the browser session state and stores it in the multi-tenant vault.
func (vc *VaultController) HandleUploadSession(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, vc.identity)
	if !ok {
		return
	}

	var req VaultUploadRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_payload", "details": err.Error()})
		return
	}

	// 1. Serialize session JSON
	sessionJSON, err := json.Marshal(req.SessionState)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "serialization_failed"})
		return
	}

	// 2. Encrypt using AES-256-GCM
	encrypted, err := vc.crypto.Encrypt(sessionJSON)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "encryption_failed"})
		return
	}

	// 3. Set expiration (default 30 days)
	days := 30
	if req.ExpiresIn != nil {
		days = *req.ExpiresIn
	}
	expiresAt := time.Now().AddDate(0, 0, days)

	// 4. Persist to database
	vaultSession := models.VaultSession{
		ID:             uuid.New().String(),
		UserID:         tenantID,
		Name:           req.Alias,
		TargetURL:      req.TargetURL,
		EncryptedState: encrypted,
		ExpiresAt:      expiresAt,
	}

	if err := vc.db.Create(&vaultSession).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "database_persistence_failed"})
		return
	}

	c.JSON(http.StatusCreated, gin.H{
		"vault_id":   vaultSession.ID,
		"name":       vaultSession.Name,
		"target_url": vaultSession.TargetURL,
		"expires_at": vaultSession.ExpiresAt,
		"status":     "securely_vaulted",
	})
}

// HandleListSessions handles GET /v1/vault/sessions.
// It returns all encrypted session metadata for the current user.
func (vc *VaultController) HandleListSessions(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, vc.identity)
	if !ok {
		return
	}

	var sessions []models.VaultSession
	if err := vc.db.Where("user_id = ?", tenantID).Order("created_at desc").Find(&sessions).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "database_fetch_failed"})
		return
	}

	// Transform for response
	type sessionResponse struct {
		ID        string    `json:"id"`
		TargetURL string    `json:"target_url"`
		CreatedAt time.Time `json:"created_at"`
		ExpiresAt time.Time `json:"expires_at"`
	}

	resp := make([]sessionResponse, len(sessions))
	for i, s := range sessions {
		resp[i] = sessionResponse{
			ID:        s.ID,
			TargetURL: s.TargetURL,
			CreatedAt: s.CreatedAt,
			ExpiresAt: s.ExpiresAt,
		}
	}

	c.JSON(http.StatusOK, gin.H{
		"sessions": resp,
		"count":    len(resp),
	})
}

// HandleDeleteSession handles DELETE /v1/vault/sessions/:id.
// It removes the encrypted session from the vault after verifying ownership.
func (vc *VaultController) HandleDeleteSession(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, vc.identity)
	if !ok {
		return
	}

	sessionID := c.Param("id")
	if sessionID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing_id"})
		return
	}

	result := vc.db.Where("id = ? AND user_id = ?", sessionID, tenantID).Delete(&models.VaultSession{})
	if result.Error != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "database_delete_failed"})
		return
	}

	if result.RowsAffected == 0 {
		c.JSON(http.StatusNotFound, gin.H{"error": "not_found_or_access_denied"})
		return
	}

	c.JSON(http.StatusOK, gin.H{"deleted": sessionID})
}

