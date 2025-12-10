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
	"github.com/redis/go-redis/v9"
)

// RateLimitMiddleware enforces token bucket rate limiting per user.
// Prevents resource exhaustion by limiting API calls to N requests per time window.
//
// Algorithm: Token Bucket
// - Each user has a bucket with a maximum capacity
// - Tokens refill over time (window-based reset)
// - Each request consumes 1 token
// - Requests are rejected when bucket is empty
type RateLimitMiddleware struct {
	redis       *redis.Client
	luaScript   *redis.Script
	maxRequests int           // Maximum requests per window
	window      time.Duration // Time window for rate limiting
}

// Lua script for atomic token bucket implementation
// Returns:
//   -1: Rate limit exceeded (no tokens available)
//   >= 0: Tokens remaining after deduction
const luaTokenBucketScript = `
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

-- Get current token count
local current = redis.call('GET', key)

if current == false then
    -- First request: initialize bucket with capacity - cost
    redis.call('SETEX', key, window, capacity - cost)
    return capacity - cost
end

current = tonumber(current)

-- Check if enough tokens available
if current >= cost then
    -- Deduct tokens
    local remaining = redis.call('DECRBY', key, cost)
    return remaining
else
    -- Rate limited
    return -1
end
`

// NewRateLimitMiddleware creates a new rate limiting middleware.
// Configuration via environment variables:
//   - RATE_LIMIT_REQUESTS: Max requests per window (default: 5)
//   - RATE_LIMIT_WINDOW: Window duration in seconds (default: 60)
func NewRateLimitMiddleware(redisClient *redis.Client) *RateLimitMiddleware {
	maxRequests, _ := strconv.Atoi(os.Getenv("RATE_LIMIT_REQUESTS"))
	if maxRequests == 0 {
		maxRequests = 5 // Default: 5 requests
	}

	windowSeconds, _ := strconv.Atoi(os.Getenv("RATE_LIMIT_WINDOW"))
	if windowSeconds == 0 {
		windowSeconds = 60 // Default: 60 seconds (1 minute)
	}

	return &RateLimitMiddleware{
		redis:       redisClient,
		luaScript:   redis.NewScript(luaTokenBucketScript),
		maxRequests: maxRequests,
		window:      time.Duration(windowSeconds) * time.Second,
	}
}

// Middleware returns the Gin middleware function for rate limiting.
func (rl *RateLimitMiddleware) Middleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		// Extract user ID from context (set by auth middleware)
		userID, exists := GetUserID(c)
		if !exists {
			// If no user ID, apply global rate limit (optional)
			// For now, we require authentication
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Authentication required for rate limiting",
			})
			c.Abort()
			return
		}

		// Check rate limit
		remaining, retryAfter, err := rl.checkRateLimit(c.Request.Context(), userID)

		if err != nil {
			// Redis error - log but don't block request (fail open)
			log.Printf("[WARN] Rate limit check failed for user %s: %v", userID, err)
			c.Next()
			return
		}

		// Rate limit exceeded
		if remaining == -1 {
			log.Printf("🚫 Rate limit exceeded for user %s", userID)
			c.Header("X-RateLimit-Limit", fmt.Sprintf("%d", rl.maxRequests))
			c.Header("X-RateLimit-Remaining", "0")
			c.Header("X-RateLimit-Reset", fmt.Sprintf("%d", time.Now().Add(retryAfter).Unix()))
			c.Header("Retry-After", fmt.Sprintf("%d", int(retryAfter.Seconds())))

			c.JSON(http.StatusTooManyRequests, gin.H{
				"error":       "Rate limit exceeded",
				"message":     fmt.Sprintf("Maximum %d jobs per minute. Please try again later.", rl.maxRequests),
				"retry_after": int(retryAfter.Seconds()),
			})
			c.Abort()
			return
		}

		// Success - add rate limit headers
		c.Header("X-RateLimit-Limit", fmt.Sprintf("%d", rl.maxRequests))
		c.Header("X-RateLimit-Remaining", fmt.Sprintf("%d", remaining))
		c.Header("X-RateLimit-Reset", fmt.Sprintf("%d", time.Now().Add(rl.window).Unix()))

		log.Printf("[OK] Rate limit check passed for user %s. Remaining: %d/%d", userID, remaining, rl.maxRequests)
		c.Next()
	}
}

// checkRateLimit performs the token bucket check using Redis Lua script.
// Returns:
//   - remaining: tokens left in bucket (-1 if rate limited)
//   - retryAfter: duration until next token available
//   - error: Redis error (nil on success)
func (rl *RateLimitMiddleware) checkRateLimit(ctx context.Context, userID string) (int64, time.Duration, error) {
	key := fmt.Sprintf("ratelimit:user:%s:jobs", userID)

	// Execute Lua script
	result, err := rl.luaScript.Run(
		ctx,
		rl.redis,
		[]string{key},
		rl.maxRequests,
		int(rl.window.Seconds()),
		1, // Cost per request
	).Result()

	if err != nil {
		return 0, 0, fmt.Errorf("lua script failed: %w", err)
	}

	// Parse result
	remaining, ok := result.(int64)
	if !ok {
		return 0, 0, fmt.Errorf("unexpected result type: %T", result)
	}

	// If rate limited, get TTL for retry-after
	var retryAfter time.Duration
	if remaining == -1 {
		ttl, err := rl.redis.TTL(ctx, key).Result()
		if err != nil {
			retryAfter = rl.window // Fallback to full window
		} else {
			retryAfter = ttl
		}
	}

	return remaining, retryAfter, nil
}

// GetRemainingQuota returns the current remaining quota for a user.
// Useful for /balance or /quota endpoints.
func (rl *RateLimitMiddleware) GetRemainingQuota(ctx context.Context, userID string) (int64, error) {
	key := fmt.Sprintf("ratelimit:user:%s:jobs", userID)

	val, err := rl.redis.Get(ctx, key).Int64()
	if err == redis.Nil {
		// No bucket yet - return full capacity
		return int64(rl.maxRequests), nil
	}
	if err != nil {
		return 0, fmt.Errorf("redis error: %w", err)
	}

	return val, nil
}

// ResetUserQuota resets the rate limit for a specific user (admin function).
func (rl *RateLimitMiddleware) ResetUserQuota(ctx context.Context, userID string) error {
	key := fmt.Sprintf("ratelimit:user:%s:jobs", userID)
	err := rl.redis.Del(ctx, key).Err()
	if err != nil {
		return fmt.Errorf("failed to reset quota: %w", err)
	}
	log.Printf("[RESET] Reset rate limit for user %s", userID)
	return nil
}
