// internal/middleware/ip_guard.go
// IPAuthGuard — Redis-backed IP-level brute-force protection.
//
// Applied to the AuthMiddleware handler chain: any IP that accumulates
// AUTH_FAIL_LIMIT (default: 20) consecutive authentication failures within
// AUTH_FAIL_WINDOW (default: 15 minutes) is blocked with HTTP 429.
//
// Design:
//   - Uses a Redis INCR + EXPIRE pattern (atomic, no Lua required).
//   - Fails open (allows traffic) if Redis is unavailable — auth middleware
//     still rejects invalid credentials, so security is not compromised.
//   - Reset: auto-expires after window. No admin endpoint needed.
package middleware

import (
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
)

const (
	ipGuardKeyPrefix    = "auth_fail:ip:"
	defaultFailLimit    = 20
	defaultFailWindowSec = 15 * 60 // 15 minutes
)

// IPAuthGuard is a Gin middleware that rate-limits authentication failures
// per client IP address to prevent API key enumeration and credential stuffing.
type IPAuthGuard struct {
	redis     *redis.Client
	failLimit int64
	windowSec int64
}

// NewIPAuthGuard creates a new IPAuthGuard. If redisClient is nil the middleware
// becomes a no-op (fails open), preserving availability over security hardening.
func NewIPAuthGuard(redisClient *redis.Client) *IPAuthGuard {
	failLimit := int64(defaultFailLimit)
	if v, _ := strconv.ParseInt(os.Getenv("AUTH_FAIL_LIMIT"), 10, 64); v > 0 {
		failLimit = v
	}

	windowSec := int64(defaultFailWindowSec)
	if v, _ := strconv.ParseInt(os.Getenv("AUTH_FAIL_WINDOW_SEC"), 10, 64); v > 0 {
		windowSec = v
	}

	return &IPAuthGuard{
		redis:     redisClient,
		failLimit: failLimit,
		windowSec: windowSec,
	}
}

// Middleware returns the Gin handler function.
// It intercepts the request BEFORE AuthMiddleware runs, checks if the IP is
// currently blocked, then checks again AFTER to record new failures.
func (g *IPAuthGuard) Middleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		if g.redis == nil {
			c.Next()
			return
		}

		ip := extractClientIP(c)
		key := ipGuardKeyPrefix + ip
		ctx := c.Request.Context()

		// Pre-check: is this IP already blocked?
		count, err := g.redis.Get(ctx, key).Int64()
		if err != nil && err != redis.Nil {
			// Redis unavailable — fail open, log and continue.
			log.Printf("[IPGUARD] Redis error on pre-check for %s: %v", ip, err)
			c.Next()
			return
		}

		if err == nil && count >= g.failLimit {
			ttl, _ := g.redis.TTL(ctx, key).Result()
			log.Printf("[IPGUARD] BLOCKED: IP=%s failures=%d ttl=%v", ip, count, ttl)
			c.Header("Retry-After", fmt.Sprintf("%d", int(ttl.Seconds())))
			c.JSON(http.StatusTooManyRequests, gin.H{
				"error":       "Too many authentication failures from this IP address.",
				"retry_after": int(ttl.Seconds()),
			})
			c.Abort()
			return
		}

		// Run the downstream handlers (including AuthMiddleware).
		c.Next()

		// Post-check: if auth failed (HTTP 401), record the failure.
		if c.Writer.Status() == http.StatusUnauthorized {
			g.recordFailure(ctx, key, ip)
		}
	}
}

// RecordAuthFailure allows external callers (e.g., AuthMiddleware) to manually
// record a failure when the full middleware chain isn't used.
func (g *IPAuthGuard) RecordAuthFailure(ctx context.Context, ip string) {
	if g.redis == nil {
		return
	}
	key := ipGuardKeyPrefix + ip
	g.recordFailure(ctx, key, ip)
}

func (g *IPAuthGuard) recordFailure(ctx context.Context, key, ip string) {
	pipe := g.redis.Pipeline()
	incrCmd := pipe.Incr(ctx, key)
	pipe.Expire(ctx, key, time.Duration(g.windowSec)*time.Second)

	if _, err := pipe.Exec(ctx); err != nil {
		log.Printf("[IPGUARD] Failed to record auth failure for IP %s: %v", ip, err)
		return
	}

	newCount := incrCmd.Val()
	if newCount >= g.failLimit {
		log.Printf("[IPGUARD] THRESHOLD REACHED: IP=%s failures=%d — IP is now blocked for %ds",
			ip, newCount, g.windowSec)
	} else {
		log.Printf("[IPGUARD] AUTH FAIL recorded: IP=%s count=%d/%d", ip, newCount, g.failLimit)
	}
}

// extractClientIP returns the real client IP, respecting X-Forwarded-For
// from trusted reverse proxies (caddy/nginx in front of the service).
func extractClientIP(c *gin.Context) string {
	// Check X-Forwarded-For first (set by nginx/caddy/cloudflare).
	if xff := strings.TrimSpace(c.GetHeader("X-Forwarded-For")); xff != "" {
		// XFF can be a comma-separated list; take the leftmost (real client).
		parts := strings.SplitN(xff, ",", 2)
		if ip := strings.TrimSpace(parts[0]); ip != "" {
			if parsed := net.ParseIP(ip); parsed != nil {
				return parsed.String()
			}
		}
	}

	// Fall back to RemoteAddr (direct connection).
	ip, _, err := net.SplitHostPort(c.Request.RemoteAddr)
	if err != nil {
		return c.Request.RemoteAddr
	}
	return ip
}
