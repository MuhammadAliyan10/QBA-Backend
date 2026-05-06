package middleware

import (
	"net/http"
	"os"
	"strings"

	"github.com/gin-gonic/gin"
)

// MetricsTokenAuth protects /metrics when METRICS_TOKEN is set (Bearer token).
func MetricsTokenAuth() gin.HandlerFunc {
	token := strings.TrimSpace(os.Getenv("METRICS_TOKEN"))
	if token == "" {
		return func(c *gin.Context) { c.Next() }
	}
	want := "Bearer " + token
	return func(c *gin.Context) {
		if c.GetHeader("Authorization") != want {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		c.Next()
	}
}
