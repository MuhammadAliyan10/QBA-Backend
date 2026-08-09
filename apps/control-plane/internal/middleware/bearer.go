package middleware

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"e2e-platform/apps/control-plane/internal/db"
	"e2e-platform/apps/control-plane/internal/models"

	"github.com/gin-gonic/gin"
)

// setAuthUser stores the verified user on the Gin context (canonical + legacy key).
func setAuthUser(c *gin.Context, userID string) {
	c.Set(string(UserIDKey), userID)
	c.Set("user_id", userID)
}

// ResolveBearerFromRequest verifies the same credentials as AuthMiddleware for a raw
// http.Request (used for WebSocket upgrades — browsers cannot set Authorization on WS).
// Supports Authorization: Bearer or query ?access_token= for WebSocket URLs.
func ResolveBearerFromRequest(r *http.Request) (string, error) {
	if strings.EqualFold(os.Getenv("SKIP_AUTH"), "true") {
		uid := strings.TrimSpace(os.Getenv("DEV_USER_ID"))
		if uid == "" {
			uid = "dev_user_local"
		}
		return uid, nil
	}
	auth := strings.TrimSpace(r.Header.Get("Authorization"))
	if auth == "" {
		if q := strings.TrimSpace(r.URL.Query().Get("access_token")); q != "" {
			auth = "Bearer " + q
		}
	}
	return resolveAuthorizationHeader(r.Context(), auth)
}

// resolveAuthorizationHeader validates Authorization: Bearer and returns the user id.
func resolveAuthorizationHeader(ctx context.Context, authHeader string) (string, error) {
	if strings.EqualFold(os.Getenv("SKIP_AUTH"), "true") {
		uid := strings.TrimSpace(os.Getenv("DEV_USER_ID"))
		if uid == "" {
			uid = "dev_user_local"
		}
		return uid, nil
	}

	if authHeader == "" {
		return "", fmt.Errorf("missing authorization")
	}

	parts := strings.SplitN(authHeader, " ", 2)
	if len(parts) != 2 || parts[0] != "Bearer" {
		return "", fmt.Errorf("invalid authorization header")
	}

	token := parts[1]

	if strings.HasPrefix(token, "sk_live_") || strings.HasPrefix(token, "sk_test_") {
		return lookupAPIKeyUserID(ctx, token)
	}

	issuerURL := clerkIssuerURL()
	if issuerURL == "" {
		return "", fmt.Errorf("jwt authentication not configured")
	}

	return verifyClerkJWT(token, issuerURL)
}

func clerkIssuerURL() string {
	issuerURL := os.Getenv("CLERK_ISSUER_URL")
	if issuerURL != "" {
		return issuerURL
	}
	pk := os.Getenv("CLERK_PUBLISHABLE_KEY")
	if pk != "" {
		return deriveClerkIssuer(pk)
	}
	return ""
}

func lookupAPIKeyUserID(ctx context.Context, apiKey string) (string, error) {
	hash := sha256.Sum256([]byte(apiKey))
	keyHash := hex.EncodeToString(hash[:])

	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	var apiKeyRecord models.ApiKey
	err := db.DB.WithContext(ctx).
		Where("key_hash = ? AND is_active = ?", keyHash, true).
		First(&apiKeyRecord).Error

	if err != nil {
		prefixLen := 12
		if len(apiKey) < prefixLen {
			prefixLen = len(apiKey)
		}
		log.Printf("[AUTH] Invalid API key | Prefix=%s", apiKey[:prefixLen])
		return "", fmt.Errorf("invalid api key")
	}

	if apiKeyRecord.ExpiresAt != nil && apiKeyRecord.ExpiresAt.Before(time.Now()) {
		return "", fmt.Errorf("api key expired")
	}

	// Async last_used_at tracking — fire-and-forget, never blocks the auth path.
	// Uses a detached context so a slow DB write cannot cancel the ongoing request.
	go func(keyHash string) {
		updateCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		now := time.Now()
		if err := db.DB.WithContext(updateCtx).
			Model(&models.ApiKey{}).
			Where("key_hash = ?", keyHash).
			Update("last_used_at", now).Error; err != nil {
			log.Printf("[AUTH] Failed to update last_used_at for key: %v", err)
		}
	}(keyHash)

	return apiKeyRecord.UserID, nil
}
