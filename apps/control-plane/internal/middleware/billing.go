package middleware

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/nats-io/nats.go"
	"github.com/redis/go-redis/v9"
)

// ─── BILLING MIDDLEWARE ──────────────────────────────────────────────────────

// BillingMiddleware enforces credit balance checks before workflow execution.
// Uses Redis Lua scripts for atomic DECRBY operations.
//
// FAIL-CLOSED: If Redis is unreachable, the request is DENIED with HTTP 503.
// Running heavy compute without billing verification is unacceptable.
//
// Flow:
//  1. Extract user_id from context (set by AuthMiddleware)
//  2. Check balance in Redis via atomic Lua script
//  3. If balance >= cost: DECRBY atomically → continue
//  4. If balance < cost: Reject with 402 Payment Required
//  5. If Redis is down: Reject with 503 Service Unavailable
//  6. Publish billing event to NATS for async ledger write
type BillingMiddleware struct {
	redis      *redis.Client
	nats       *nats.Conn
	luaScript  *redis.Script
	creditCost int
}

// Lua script for atomic credit deduction with rollback.
// Returns:
//
//	-1: Insufficient balance (operation rolled back)
//	>= 0: New balance after deduction
const luaDeductScript = `
local balance = redis.call('DECRBY', KEYS[1], ARGV[1])
if balance < 0 then
    redis.call('INCRBY', KEYS[1], ARGV[1])  -- Rollback
    return -1
end
return balance
`

// NewBillingMiddleware creates the billing enforcement middleware.
func NewBillingMiddleware(redisClient *redis.Client, natsConn *nats.Conn) *BillingMiddleware {
	creditCost, _ := strconv.Atoi(os.Getenv("CREDIT_PER_JOB"))
	if creditCost == 0 {
		creditCost = 1 // Default: 1 credit per job
	}

	return &BillingMiddleware{
		redis:      redisClient,
		nats:       natsConn,
		luaScript:  redis.NewScript(luaDeductScript),
		creditCost: creditCost,
	}
}

// Middleware returns the Gin middleware function.
// FAIL-CLOSED: No billing verification = no execution.
func (bm *BillingMiddleware) Middleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		// FAIL-CLOSED: If Redis is not available, deny ALL execution.
		if bm.redis == nil {
			log.Printf("[BILLING] REJECT: Redis not configured — billing unavailable | IP=%s", c.ClientIP())
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error":   "Billing service unavailable",
				"message": "Cannot verify credit balance. Service misconfigured.",
				"code":    "BILLING_UNAVAILABLE",
			})
			c.Abort()
			return
		}

		// Extract user ID from context (set by AuthMiddleware).
		userID, exists := GetUserID(c)
		if !exists {
			log.Printf("[BILLING] REJECT: No user identity | IP=%s | Path=%s",
				c.ClientIP(), c.Request.URL.Path)
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Authentication required for billing",
			})
			c.Abort()
			return
		}

		// Check and deduct credits atomically.
		balanceAfter, err := bm.deductCredits(c.Request.Context(), userID)
		if err != nil {
			if err.Error() == "insufficient_balance" {
				log.Printf("[BILLING] REJECT: Insufficient credits | User=%s | IP=%s | Path=%s",
					userID, c.ClientIP(), c.Request.URL.Path)
				c.JSON(http.StatusPaymentRequired, gin.H{
					"error":   "Insufficient credits",
					"message": "Please top up your account to continue",
					"code":    "INSUFFICIENT_CREDITS",
				})
				c.Abort()
				return
			}

			// ── FAIL-CLOSED: Redis is down → deny execution ──────────────
			log.Printf("[BILLING] REJECT: Billing service unavailable | User=%s | IP=%s | Error=%v",
				userID, c.ClientIP(), err)
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error":   "Billing service unavailable",
				"message": "Cannot verify credit balance. Please try again shortly.",
				"code":    "BILLING_UNAVAILABLE",
			})
			c.Abort()
			return
		}

		// ── Success: publish billing event for async ledger persistence ───
		go bm.publishBillingEvent(userID, -bm.creditCost, int(balanceAfter))

		// Store balance in context for response headers.
		c.Set("credits_remaining", balanceAfter)
		c.Header("X-Credits-Remaining", fmt.Sprintf("%d", balanceAfter))

		c.Next()
	}
}

