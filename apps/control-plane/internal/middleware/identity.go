// internal/middleware/identity.go
// Provides ResolveTenantID — a shared helper used by every controller to map
// the authenticated Clerk user ID (set by AuthMiddleware) to the internal
// database UUID (user_profiles.id).
//
// Error semantics:
//   - GetUserID returns false   → 401 Unauthorized  (AuthMiddleware did not set a user — should never happen in protected group)
//   - ResolveUserProfileID fails → 403 Forbidden     (identity confirmed but DB profile not yet provisioned via webhook)
//
// This is NOT a 401 because the caller HAS valid credentials (API key or JWT).
// The Clerk webhook simply hasn't fired yet (at-most-once delivery lag, cold start, etc.).
// Returning 403 tells the client "you are authenticated but not yet authorised to use this resource".
package middleware

import (
	"errors"
	"log"
	"net/http"

	"e2e-platform/apps/control-plane/internal/services"

	"github.com/gin-gonic/gin"
)

// ResolveTenantID extracts the authenticated user's Clerk ID from the Gin
// context (set by AuthMiddleware) and resolves it to the internal DB UUID
// via IdentityService.
//
// On failure it writes the appropriate HTTP error and returns "", false.
// Callers MUST check the boolean and return immediately on false.
//
//	tenantID, ok := middleware.ResolveTenantID(c, identityService)
//	if !ok {
//	    return
//	}
func ResolveTenantID(c *gin.Context, identity *services.IdentityService) (string, bool) {
	clerkID, exists := GetUserID(c)
	if !exists || clerkID == "" {
		requestID := GetRequestID(c)
		log.Printf("[Identity] Missing Clerk user ID in context | Path=%s | ReqID=%s", c.Request.URL.Path, requestID)
		c.JSON(http.StatusUnauthorized, gin.H{
			"error":      "authentication_required",
			"message":    "Authentication required. Provide a valid API key or session token.",
			"request_id": requestID,
		})
		return "", false
	}

	tenantID, err := identity.ResolveUserProfileID(clerkID)
	if err != nil {
		requestID := GetRequestID(c)
		if errors.Is(err, services.ErrUserNotFound) {
			// The user is authenticated (valid key/JWT) but their profile
			// hasn't been provisioned by the Clerk webhook yet.
			// This is a timing race on first sign-up — guide the user.
			log.Printf("[Identity] User profile not found | ClerkID=%s | ReqID=%s", clerkID, requestID)
			c.JSON(http.StatusForbidden, gin.H{
				"error":      "user_not_provisioned",
				"message":    "Your account profile is still being set up. This usually takes a few seconds. Please try again shortly.",
				"request_id": requestID,
			})
		} else {
			// Database or infrastructure error — don't expose internals.
			log.Printf("[Identity] ResolveUserProfileID failed | ClerkID=%s | Error=%v | ReqID=%s", clerkID, err, requestID)
			c.JSON(http.StatusInternalServerError, gin.H{
				"error":      "identity_resolution_failed",
				"message":    "Failed to resolve your account identity. Please try again.",
				"request_id": requestID,
			})
		}
		return "", false
	}

	return tenantID, true
}
