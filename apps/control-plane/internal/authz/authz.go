package authz

import (
	"errors"

	"e2e-platform/apps/control-plane/internal/models"
	"gorm.io/gorm"
)

var ErrNotFound = errors.New("job not found or unauthorized")

func LoadJobForUser(db *gorm.DB, jobID string, userID string) (models.Job, error) {
	var job models.Job
	if err := db.Joins("LEFT JOIN user_profiles ON user_profiles.id = jobs.user_id").
		Where("jobs.id = ? AND (jobs.user_id::text = ? OR user_profiles.clerk_user_id = ?)", jobID, userID, userID).
		First(&job).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return job, ErrNotFound
		}
		return job, err
	}
	return job, nil
}

func UserOwnsJob(db *gorm.DB, jobID string, userID string) bool {
	var count int64
	db.Table("jobs").
		Joins("LEFT JOIN user_profiles ON user_profiles.id = jobs.user_id").
		Where("jobs.id = ? AND (jobs.user_id::text = ? OR user_profiles.clerk_user_id = ?)", jobID, userID, userID).
		Count(&count)
	return count > 0
}
