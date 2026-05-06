package streaming

import (
	"fmt"
	"log"
	"net/http"
	"time"

	"e2e-platform/apps/control-plane/internal/authz"
	"e2e-platform/apps/control-plane/internal/middleware"

	"github.com/gin-gonic/gin"
	"github.com/nats-io/nats.go"
	"gorm.io/gorm"
)

// ─── CONFIG ──────────────────────────────────────────────────────────────────

const (
	StreamName = "QUANTA_TELEMETRY"

	SubjectPrefix = "quanta.telemetry."

	SubjectWildcard = "quanta.telemetry.>"

	SSEKeepAliveInterval = 15 * time.Second
)

// StreamManager holds NATS and authorizes SSE per job owner.
type StreamManager struct {
	nc *nats.Conn
	db *gorm.DB
}

// NewStreamManager registers the JetStream stream when JetStream is enabled on the server.
func NewStreamManager(nc *nats.Conn, gdb *gorm.DB) *StreamManager {
	sm := &StreamManager{nc: nc, db: gdb}

	js, err := nc.JetStream()
	if err != nil {
		log.Printf("[StreamManager] JetStream context unavailable (%v); using core NATS subscriptions only", err)
		return sm
	}

	_, err = js.AddStream(&nats.StreamConfig{
		Name:      StreamName,
		Subjects:  []string{SubjectWildcard},
		Retention: nats.InterestPolicy,
		MaxAge:    30 * time.Minute,
		Storage:   nats.MemoryStorage,
	})
	if err != nil {
		_, _ = js.UpdateStream(&nats.StreamConfig{
			Name:      StreamName,
			Subjects:  []string{SubjectWildcard},
			Retention: nats.InterestPolicy,
			MaxAge:    30 * time.Minute,
			Storage:   nats.MemoryStorage,
		})
	}

	log.Printf("[StreamManager] Telemetry stream '%s' configured (subjects: %s)", StreamName, SubjectWildcard)
	return sm
}

// HandleSSE serves SSE for quanta.telemetry.{job_id} via a core NATS subscription.
// Messages are delivered live only (no durable replay). Requires authenticated job ownership.
func (sm *StreamManager) HandleSSE(c *gin.Context) {
	jobID := c.Param("job_id")
	if jobID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "job_id is required"})
		return
	}

	userID, ok := middleware.GetUserID(c)
	if !ok || userID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Authentication required"})
		return
	}

	if !authz.UserOwnsJob(sm.db, jobID, userID) {
		c.JSON(http.StatusNotFound, gin.H{"error": "Job not found"})
		return
	}

	subject := SubjectPrefix + jobID

	c.Writer.Header().Set("Content-Type", "text/event-stream")
	c.Writer.Header().Set("Cache-Control", "no-cache")
	c.Writer.Header().Set("Connection", "keep-alive")
	c.Writer.Header().Set("X-Accel-Buffering", "no")
	c.Writer.Flush()

	sub, err := sm.nc.SubscribeSync(subject)
	if err != nil {
		log.Printf("[StreamManager] Failed to subscribe to %s: %v", subject, err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "failed to subscribe to telemetry stream"})
		return
	}
	defer func() {
		if err := sub.Unsubscribe(); err != nil {
			log.Printf("[StreamManager] Unsubscribe error for %s: %v", subject, err)
		}
		log.Printf("[StreamManager] Cleaned up subscription for job %s", jobID)
	}()

	log.Printf("[StreamManager] SSE stream opened for job %s (subject: %s)", jobID, subject)

	ctx := c.Request.Context()
	keepAlive := time.NewTicker(SSEKeepAliveInterval)
	defer keepAlive.Stop()

	for {
		select {
		case <-ctx.Done():
			log.Printf("[StreamManager] Client disconnected from job %s stream", jobID)
			return

		case <-keepAlive.C:
			fmt.Fprintf(c.Writer, ": keepalive\n\n")
			c.Writer.Flush()

		default:
			msg, err := sub.NextMsg(1 * time.Second)
			if err != nil {
				if err == nats.ErrTimeout {
					continue
				}
				log.Printf("[StreamManager] NextMsg error for %s: %v", subject, err)
				return
			}

			fmt.Fprintf(c.Writer, "data: %s\n\n", string(msg.Data))
			c.Writer.Flush()
		}
	}
}
