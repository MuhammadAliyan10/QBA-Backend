package middleware

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
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

// AuthMiddleware validates API keys from the Authorization header
// Expected format: Authorization: Bearer sk_live_xxx or sk_test_xxx
func AuthMiddleware() gin.HandlerFunc {
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

		// Hash the API key (SHA-256)
		hash := sha256.Sum256([]byte(apiKey))
		keyHash := hex.EncodeToString(hash[:])

		// Query database to validate API key using GORM (with timeout)
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()

		var apiKeyRecord models.ApiKey
		err := db.DB.WithContext(ctx).Where("key_hash = ? AND is_active = ?", keyHash, true).First(&apiKeyRecord).Error

		if err != nil {
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Invalid API key",
			})
			c.Abort()
			return
		}

		if !apiKeyRecord.IsActive {
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "API key has been deactivated",
			})
			c.Abort()
			return
		}

		// Store user ID in context for downstream handlers
		c.Set(string(UserIDKey), apiKeyRecord.UserID)

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
