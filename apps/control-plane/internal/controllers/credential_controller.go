// internal/controllers/credential_controller.go
package controllers

import (
	"bytes"
	"encoding/json"
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

// ─── REQUEST / RESPONSE ──────────────────────────────────────────────────────

type Cookie struct {
	Name     string  `json:"name" binding:"required"`
	Value    string  `json:"value" binding:"required"`
	Domain   string  `json:"domain" binding:"required"`
	Path     string  `json:"path" binding:"required"`
	Expires  float64 `json:"expires" binding:"required"`
	HTTPOnly bool    `json:"httpOnly"`
	Secure   bool    `json:"secure"`
	SameSite string  `json:"sameSite" binding:"required,oneof=Strict Lax None"`
}

type LocalStorageItem struct {
	Name  string `json:"name" binding:"required"`
	Value string `json:"value" binding:"required"`
}

type OriginState struct {
	Origin       string             `json:"origin" binding:"required"`
	LocalStorage []LocalStorageItem `json:"localStorage" binding:"required,dive"`
}

type StorageState struct {
	Cookies []Cookie      `json:"cookies" binding:"required,dive"`
	Origins []OriginState `json:"origins" binding:"required,dive"`
}

type createCredentialRequest struct {
	Name        string          `json:"name"         binding:"required"`
	SessionData json.RawMessage `json:"session_data" binding:"required"`
}

func ValidateNoJS(s *StorageState) bool {
	check := func(str string) bool {
		lower := strings.ToLower(str)
		if strings.Contains(lower, "<script") || strings.Contains(lower, "javascript:") || strings.Contains(lower, "eval(") {
			return false
		}
		return true
	}
	for _, c := range s.Cookies {
		if !check(c.Name) || !check(c.Value) || !check(c.Domain) || !check(c.Path) {
			return false
		}
	}
	for _, o := range s.Origins {
		if !check(o.Origin) {
			return false
		}
		for _, ls := range o.LocalStorage {
			if !check(ls.Name) || !check(ls.Value) {
				return false
			}
		}
	}
	return true
}

type credentialSummary struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	CreatedAt time.Time `json:"created_at"`
}

// ─── CONTROLLER ──────────────────────────────────────────────────────────────

// CredentialController manages encrypted Playwright session storage.
type CredentialController struct {
	db       *gorm.DB
	crypto   *services.CryptoService
	identity *services.IdentityService
}

// NewCredentialController wires up the controller with the shared DB and singleton CryptoService.
func NewCredentialController(db *gorm.DB, identity *services.IdentityService) *CredentialController {
	return &CredentialController{
		db:       db,
		crypto:   services.GetCryptoService(),
		identity: identity,
	}
}

// HandleCreate handles POST /v1/credentials.
// Accepts { "name": "...", "session_data": { ... } }, encrypts, and persists.
func (cc *CredentialController) HandleCreate(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, cc.identity)
	if !ok {
		return
	}

	var req createCredentialRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "invalid_request",
			"message": "Fields 'name' and 'session_data' are required",
			"details": err.Error(),
		})
		return
	}

	// BYOS Schema Validation
	dec := json.NewDecoder(bytes.NewReader(req.SessionData))
	dec.DisallowUnknownFields()

	var state StorageState
	if err := dec.Decode(&state); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "schema_violation",
			"message": "Invalid session_data payload. Must conform strictly to Playwright storageState.",
			"details": err.Error(),
		})
		return
	}

	// Zero-Trust XSS Shield
	if !ValidateNoJS(&state) {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "security_violation",
			"message": "Malicious payload detected in session_data.",
		})
		return
	}

	plaintext, err := json.Marshal(state)
	if err != nil {
		log.Printf("[CredentialController] JSON marshal error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "serialization_error", "message": "Failed to serialize session_data"})
		return
	}

	encryptedData, err := cc.crypto.Encrypt(plaintext)
	if err != nil {
		log.Printf("[CredentialController] Encryption error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "encryption_error", "message": "Failed to encrypt session data"})
		return
	}

	cred := &models.Credential{
		ClientID:      tenantID,
		Name:          strings.TrimSpace(req.Name),
		EncryptedData: encryptedData,
	}

	if err := cc.db.Create(cred).Error; err != nil {
		log.Printf("[CredentialController] DB create error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "database_error", "message": "Failed to persist credential"})
		return
	}

	c.JSON(http.StatusCreated, gin.H{"id": cred.ID})
}

// HandleList handles GET /v1/credentials.
// Returns id, name, created_at for all credentials belonging to the authenticated client.
func (cc *CredentialController) HandleList(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, cc.identity)
	if !ok {
		return
	}

	var creds []models.Credential
	if err := cc.db.Debug().
		Select("id, name, created_at").
		Where("client_id = ?", tenantID).
		Order("created_at DESC").
		Find(&creds).Error; err != nil {
		log.Printf("[CredentialController] DB list error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "database_error", "message": "Failed to retrieve credentials"})
		return
	}

	summaries := make([]credentialSummary, 0, len(creds))
	for _, cr := range creds {
		summaries = append(summaries, credentialSummary{
			ID:        cr.ID,
			Name:      cr.Name,
			CreatedAt: cr.CreatedAt,
		})
	}

	c.JSON(http.StatusOK, gin.H{"data": summaries})
}

// HandleDelete handles DELETE /v1/credentials/:id.
// Hard-deletes the credential after verifying ownership.
func (cc *CredentialController) HandleDelete(c *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(c, cc.identity)
	if !ok {
		return
	}

	credID := strings.TrimSpace(c.Param("id"))
	if credID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing_id", "message": "Credential ID is required"})
		return
	}

	result := cc.db.
		Where("id = ? AND client_id = ?", credID, tenantID).
		Delete(&models.Credential{})

	if result.Error != nil {
		log.Printf("[CredentialController] DB delete error: %v", result.Error)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "database_error", "message": "Failed to delete credential"})
		return
	}

	if result.RowsAffected == 0 {
		c.JSON(http.StatusNotFound, gin.H{"error": "not_found", "message": "Credential not found or access denied"})
		return
	}

	c.JSON(http.StatusOK, gin.H{"deleted": credID})
}
