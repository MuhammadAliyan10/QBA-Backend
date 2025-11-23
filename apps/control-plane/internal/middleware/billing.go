package middleware

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/nats-io/nats.go"
	"github.com/redis/go-redis/v9"
)

// BillingMiddleware enforces credit balance checks before workflow execution.
// Uses Redis Lua scripts for atomic DECRBY operations.
//
// Flow:
// 1. Extract user_id from request (JWT or header)
// 2. Check balance in Redis
// 3. If balance >= cost: DECRBY atomically
// 4. If balance < cost: Reject with 402 Payment Required
// 5. Publish billing event to NATS for async ledger write
type BillingMiddleware struct {
	redis      *redis.Client
	nats       *nats.Conn
	luaScript  *redis.Script
	creditCost int
}

// Lua script for atomic credit deduction with rollback
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
		creditCost = 1 // Default
	}

	return &BillingMiddleware{
		redis:      redisClient,
		nats:       natsConn,
		luaScript:  redis.NewScript(luaDeductScript),
		creditCost: creditCost,
	}
}

// Middleware returns the Gin middleware function.
func (bm *BillingMiddleware) Middleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		// Extract user ID from context (set by auth middleware)
		userID, exists := c.Get("user_id")
		if !exists {
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "user_id not found in context",
			})
			c.Abort()
			return
		}

		userIDStr := userID.(string)

		// Check and deduct credits atomically
		balanceAfter, err := bm.deductCredits(c.Request.Context(), userIDStr)
		if err != nil {
			// Insufficient balance
			if err.Error() == "insufficient_balance" {
				log.Printf("❌ User %s has insufficient credits", userIDStr)
				c.JSON(http.StatusPaymentRequired, gin.H{
					"error":   "Insufficient credits",
					"message": "Please top up your account to continue",
					"code":    "INSUFFICIENT_CREDITS",
				})
				c.Abort()
				return
			}

			// Redis error
			log.Printf("❌ Redis error for user %s: %v", userIDStr, err)
			c.JSON(http.StatusInternalServerError, gin.H{
				"error": "Billing service unavailable",
			})
			c.Abort()
			return
		}

		// Success - publish billing event for async ledger write
		go bm.publishBillingEvent(userIDStr, -bm.creditCost, int(balanceAfter))

		// Store balance in context for response headers
		c.Set("credits_remaining", balanceAfter)

		// Continue to next handler
		c.Next()

		// Add balance info to response headers
		c.Header("X-Credits-Remaining", fmt.Sprintf("%d", balanceAfter))
	}
}

// deductCredits performs atomic credit deduction using Lua script.
func (bm *BillingMiddleware) deductCredits(ctx context.Context, userID string) (int64, error) {
	key := fmt.Sprintf("user:%s:credits", userID)

	// Execute Lua script
	result, err := bm.luaScript.Run(
		ctx,
		bm.redis,
		[]string{key},
		bm.creditCost,
	).Result()

	if err != nil {
		return 0, fmt.Errorf("lua script failed: %w", err)
	}

	// Parse result
	balance, ok := result.(int64)
	if !ok {
		return 0, fmt.Errorf("unexpected result type: %T", result)
	}

	// Check if balance was insufficient
	if balance == -1 {
		return 0, fmt.Errorf("insufficient_balance")
	}

	log.Printf("✅ Deducted %d credits from user %s. Balance: %d", bm.creditCost, userID, balance)
	return balance, nil
}

// publishBillingEvent sends a billing event to NATS for async ledger write.
func (bm *BillingMiddleware) publishBillingEvent(userID string, amount int, balanceAfter int) {
	// Marshal to JSON (or protobuf in production)
	data := fmt.Sprintf(`{"user_id":"%s","amount":%d,"balance_after":%d,"type":"DEDUCTION","timestamp":%d}`,
		userID, amount, balanceAfter, time.Now().Unix())

	// Publish to NATS
	err := bm.nats.Publish("billing.events", []byte(data))
	if err != nil {
		log.Printf("⚠️  Failed to publish billing event: %v", err)
		// Don't fail the request - ledger write is async
		return
	}

	log.Printf("📨 Published billing event for user %s", userID)
}

// GetBalance retrieves the current credit balance for a user.
func (bm *BillingMiddleware) GetBalance(ctx context.Context, userID string) (int64, error) {
	key := fmt.Sprintf("user:%s:credits", userID)

	val, err := bm.redis.Get(ctx, key).Int64()
	if err == redis.Nil {
		// User not found - return 0
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("redis error: %w", err)
	}

	return val, nil
}

// AddCredits adds credits to a user's balance (used by Stripe webhook).
func (bm *BillingMiddleware) AddCredits(ctx context.Context, userID string, amount int) (int64, error) {
	key := fmt.Sprintf("user:%s:credits", userID)

	newBalance, err := bm.redis.IncrBy(ctx, key, int64(amount)).Result()
	if err != nil {
		return 0, fmt.Errorf("redis incrby failed: %w", err)
	}

	// Publish billing event
	go bm.publishBillingEvent(userID, amount, int(newBalance))

	log.Printf("💰 Added %d credits to user %s. New balance: %d", amount, userID, newBalance)
	return newBalance, nil
}

// InitializeUserCredits sets default credits for a new user.
func (bm *BillingMiddleware) InitializeUserCredits(ctx context.Context, userID string) error {
	defaultCredits, _ := strconv.Atoi(os.Getenv("DEFAULT_CREDITS"))
	if defaultCredits == 0 {
		defaultCredits = 100 // Default
	}

	key := fmt.Sprintf("user:%s:credits", userID)

	// Only set if not exists
	set, err := bm.redis.SetNX(ctx, key, defaultCredits, 0).Result()
	if err != nil {
		return fmt.Errorf("redis setnx failed: %w", err)
	}

	if set {
		log.Printf("🆕 Initialized %d credits for new user %s", defaultCredits, userID)
		// Publish event
		go bm.publishBillingEvent(userID, defaultCredits, defaultCredits)
	}

	return nil
}
