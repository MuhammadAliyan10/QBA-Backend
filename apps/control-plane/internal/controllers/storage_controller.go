// internal/controllers/storage_controller.go
package controllers

import (
	"log"
	"net/http"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

type StorageController struct {
	db       *gorm.DB
	identity *services.IdentityService
}

func NewStorageController(db *gorm.DB, identity *services.IdentityService) *StorageController {
	return &StorageController{
		db:       db,
		identity: identity,
	}
}

// ─── DTOS ───────────────────────────────────────────────────────────────────

type RecordAssetRequest struct {
	Type         string `json:"type"`
	Filename     string `json:"filename" binding:"required"`
	FriendlyName string `json:"friendly_name"`
	MimeType     string `json:"mime_type" binding:"required"`
	SizeBytes    int    `json:"size_bytes" binding:"required"`
	AzureBlobUrl string `json:"azure_blob_url" binding:"required"`
	AzureBlobId  string `json:"azure_blob_id" binding:"required"`
}

type StorageAssetResponse struct {
	ID           string `json:"id"`
	Type         string `json:"type"`
	Filename     string `json:"filename"`
	FriendlyName string `json:"friendly_name,omitempty"`
	MimeType     string `json:"mime_type"`
	SizeBytes    int    `json:"size_bytes"`
	AzureBlobUrl string `json:"azure_blob_url"`
	CreatedAt    string `json:"created_at"`
}

// ─── HANDLERS ───────────────────────────────────────────────────────────────

func (c *StorageController) HandleList(ctx *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(ctx, c.identity)
	if !ok {
		return
	}

	var assets []models.StorageAsset
	if err := c.db.Where("user_id = ?", tenantID).Order("created_at DESC").Find(&assets).Error; err != nil {
		log.Printf("[StorageController] HandleList error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch assets"})
		return
	}

	var response []StorageAssetResponse
	for _, a := range assets {
		var friendlyName string
		if a.FriendlyName != nil {
			friendlyName = *a.FriendlyName
		}
		response = append(response, StorageAssetResponse{
			ID:           a.ID,
			Type:         a.Type,
			Filename:     a.Filename,
			FriendlyName: friendlyName,
			MimeType:     a.MimeType,
			SizeBytes:    a.SizeBytes,
			AzureBlobUrl: a.AzureBlobURL,
			CreatedAt:    a.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
		})
	}

	if response == nil {
		response = make([]StorageAssetResponse, 0)
	}

	ctx.JSON(http.StatusOK, gin.H{"data": response})
}

func (c *StorageController) HandleRecord(ctx *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(ctx, c.identity)
	if !ok {
		return
	}

	var req RecordAssetRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request payload"})
		return
	}

	assetType := req.Type
	if assetType == "" {
		assetType = "INPUT"
	}

	var friendlyNamePtr *string
	if req.FriendlyName != "" {
		friendlyNamePtr = &req.FriendlyName
	}

	asset := models.StorageAsset{
		UserID:       tenantID,
		Type:         assetType,
		Filename:     req.Filename,
		FriendlyName: friendlyNamePtr,
		MimeType:     req.MimeType,
		SizeBytes:    req.SizeBytes,
		AzureBlobURL: req.AzureBlobUrl,
		AzureBlobID:  req.AzureBlobId,
	}

	if err := c.db.Create(&asset).Error; err != nil {
		log.Printf("[StorageController] HandleRecord error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to record asset"})
		return
	}

	var friendlyName string
	if asset.FriendlyName != nil {
		friendlyName = *asset.FriendlyName
	}

	ctx.JSON(http.StatusOK, gin.H{
		"data": StorageAssetResponse{
			ID:           asset.ID,
			Type:         asset.Type,
			Filename:     asset.Filename,
			FriendlyName: friendlyName,
			MimeType:     asset.MimeType,
			SizeBytes:    asset.SizeBytes,
			AzureBlobUrl: asset.AzureBlobURL,
			CreatedAt:    asset.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
		},
	})
}

func (c *StorageController) HandleDelete(ctx *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(ctx, c.identity)
	if !ok {
		return
	}

	assetID := ctx.Param("id")
	if assetID == "" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Missing asset ID"})
		return
	}

	// TODO: Add actual R2/Azure Blob deletion here in the future
	// For now, we only delete the DB record, mimicking the current frontend logic

	result := c.db.Where("id = ? AND user_id = ?", assetID, tenantID).Delete(&models.StorageAsset{})
	if result.Error != nil {
		log.Printf("[StorageController] Delete error: %v", result.Error)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to delete asset"})
		return
	}

	if result.RowsAffected == 0 {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Asset not found"})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{"status": "deleted"})
}
