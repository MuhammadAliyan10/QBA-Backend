package middleware

import (
	"context"
	"database/sql"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

// ContextKey is the type for context keys to avoid collisions
type ContextKey string

const (
	// UserIDKey is the context key for storing user ID
	UserIDKey ContextKey = "userID"
)

// AuthMiddleware validates API keys from the Authorization header
// Expected format: Authorization: Bearer sk_live_xxx or sk_test_xxx
func AuthMiddleware(db *sql.DB) gin.HandlerFunc {
	return func(c *gin.Context) {
		// Extract Authorization header
		authHeader := c.GetHeader("Authorization")
		if authHeader == "" {
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Missing Authorization header",
			})
			c.Abort()
			return
		}

		// Parse Bearer token
		parts := strings.SplitN(authHeader, " ", 2)
		if len(parts) != 2 || parts[0] != "Bearer" {
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Invalid Authorization header format. Expected: Bearer <token>",
			})
			c.Abort()
			return
		}

		apiKey := parts[1]

		// Validate format (sk_live_xxx or sk_test_xxx)
		if !strings.HasPrefix(apiKey, "sk_live_") && !strings.HasPrefix(apiKey, "sk_test_") {
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Invalid API key format",
			})
			c.Abort()
			return
		}

		// Query database to validate API key
		var userID string
		var active bool

		query := `
			SELECT user_id, active
			FROM api_keys
			WHERE key_hash = encode(sha256($1::bytea), 'hex')
			AND active = true
		`

		err := db.QueryRowContext(context.Background(), query, apiKey).Scan(&userID, &active)

		if err == sql.ErrNoRows {
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Invalid API key",
			})
			c.Abort()
			return
		}

		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{
				"error": "Database error during authentication",
			})
			c.Abort()
			return
		}

		if !active {
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "API key has been deactivated",
			})
			c.Abort()
			return
		}

		// Store user ID in context for downstream handlers
		c.Set(string(UserIDKey), userID)

		// Continue to next handler
		c.Next()
	}
}

// GetUserID extracts userID from the request context
// Should be called after AuthMiddleware
func GetUserID(c *gin.Context) (string, bool) {
	userID, exists := c.Get(string(UserIDKey))
	if !exists {
		return "", false
	}

	userIDStr, ok := userID.(string)
	return userIDStr, ok
}
