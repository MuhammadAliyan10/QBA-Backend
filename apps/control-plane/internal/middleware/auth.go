package middleware

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"os"
	"strings"
	"time"

	"e2e-platform/apps/control-plane/internal/db"
	"e2e-platform/apps/control-plane/internal/models"

	"github.com/gin-gonic/gin"
)

// ContextKey is the type for context keys to avoid collisions
type ContextKey string

const (
	// UserIDKey is the context key for storing user ID
	UserIDKey ContextKey = "userID"
)

// AuthMiddleware validates requests using one of two methods:
//   1. API Key: Authorization: Bearer sk_live_xxx or sk_test_xxx
//      → Validates against api_keys table (for programmatic/external access)
//   2. Clerk JWT: Authorization: Bearer <jwt_token>
//      → Extracts user ID from X-User-Id header set by frontend (for browser access)
//   3. Development mode: X-User-ID header for testing
//
// In development mode (ENVIRONMENT != "production"), unauthenticated requests are allowed
// with a warning log, so the frontend can operate without API keys during development.
func AuthMiddleware() gin.HandlerFunc {
	isProduction := os.Getenv("ENVIRONMENT") == "production" || os.Getenv("GIN_MODE") == "release"

	return func(c *gin.Context) {
		authHeader := c.GetHeader("Authorization")

		// --- Path 1: API Key Authentication ---
		if authHeader != "" {
			parts := strings.SplitN(authHeader, " ", 2)
			if len(parts) == 2 && parts[0] == "Bearer" {
				token := parts[1]

				// Check if it's an API key (sk_live_ or sk_test_ prefix)
				if strings.HasPrefix(token, "sk_live_") || strings.HasPrefix(token, "sk_test_") {
					if authenticateAPIKey(c, token) {
						c.Next()
						return
					}
					// API key was provided but invalid — reject
					return
				}

				// Otherwise treat as Clerk JWT — extract user identity from it
				// Clerk tokens are JWTs, but full verification requires JWKS.
				// For now, we trust the frontend (which is Clerk-protected) and
				// extract user identity from the X-User-Id header if present.
			}
		}

		// --- Path 2: User ID from header (frontend/dev) ---
		if userID := c.GetHeader("X-User-Id"); userID != "" {
			c.Set(string(UserIDKey), userID)
			c.Next()
			return
		}

		// Also check Clerk's standard header
		if userID := c.GetHeader("X-Clerk-User-Id"); userID != "" {
			c.Set(string(UserIDKey), userID)
			c.Next()
			return
		}

		// --- Path 3: No auth provided ---
		if isProduction {
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Authentication required. Provide an API key (Authorization: Bearer sk_live_xxx) or valid session.",
			})
			c.Abort()
			return
		}

		// Development mode: allow with warning
		c.Set(string(UserIDKey), "anonymous-dev")
		c.Next()
	}
}

// authenticateAPIKey validates an API key against the database.
// Returns true if authentication succeeded, false if it failed (and response was sent).
func authenticateAPIKey(c *gin.Context, apiKey string) bool {
	// Hash the API key (SHA-256)
	hash := sha256.Sum256([]byte(apiKey))
	keyHash := hex.EncodeToString(hash[:])

	// Query database with timeout
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	var apiKeyRecord models.ApiKey
	err := db.DB.WithContext(ctx).Where("key_hash = ? AND is_active = ?", keyHash, true).First(&apiKeyRecord).Error

	if err != nil {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid API key"})
		c.Abort()
		return false
	}

	if !apiKeyRecord.IsActive {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "API key has been deactivated"})
		c.Abort()
		return false
	}

	// Store user ID in context
	c.Set(string(UserIDKey), apiKeyRecord.UserID)
	return true
}

// GetUserID extracts userID from the request context.
// Should be called after AuthMiddleware.
func GetUserID(c *gin.Context) (string, bool) {
	userID, exists := c.Get(string(UserIDKey))
	if !exists {
		return "", false
	}

	userIDStr, ok := userID.(string)
	return userIDStr, ok
}
