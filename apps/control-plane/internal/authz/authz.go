package authz

import (
	"errors"

	"e2e-platform/apps/control-plane/internal/models"
	"gorm.io/gorm"
)

var ErrNotFound = errors.New("job not found or unauthorized")

func LoadJobForUser(db *gorm.DB, jobID string, userID string) (models.Job, error) {
	var job models.Job
	if err := db.Where("id = ? AND user_id = ?", jobID, userID).First(&job).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return job, ErrNotFound
		}
		return job, err
	}
	return job, nil
}

func UserOwnsJob(db *gorm.DB, jobID string, userID string) bool {
	var count int64
	db.Model(&models.Job{}).Where("id = ? AND user_id = ?", jobID, userID).Count(&count)
	return count > 0
}
