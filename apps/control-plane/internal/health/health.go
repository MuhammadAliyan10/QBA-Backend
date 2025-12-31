package health

import (
	"context"
	"net/http"
	"time"

	"e2e-platform/apps/control-plane/internal/db"

	"github.com/gin-gonic/gin"
	"github.com/nats-io/nats.go"
	"github.com/redis/go-redis/v9"
)

// HealthHandler provides health check endpoints for the control plane.
// Used by Azure Container Apps, Kubernetes, and load balancers.
type HealthHandler struct {
	redis *redis.Client
	nats  *nats.Conn
}

// HealthStatus represents the response from the health endpoint.
type HealthStatus struct {
	Status   string            `json:"status"`
	Time     string            `json:"time"`
	Services map[string]string `json:"services"`
}

// NewHealthHandler creates a new health check handler.
func NewHealthHandler(redisClient *redis.Client, natsConn *nats.Conn) *HealthHandler {
	return &HealthHandler{
		redis: redisClient,
		nats:  natsConn,
	}
}

// HandleHealth is the Gin handler for GET /health
// Returns 200 OK only if all services are healthy.
func (h *HealthHandler) HandleHealth(c *gin.Context) {
	status := HealthStatus{
		Status:   "healthy",
		Time:     time.Now().UTC().Format(time.RFC3339),
		Services: make(map[string]string),
	}

	allHealthy := true
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// 1. Check Database
	if err := db.Ping(); err != nil {
		status.Services["database"] = "unhealthy: " + err.Error()
		allHealthy = false
	} else {
		status.Services["database"] = "healthy"
	}

	// 2. Check Redis
	if h.redis != nil {
		if err := h.redis.Ping(ctx).Err(); err != nil {
			status.Services["redis"] = "unhealthy: " + err.Error()
			allHealthy = false
		} else {
			status.Services["redis"] = "healthy"
		}
	} else {
		status.Services["redis"] = "not configured"
	}

	// 3. Check NATS
	if h.nats != nil {
		if h.nats.IsConnected() {
			status.Services["nats"] = "healthy"
		} else {
			status.Services["nats"] = "unhealthy: disconnected"
			allHealthy = false
		}
	} else {
		status.Services["nats"] = "not configured"
	}

	// Return appropriate status code
	if allHealthy {
		c.JSON(http.StatusOK, status)
	} else {
		status.Status = "unhealthy"
		c.JSON(http.StatusServiceUnavailable, status)
	}
}

// HandleLiveness is a simple liveness probe (just checks if the server is up).
// Used by Kubernetes liveness probes - should be fast and minimal.
func (h *HealthHandler) HandleLiveness(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{
		"status": "alive",
		"time":   time.Now().UTC().Format(time.RFC3339),
	})
}

// HandleReadiness is an alias for HandleHealth.
// Used by Kubernetes readiness probes.
func (h *HealthHandler) HandleReadiness(c *gin.Context) {
	h.HandleHealth(c)
}
