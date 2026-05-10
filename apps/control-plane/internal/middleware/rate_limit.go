package middleware

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"golang.org/x/time/rate"
)

// ─── RATE LIMIT MIDDLEWARE ───────────────────────────────────────────────────

// RateLimitMiddleware enforces token bucket rate limiting per user.
//
// Three-tier defense:
//  1. Primary:   Redis Lua token bucket (distributed, persistent)
//  2. Fallback:  In-memory x/time/rate.Limiter per userID (local, ephemeral)
//  3. Dead stop: HTTP 503 — if both fail, NO traffic passes
//
// This middleware NEVER fails open. Un-metered traffic on heavy compute is
// a resource exhaustion vector.
type RateLimitMiddleware struct {
	redis       *redis.Client
	luaScript   *redis.Script
	maxRequests int
	window      time.Duration

	// In-memory fallback limiter (sync.Map[string]*rate.Limiter)
	localLimiters sync.Map
	localRate     rate.Limit
	localBurst    int

	// Periodic cleanup of stale local limiters
	cleanupOnce sync.Once
}

// Lua script for atomic token bucket implementation.
// Returns:
//
//	-1: Rate limit exceeded (no tokens available)
//	>= 0: Tokens remaining after deduction
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

// NewRateLimitMiddleware creates a new fail-closed rate limiting middleware.
//
// Configuration via environment variables:
//   - RATE_LIMIT_REQUESTS: Max requests per window (default: 5)
//   - RATE_LIMIT_WINDOW:   Window duration in seconds (default: 60)
func NewRateLimitMiddleware(redisClient *redis.Client) *RateLimitMiddleware {
	maxRequests, _ := strconv.Atoi(os.Getenv("RATE_LIMIT_REQUESTS"))
	if maxRequests == 0 {
		maxRequests = 100 // Default: 100 requests (increased from 5 to support frontend boot sequences)
	}

	windowSeconds, _ := strconv.Atoi(os.Getenv("RATE_LIMIT_WINDOW"))
	if windowSeconds == 0 {
		windowSeconds = 60 // Default: 60 seconds
	}

	// Compute local rate limiter parameters to mirror Redis config.
	// rate.Limit is events per second. We want maxRequests per windowSeconds.
	localRate := rate.Limit(float64(maxRequests) / float64(windowSeconds))

	rl := &RateLimitMiddleware{
		redis:       redisClient,
		luaScript:   redis.NewScript(luaTokenBucketScript),
		maxRequests: maxRequests,
		window:      time.Duration(windowSeconds) * time.Second,
		localRate:   localRate,
		localBurst:  maxRequests,
	}

	return rl
}

// Middleware returns the Gin middleware function for rate limiting.
func (rl *RateLimitMiddleware) Middleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		// Extract user ID from context (set by auth middleware).
		userID, exists := GetUserID(c)
		if !exists {
			log.Printf("[RATE] REJECT: No user identity in context | IP=%s", c.ClientIP())
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Authentication required",
			})
			c.Abort()
			return
		}

		// Start periodic cleanup of stale local limiters.
		rl.cleanupOnce.Do(func() {
			go rl.cleanupLoop()
		})

		// ── Tier 1: Redis token bucket ───────────────────────────────────
		if rl.redis != nil {
			remaining, retryAfter, err := rl.checkRedisRateLimit(c.Request.Context(), userID)

			if err == nil {
				// Redis is healthy — evaluate result.
				if remaining == -1 {
					rl.rejectRateLimited(c, userID, retryAfter)
					return
				}
				// Passed — set headers and continue.
				c.Header("X-RateLimit-Limit", fmt.Sprintf("%d", rl.maxRequests))
				c.Header("X-RateLimit-Remaining", fmt.Sprintf("%d", remaining))
				c.Next()
				return
			}

			// Redis failed — fall through to tier 2.
			log.Printf("[RATE] WARNING: Redis unavailable — falling back to in-memory limiter | User=%s | Error=%v",
				userID, err)
		} else {
			log.Printf("[RATE] WARNING: Redis not configured — using in-memory limiter")
		}

		// ── Tier 2: Redis failed — fall back to in-memory limiter ────────

		if rl.checkLocalRateLimit(userID) {
			// Passed local check.
			c.Header("X-RateLimit-Limit", fmt.Sprintf("%d", rl.maxRequests))
			c.Header("X-RateLimit-Source", "local-fallback")
			c.Next()
			return
		}

		// Local limiter also says no.
		rl.rejectRateLimited(c, userID, rl.window)
	}
}

