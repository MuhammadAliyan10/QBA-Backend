// internal/middleware/request_id.go
// RequestIDMiddleware assigns a unique, traceable X-Request-ID to every request.
//
// Behaviour:
//   - If the client sends an X-Request-ID header, it is validated and reused
//     (allows distributed tracing across microservices).
//   - Otherwise, a new UUID v4 is generated server-side.
//   - The resolved ID is stored in the Gin context under the key "request_id"
//     and echoed back on every response via the X-Request-ID header.
//
// This is a zero-allocation hot path: UUID generation is the only allocation.
package middleware

import (
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

const RequestIDKey = "request_id"

// RequestIDMiddleware returns a Gin middleware that sets X-Request-ID on every response.
func RequestIDMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		requestID := strings.TrimSpace(c.GetHeader("X-Request-ID"))

		// Validate client-supplied ID: must be a canonical UUID to prevent header injection.
		if requestID != "" {
			if _, err := uuid.Parse(requestID); err != nil {
				// Discard malformed IDs silently and generate a new one.
				requestID = ""
			}
		}

		if requestID == "" {
			requestID = uuid.New().String()
		}

		// Expose for downstream handlers (e.g., structured logging).
		c.Set(RequestIDKey, requestID)

		// Set on the response so clients can correlate logs.
		c.Header("X-Request-ID", requestID)

		c.Next()
	}
}

// GetRequestID retrieves the request ID from the Gin context.
// Returns an empty string if not set (safe to call unconditionally).
func GetRequestID(c *gin.Context) string {
	id, _ := c.Get(RequestIDKey)
	if id == nil {
		return ""
	}
	s, _ := id.(string)
	return s
}
