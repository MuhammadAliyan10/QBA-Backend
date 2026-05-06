package middleware

import (
	"os"
	"strings"

	"github.com/gin-contrib/cors"
)

// DefaultCORS returns a restrictive CORS config.
// Set ALLOWED_ORIGINS to a comma-separated list (e.g. https://app.example.com,http://localhost:3000).
func DefaultCORS() cors.Config {
	raw := strings.TrimSpace(os.Getenv("ALLOWED_ORIGINS"))
	cfg := cors.Config{
		AllowHeaders: []string{"Origin", "Content-Length", "Content-Type", "Authorization", "X-User-Id", "X-Clerk-User-Id", "Accept", "Referer", "User-Agent", "X-Idempotency-Key"},
		AllowMethods: []string{"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"},
	}

	if raw == "" {
		cfg.AllowOrigins = []string{
			"http://localhost:3000",
			"http://127.0.0.1:3000",
			"http://localhost:5173",
			"http://127.0.0.1:5173",
		}
		return cfg
	}

	var origins []string
	for _, p := range strings.Split(raw, ",") {
		if o := strings.TrimSpace(p); o != "" {
			origins = append(origins, o)
		}
	}
	cfg.AllowOrigins = origins
	return cfg
}