// deductCredits performs atomic credit deduction using Lua script.
func (bm *BillingMiddleware) deductCredits(ctx context.Context, userID string) (int64, error) {
	key := fmt.Sprintf("user:%s:credits", userID)

	result, err := bm.luaScript.Run(
		ctx,
		bm.redis,
		[]string{key},
		bm.creditCost,
	).Result()

	if err != nil {
		return 0, fmt.Errorf("billing redis unavailable: %w", err)
	}

	balance, ok := result.(int64)
	if !ok {
		return 0, fmt.Errorf("unexpected result type: %T", result)
	}

	if balance == -1 {
		return 0, fmt.Errorf("insufficient_balance")
	}

	log.Printf("[BILLING] OK: Deducted %d credits from user %s. Balance: %d",
		bm.creditCost, userID, balance)
	return balance, nil
}

// publishBillingEvent sends a billing event to NATS for async ledger write.
func (bm *BillingMiddleware) publishBillingEvent(userID string, amount int, balanceAfter int) {
	ev := map[string]interface{}{
		"user_id":        userID,
		"amount":         amount,
		"balance_after":  balanceAfter,
		"type":           "DEDUCTION",
		"timestamp":      time.Now().Unix(),
		"transaction_id": uuid.New().String(),
	}
	data, err := json.Marshal(ev)
	if err != nil {
		log.Printf("[BILLING] WARNING: Failed to marshal billing event: %v", err)
		return
	}

	if err := bm.nats.Publish("billing.events", data); err != nil {
		log.Printf("[BILLING] WARNING: Failed to publish billing event: %v", err)
		return
	}
}

// ─── PUBLIC HELPERS ──────────────────────────────────────────────────────────

// GetBalance retrieves the current credit balance for a user.
func (bm *BillingMiddleware) GetBalance(ctx context.Context, userID string) (int64, error) {
	key := fmt.Sprintf("user:%s:credits", userID)

	val, err := bm.redis.Get(ctx, key).Int64()
	if err == redis.Nil {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("redis error: %w", err)
	}

	return val, nil
}

// AddCredits adds credits to a user's balance (used by payment webhook).
func (bm *BillingMiddleware) AddCredits(ctx context.Context, userID string, amount int) (int64, error) {
	key := fmt.Sprintf("user:%s:credits", userID)

	newBalance, err := bm.redis.IncrBy(ctx, key, int64(amount)).Result()
	if err != nil {
		return 0, fmt.Errorf("redis incrby failed: %w", err)
	}

	go bm.publishBillingEvent(userID, amount, int(newBalance))

	log.Printf("[BILLING] Added %d credits to user %s. New balance: %d", amount, userID, newBalance)
	return newBalance, nil
}

// InitializeUserCredits sets default credits for a new user.
func (bm *BillingMiddleware) InitializeUserCredits(ctx context.Context, userID string) error {
	defaultCredits, _ := strconv.Atoi(os.Getenv("DEFAULT_CREDITS"))
	if defaultCredits == 0 {
		defaultCredits = 100
	}

	key := fmt.Sprintf("user:%s:credits", userID)

	set, err := bm.redis.SetNX(ctx, key, defaultCredits, 0).Result()
	if err != nil {
		return fmt.Errorf("redis setnx failed: %w", err)
	}

	if set {
		log.Printf("[BILLING] Initialized %d credits for new user %s", defaultCredits, userID)
		go bm.publishBillingEvent(userID, defaultCredits, defaultCredits)
	}

	return nil
}
