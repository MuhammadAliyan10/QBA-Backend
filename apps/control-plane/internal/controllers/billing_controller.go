// internal/controllers/billing_controller.go
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

type BillingController struct {
	db       *gorm.DB
	identity *services.IdentityService
}

func NewBillingController(db *gorm.DB, identity *services.IdentityService) *BillingController {
	return &BillingController{
		db:       db,
		identity: identity,
	}
}

// ─── DTOS ───────────────────────────────────────────────────────────────────

type TransactionRecord struct {
	ID           string `json:"id"`
	Type         string `json:"type"`
	Amount       int    `json:"amount"`
	BalanceAfter int    `json:"balance_after"`
	Description  string `json:"description"`
	CreatedAt    string `json:"created_at"`
}

type SpendByDay struct {
	Date   string `json:"date"`
	Amount int    `json:"amount"`
}

type BillingDataResponse struct {
	Balance          int                 `json:"balance"`
	TotalCreditsUsed int                 `json:"total_credits_used"`
	TotalJobsRun     int                 `json:"total_jobs_run"`
	Plan             string              `json:"plan"`
	RenewalDate      *string             `json:"renewal_date"`
	Transactions     []TransactionRecord `json:"transactions"`
	SpendByDay       []SpendByDay        `json:"spend_by_day"`
}

// ─── HANDLERS ───────────────────────────────────────────────────────────────

func (c *BillingController) HandleGetBilling(ctx *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(ctx, c.identity)
	if !ok {
		return
	}

	var usage models.UserUsage
	if err := c.db.Where("user_id = ?", tenantID).First(&usage).Error; err != nil {
		// Just provide default 0s if usage missing (handled by webhook, but safe fallback)
		usage = models.UserUsage{}
	}

	var profile models.UserProfile
	if err := c.db.Select("tier").Where("id = ?", tenantID).First(&profile).Error; err != nil {
		profile.Tier = "FREE"
	}

	var transactions []models.CreditTransaction
	c.db.Where("user_id = ?", tenantID).Order("created_at DESC").Limit(50).Find(&transactions)

	// Calculate spend by day for the last 30 days
	thirtyDaysAgo := time.Now().Add(-30 * 24 * time.Hour)
	var recentTx []models.CreditTransaction
	c.db.Where("user_id = ? AND type = ? AND created_at >= ?", tenantID, "DEBIT", thirtyDaysAgo).Find(&recentTx)

	spendByDayMap := make(map[string]int)
	for i := 29; i >= 0; i-- {
		d := time.Now().AddDate(0, 0, -i)
		dateStr := d.Format("2006-01-02")
		spendByDayMap[dateStr] = 0
	}

	for _, tx := range recentTx {
		dateStr := tx.CreatedAt.Format("2006-01-02")
		// DEBIT amount should be converted to positive for display
		val := tx.Amount
		if val < 0 {
			val = -val
		}
		spendByDayMap[dateStr] += val
	}

	var spendByDay []SpendByDay
	// Build sorted array based on the generated last 30 days to ensure order
	for i := 29; i >= 0; i-- {
		d := time.Now().AddDate(0, 0, -i)
		dateStr := d.Format("2006-01-02")
		spendByDay = append(spendByDay, SpendByDay{
			Date:   dateStr,
			Amount: spendByDayMap[dateStr],
		})
	}

	var txResponse []TransactionRecord
	for _, tx := range transactions {
		txResponse = append(txResponse, TransactionRecord{
			ID:           tx.ID,
			Type:         tx.Type,
			Amount:       tx.Amount,
			BalanceAfter: tx.BalanceAfter,
			Description:  tx.Description,
			CreatedAt:    tx.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
		})
	}
	if txResponse == nil {
		txResponse = make([]TransactionRecord, 0)
	}

	resp := BillingDataResponse{
		Balance:          usage.CreditsBalance,
		TotalCreditsUsed: usage.TotalCreditsUsed,
		TotalJobsRun:     usage.TotalJobsRun,
		Plan:             profile.Tier,
		RenewalDate:      nil,
		Transactions:     txResponse,
		SpendByDay:       spendByDay,
	}

	ctx.JSON(http.StatusOK, gin.H{"data": resp})
}

func (c *BillingController) HandleGetTransactions(ctx *gin.Context) {
	tenantID, ok := middleware.ResolveTenantID(ctx, c.identity)
	if !ok {
		return
	}

	limitStr := ctx.Query("limit")
	limit := 50
	if parsed, err := strconv.Atoi(limitStr); err == nil && parsed > 0 && parsed <= 500 {
		limit = parsed
	}

	var transactions []models.CreditTransaction
	if err := c.db.Where("user_id = ?", tenantID).Order("created_at DESC").Limit(limit).Find(&transactions).Error; err != nil {
		log.Printf("[BillingController] HandleGetTransactions error: %v", err)
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch transactions"})
		return
	}

	var txResponse []TransactionRecord
	for _, tx := range transactions {
		txResponse = append(txResponse, TransactionRecord{
			ID:           tx.ID,
			Type:         tx.Type,
			Amount:       tx.Amount,
			BalanceAfter: tx.BalanceAfter,
			Description:  tx.Description,
			CreatedAt:    tx.CreatedAt.Format("2006-01-02T15:04:05Z07:00"),
		})
	}
	if txResponse == nil {
		txResponse = make([]TransactionRecord, 0)
	}

	ctx.JSON(http.StatusOK, gin.H{"data": txResponse})
}