// ── TIER 1: REDIS ────────────────────────────────────────────────────────────

func (rl *RateLimitMiddleware) checkRedisRateLimit(
	ctx context.Context, userID string,
) (int64, time.Duration, error) {
	key := fmt.Sprintf("ratelimit:user:%s:jobs", userID)

	result, err := rl.luaScript.Run(
		ctx,
		rl.redis,
		[]string{key},
		rl.maxRequests,
		int(rl.window.Seconds()),
		1, // Cost per request
	).Result()

	if err != nil {
		return 0, 0, fmt.Errorf("redis lua script failed: %w", err)
	}

	remaining, ok := result.(int64)
	if !ok {
		return 0, 0, fmt.Errorf("unexpected result type: %T", result)
	}

	var retryAfter time.Duration
	if remaining == -1 {
		ttl, err := rl.redis.TTL(ctx, key).Result()
		if err != nil {
			retryAfter = rl.window
		} else {
			retryAfter = ttl
		}
	}

	return remaining, retryAfter, nil
}

// ── TIER 2: IN-MEMORY FALLBACK ───────────────────────────────────────────────

// checkLocalRateLimit uses a per-user x/time/rate.Limiter as a fallback
// when Redis is unavailable. Returns true if the request is allowed.
func (rl *RateLimitMiddleware) checkLocalRateLimit(userID string) bool {
	limiterI, _ := rl.localLimiters.LoadOrStore(userID, &localLimiterEntry{
		limiter:  rate.NewLimiter(rl.localRate, rl.localBurst),
		lastSeen: time.Now(),
	})

	entry := limiterI.(*localLimiterEntry)
	entry.lastSeen = time.Now()
	return entry.limiter.Allow()
}

type localLimiterEntry struct {
	limiter  *rate.Limiter
	lastSeen time.Time
}

// cleanupLoop removes stale local limiters every 5 minutes to prevent unbounded
// memory growth. A limiter is stale if not seen for 10 minutes.
func (rl *RateLimitMiddleware) cleanupLoop() {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()

	for range ticker.C {
		now := time.Now()
		stale := 0
		rl.localLimiters.Range(func(key, value interface{}) bool {
			entry := value.(*localLimiterEntry)
			if now.Sub(entry.lastSeen) > 10*time.Minute {
				rl.localLimiters.Delete(key)
				stale++
			}
			return true
		})
		if stale > 0 {
			log.Printf("[RATE] Cleaned up %d stale local rate limiters", stale)
		}
	}
}

// ── REJECTION ────────────────────────────────────────────────────────────────

func (rl *RateLimitMiddleware) rejectRateLimited(c *gin.Context, userID string, retryAfter time.Duration) {
	log.Printf("[RATE] REJECT: Rate limit exceeded | User=%s | IP=%s | Path=%s",
		userID, c.ClientIP(), c.Request.URL.Path)

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
}

// ── PUBLIC HELPERS ───────────────────────────────────────────────────────────

// GetRemainingQuota returns the current remaining quota for a user.
func (rl *RateLimitMiddleware) GetRemainingQuota(ctx context.Context, userID string) (int64, error) {
	key := fmt.Sprintf("ratelimit:user:%s:jobs", userID)

	val, err := rl.redis.Get(ctx, key).Int64()
	if err == redis.Nil {
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
	log.Printf("[RATE] Reset rate limit for user %s", userID)
	return nil
}
