// internal/controllers/dashboard_controller.go
package controllers

import (
	"log"
	"net/http"
	"strconv"
	"time"

	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

type DashboardController struct {
	db       *gorm.DB
	identity *services.IdentityService
}

func NewDashboardController(db *gorm.DB, identity *services.IdentityService) *DashboardController {
	return &DashboardController{
		db:       db,
		identity: identity,
	}
}

// ─── DTOS ───────────────────────────────────────────────────────────────────

type DailyStat struct {
	Date      string `json:"date"`
	Completed int    `json:"completed"`
	Failed    int    `json:"failed"`
}

type DailyCredit struct {
	Date    string `json:"date"`
	Credits int    `json:"credits"`
}

type DashboardStatsResponse struct {
	TotalJobs         int64            `json:"total_jobs"`
	ActiveJobs        int64            `json:"active_jobs"`
	CompletedJobs     int64            `json:"completed_jobs"`
	FailedJobs        int64            `json:"failed_jobs"`
	TotalCredits      int              `json:"total_credits"`
	SuccessRate       float64          `json:"success_rate"`
	AvgDurationMs     float64          `json:"avg_duration_ms"`
	WorkflowCount     int64            `json:"workflow_count"`
	SecretsCount      int64            `json:"secrets_count"`
	AssetsCount       int64            `json:"assets_count"`
	JobsByStatus      map[string]int64 `json:"jobs_by_status"`
	Last7DaysJobs     []DailyStat      `json:"last_7_days_jobs"`
	Last30DaysCredits []DailyCredit    `json:"last_30_days_credits"`
}

type RecentJobResponse struct {
	ID           string  `json:"id"`
	WorkflowID   string  `json:"workflow_id"`
	WorkflowName string  `json:"workflow_name"`
	Status       string  `json:"status"`
	StartedAt    *string `json:"started_at"`
	CompletedAt  *string `json:"completed_at"`
	DurationMs   *int    `json:"duration_ms"`
	CreditsUsed  int     `json:"credits_used"`
	CreatedAt    string  `json:"created_at"`
}

// ─── HANDLERS ───────────────────────────────────────────────────────────────

func (c *DashboardController) HandleGetStats(ctx *gin.Context) {
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

	var stats DashboardStatsResponse
	stats.JobsByStatus = make(map[string]int64)

	c.db.Model(&models.Job{}).Where("user_id = ?", tenantID).Count(&stats.TotalJobs)
	c.db.Model(&models.Job{}).Where("user_id = ? AND status IN ?", tenantID, []string{"QUEUED", "RUNNING"}).Count(&stats.ActiveJobs)
	c.db.Model(&models.Job{}).Where("user_id = ? AND status = ?", tenantID, "COMPLETED").Count(&stats.CompletedJobs)
	c.db.Model(&models.Job{}).Where("user_id = ? AND status = ?", tenantID, "FAILED").Count(&stats.FailedJobs)

	c.db.Model(&models.Workflow{}).Where("user_id = ?", tenantID).Count(&stats.WorkflowCount)
	c.db.Model(&models.VaultSecret{}).Where("user_id = ?", tenantID).Count(&stats.SecretsCount)
	c.db.Model(&models.StorageAsset{}).Where("user_id = ?", tenantID).Count(&stats.AssetsCount)

	stats.JobsByStatus["QUEUED"] = 0
	stats.JobsByStatus["RUNNING"] = stats.ActiveJobs
	stats.JobsByStatus["COMPLETED"] = stats.CompletedJobs
	stats.JobsByStatus["FAILED"] = stats.FailedJobs
	stats.JobsByStatus["CANCELLED"] = 0

	var sumData struct {
		TotalCredits int
		AvgDuration  float64
	}
	c.db.Model(&models.Job{}).
		Select("COALESCE(SUM(credits_used), 0) as total_credits, COALESCE(AVG(duration_ms), 0) as avg_duration").
		Where("user_id = ? AND status = ?", tenantID, "COMPLETED").
		Scan(&sumData)

	stats.TotalCredits = sumData.TotalCredits
	stats.AvgDurationMs = sumData.AvgDuration

	if stats.TotalJobs > 0 {
		stats.SuccessRate = float64(stats.CompletedJobs) / float64(stats.CompletedJobs+stats.FailedJobs) * 100
	} else {
		stats.SuccessRate = 100
	}

	// Last 7 days jobs
	sevenDaysAgo := time.Now().Add(-7 * 24 * time.Hour)
	var recentJobs []models.Job
	c.db.Select("created_at, status").Where("user_id = ? AND created_at >= ?", tenantID, sevenDaysAgo).Find(&recentJobs)

	jobsByDay := make(map[string]*DailyStat)
	for i := 6; i >= 0; i-- {
		d := time.Now().AddDate(0, 0, -i)
		dateStr := d.Format("2006-01-02")
		jobsByDay[dateStr] = &DailyStat{Date: dateStr, Completed: 0, Failed: 0}
		stats.Last7DaysJobs = append(stats.Last7DaysJobs, *jobsByDay[dateStr])
	}

	for _, j := range recentJobs {
		dateStr := j.CreatedAt.Format("2006-01-02")
		if stat, ok := jobsByDay[dateStr]; ok {
			if j.Status == "COMPLETED" {
				stat.Completed++
			} else if j.Status == "FAILED" {
				stat.Failed++
			}
		}
	}
	// Rebuild slice since we updated pointers
	stats.Last7DaysJobs = nil
	for i := 6; i >= 0; i-- {
		d := time.Now().AddDate(0, 0, -i)
		dateStr := d.Format("2006-01-02")
		stats.Last7DaysJobs = append(stats.Last7DaysJobs, *jobsByDay[dateStr])
	}

	// Last 30 days credits
	thirtyDaysAgo := time.Now().Add(-30 * 24 * time.Hour)
	var recentCompleted []models.Job
	c.db.Select("created_at, credits_used").Where("user_id = ? AND status = ? AND created_at >= ?", tenantID, "COMPLETED", thirtyDaysAgo).Find(&recentCompleted)

	creditsByDay := make(map[string]int)
	for i := 29; i >= 0; i-- {
		d := time.Now().AddDate(0, 0, -i)
		dateStr := d.Format("2006-01-02")
		creditsByDay[dateStr] = 0
	}

	for _, j := range recentCompleted {
		dateStr := j.CreatedAt.Format("2006-01-02")
		creditsByDay[dateStr] += j.CreditsUsed
	}

	for i := 29; i >= 0; i-- {
		d := time.Now().AddDate(0, 0, -i)
		dateStr := d.Format("2006-01-02")
		stats.Last30DaysCredits = append(stats.Last30DaysCredits, DailyCredit{
			Date:    dateStr,
			Credits: creditsByDay[dateStr],
		})
	}

	ctx.JSON(http.StatusOK, gin.H{"data": stats})
}

func (c *DashboardController) HandleGetRecentJobs(ctx *gin.Context) {
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

	limitStr := ctx.Query("limit")
	limit := 10
	if parsed, err := strconv.Atoi(limitStr); err == nil && parsed > 0 && parsed <= 100 {
		limit = parsed
	}

	var jobs []struct {
		ID           string
		WorkflowID   string
		WorkflowName string
		Status       string
		StartedAt    *time.Time
		CompletedAt  *time.Time
		DurationMs   *int
		CreditsUsed  int
		CreatedAt    time.Time
	}

	query := `
		SELECT j.id, j.workflow_id, w.name as workflow_name, j.status, 
		       j.started_at, j.completed_at, j.duration_ms, j.credits_used, j.created_at
		FROM jobs j
		JOIN workflows w ON j.workflow_id = w.id
		WHERE j.user_id = ?
		ORDER BY j.created_at DESC
		LIMIT ?
	`

	if err := c.db.Raw(query, tenantID, limit).Scan(&jobs).Error; err != nil {
		log.Printf("[DashboardController] HandleGetRecentJobs error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch recent jobs"})
		return
	}

	var response []RecentJobResponse
	for _, j := range jobs {
		var startedAt, completedAt *string
		if j.StartedAt != nil {
			s := j.StartedAt.Format("2006-01-02T15:04:05Z07:00")
			startedAt = &s
		}
		if j.CompletedAt != nil {
			s := j.CompletedAt.Format("2006-01-02T15:04:05Z07:00")
			completedAt = &s
		}

		response = append(response, RecentJobResponse{
			ID:           j.ID,
			WorkflowID:   j.WorkflowID,
			WorkflowName: j.WorkflowName,
			Status:       j.Status,
			StartedAt:    startedAt,
			CompletedAt:  completedAt,
			DurationMs:   j.DurationMs,
			CreditsUsed:  j.CreditsUsed,
			CreatedAt:    j.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
		})
	}
	if response == nil {
		response = make([]RecentJobResponse, 0)
	}

	ctx.JSON(http.StatusOK, gin.H{"data": response})
}
