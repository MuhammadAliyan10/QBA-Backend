package events

import (
	"e2e-platform/apps/control-plane/internal/ws"
	"log"
	"strings"

	"github.com/nats-io/nats.go"
)

type Consumer struct {
	nc *nats.Conn
	ws *ws.Manager
}

func NewConsumer(nc *nats.Conn, ws *ws.Manager) *Consumer {
	return &Consumer{
		nc: nc,
		ws: ws,
	}
}

func (c *Consumer) StartListening() {
	// Subscribe to ALL job updates (job.update.*)
	// The wildcard '*' captures the Job ID
	_, err := c.nc.Subscribe("job.update.*", func(m *nats.Msg) {
		// 1. Extract Job ID from Subject (job.update.JOB_123)
		parts := strings.Split(m.Subject, ".")
		if len(parts) < 3 {
			return
		}
		jobID := parts[2]

		// 2. Log it (for debugging)
		log.Printf("📨 Received Event for %s: %s", jobID, string(m.Data))

		// 3. Push to WebSocket
		// We send the raw bytes directly to the frontend. Zero copy.
		c.ws.BroadcastToJob(jobID, m.Data)
	})

	if err != nil {
		log.Fatalf("[ERROR] Failed to subscribe to NATS: %v", err)
	}

	log.Println("👂 Listening for Job Updates on NATS...")
}
