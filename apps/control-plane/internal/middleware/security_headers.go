// internal/middleware/security_headers.go
// SecurityHeadersMiddleware sets enterprise-grade HTTP security response headers
// on every response from the control-plane.
//
// Headers applied:
//   - X-Content-Type-Options: nosniff         (prevents MIME-sniffing attacks)
//   - X-Frame-Options: DENY                   (prevents clickjacking via iframe embedding)
//   - X-XSS-Protection: 0                     (CSP is the real defence; disable legacy broken filter)
//   - Referrer-Policy: strict-origin           (no path leakage in Referer header)
//   - Permissions-Policy: ...                  (disables unneeded browser APIs)
//   - Strict-Transport-Security               (HSTS — only set in release/production mode)
//   - Cache-Control: no-store                 (API responses must never be cached by intermediaries)
//   - X-Request-ID                            (echoed from context if RequestIDMiddleware ran first)
package middleware

import (
	"os"

	"github.com/gin-gonic/gin"
)

// SecurityHeadersMiddleware returns a Gin middleware that sets hardened HTTP response headers.
func SecurityHeadersMiddleware() gin.HandlerFunc {
	isProduction := os.Getenv("GIN_MODE") == "release"

	return func(c *gin.Context) {
		// MIME-type sniffing protection
		c.Header("X-Content-Type-Options", "nosniff")

		// Clickjacking protection — API endpoints are never embedded in iframes
		c.Header("X-Frame-Options", "DENY")

		// Modern browsers rely on CSP; the legacy XSS filter causes more harm than good
		c.Header("X-XSS-Protection", "0")

		// Restrict Referer header to origin only (no path/query leakage)
		c.Header("Referrer-Policy", "strict-origin-when-cross-origin")

		// Restrict access to powerful browser APIs
		c.Header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")

		// API responses must not be stored in intermediate caches (proxies, CDNs)
		c.Header("Cache-Control", "no-store, no-cache, must-revalidate, private")

		// HSTS: only set in production — in development, HTTP is fine
		if isProduction {
			// max-age=63072000 = 2 years. includeSubDomains + preload for max protection.
			c.Header("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload")
		}

		// Echo request ID if set by RequestIDMiddleware (allows log correlation from clients)
		if requestID := GetRequestID(c); requestID != "" {
			c.Header("X-Request-ID", requestID)
		}

		c.Next()
	}
}
