// internal/models/storage_asset.go
package models

import "time"

// StorageAsset represents a file stored in Cloudflare R2 (or Azure Blob Storage).
// Metadata only — the actual binary content lives in the object store.
type StorageAsset struct {
	ID           string     `gorm:"primaryKey;type:uuid;column:id;default:gen_random_uuid()" json:"id"`
	UserID       string     `gorm:"column:user_id;type:uuid;index;not null"                  json:"user_id"`
	JobID        *string    `gorm:"column:job_id;type:uuid;index"                            json:"job_id,omitempty"`
	Type         string     `gorm:"column:type;type:asset_type;not null"                     json:"type"`
	Filename     string     `gorm:"column:filename;type:text;not null"                       json:"filename"`
	FriendlyName *string    `gorm:"column:friendly_name;type:text"                           json:"friendly_name,omitempty"`
	MimeType     string     `gorm:"column:mime_type;type:text;not null"                      json:"mime_type"`
	SizeBytes    int        `gorm:"column:size_bytes;not null"                               json:"size_bytes"`
	AzureBlobURL string    `gorm:"column:azure_blob_url;type:text;not null"                  json:"azure_blob_url"`
	AzureBlobID  string    `gorm:"column:azure_blob_id;type:text;not null"                   json:"azure_blob_id"`
	IsPublic     bool       `gorm:"column:is_public;default:false"                           json:"is_public"`
	ExpiresAt    *time.Time `gorm:"column:expires_at"                                        json:"expires_at,omitempty"`
	CreatedAt    time.Time  `gorm:"column:created_at"                                        json:"created_at"`
}

// TableName specifies the table name for GORM.
func (StorageAsset) TableName() string {
	return "storage_assets"
}
