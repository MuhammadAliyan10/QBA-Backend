package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	// FIX: Correct imports
	"e2e-platform/apps/control-plane/internal/ws"

	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"
	"github.com/joho/godotenv"
	"github.com/nats-io/nats.go"
	"go.temporal.io/sdk/client"
	"google.golang.org/protobuf/proto"

	// Import generated protobufs
	pb "e2e-platform/api/gen/go/v1"
)

// Consumer listens to NATS events
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
	_, err := c.nc.Subscribe("job.update.*", func(m *nats.Msg) {
		parts := strings.Split(m.Subject, ".")
		if len(parts) < 3 {
			return
		}
		jobID := parts[2]

		log.Printf("📨 Received Event for %s", jobID)

		// Unmarshal Protobuf
		var event pb.StepUpdateEvent
		if err := proto.Unmarshal(m.Data, &event); err != nil {
			log.Printf("❌ Failed to unmarshal event: %v", err)
			return
		}

		log.Printf("   Status: %s | Msg: %s", event.Status, event.LogMessage)

		// Push to WebSocket (Serialize to JSON for frontend)
		// In a real app, we might send binary or JSON. React Flow usually likes JSON.
		// For now, let's just broadcast the raw bytes or a JSON wrapper.
		// Let's re-marshal to JSON for the frontend.
		// Or just send the struct if wsManager handles it.
		c.ws.BroadcastToJob(jobID, m.Data) // Assuming frontend can handle it or we change this later.
	})

	if err != nil {
		log.Fatalf("❌ Failed to subscribe to NATS: %v", err)
	}
	log.Println("👂 Listening for Job Updates on NATS...")
}

func main() {
	// 1. Load Config
	// Trying 3 levels up since we run from apps/control-plane/cmd/server/
	if err := godotenv.Load("../../../.env"); err != nil {
		// Fallback for running via 'make run-go'
		if err := godotenv.Load("../../.env"); err != nil {
			log.Println("⚠️  No .env file found, relying on system env")
		}
	}

	// 2. Connect to NATS
	natsURL := os.Getenv("NATS_URL")
	if natsURL == "" {
		natsURL = nats.DefaultURL
	}
	log.Printf("🔌 Connecting to NATS at %s...", natsURL)
	nc, err := nats.Connect(natsURL)
	if err != nil {
		log.Fatalf("❌ Failed to connect to NATS: %v", err)
	}
	defer nc.Close()
	log.Println("✅ NATS JetStream Connected!")

	// 3. Connect to Temporal
	temporalHost := os.Getenv("TEMPORAL_HOST")
	if temporalHost == "" {
		temporalHost = "localhost:7233"
	}
	log.Printf("⏳ Connecting to Temporal at %s...", temporalHost)

	temporalClient, err := client.Dial(client.Options{
		HostPort: temporalHost,
	})
	if err != nil {
		log.Fatalf("❌ Failed to connect to Temporal: %v", err)
	}
	defer temporalClient.Close()
	log.Println("✅ Temporal Connected!")

	// 4. Setup Components
	wsManager := ws.NewManager()
	consumer := NewConsumer(nc, wsManager)
	consumer.StartListening()

	// 5. Setup Gin
	r := gin.Default()
	r.Use(cors.Default())

	r.GET("/health", func(c *gin.Context) {
		c.JSON(200, gin.H{"status": "alive"})
	})

	r.GET("/ws", func(c *gin.Context) {
		wsManager.HandleRequest(c)
	})

	r.POST("/run", func(c *gin.Context) {
		jobID := fmt.Sprintf("job-%d", time.Now().Unix())

		steps := []*pb.BrowserStepInput{
			{
				JobId:  jobID,
				NodeId: "node-1",
				Action: "GOTO",
				Params: map[string]string{"url": "http://localhost:8888/test_page.html"},
			},
			// Test 1: Click login button (Should be found by Sniper)
			{
				JobId:  jobID,
				NodeId: "node-2",
				Action: "CLICK",
				Params: map[string]string{"intent": "login"},
			},
			// Test 2: Type in username field
			{
				JobId:  jobID,
				NodeId: "node-3",
				Action: "TYPE",
				Params: map[string]string{"intent": "username", "text": "test_user_123"},
			},
			// Test 3: Scroll down
			{
				JobId:  jobID,
				NodeId: "node-4",
				Action: "SCROLL",
				Params: map[string]string{"direction": "down", "amount": "300"},
			},
		}

		workflowOptions := client.StartWorkflowOptions{
			ID:        "workflow-" + jobID,
			TaskQueue: "e2e-browser-tasks",
		}

		// Convert to generic map to avoid Temporal Protobuf decoding issues in Python
		// This is a quick fix. Ideally we use a custom DataConverter.
		var stepsData []map[string]interface{}
		for _, step := range steps {
			stepsData = append(stepsData, map[string]interface{}{
				"job_id":  step.JobId,
				"node_id": step.NodeId,
				"action":  step.Action,
				"params":  step.Params,
			})
		}

		we, err := temporalClient.ExecuteWorkflow(context.Background(), workflowOptions, "BrowserWorkflow", stepsData)
		if err != nil {
			c.JSON(500, gin.H{"error": err.Error()})
			return
		}

		c.JSON(200, gin.H{
			"message": "Mission Launched 🚀",
			"job_id":  jobID,
			"run_id":  we.GetRunID(),
		})
	})

	// Start Server
	port := os.Getenv("PORT_GO_API")
	if port == "" {
		port = "8080"
	}

	srv := &http.Server{Addr: ":" + port, Handler: r}

	go func() {
		log.Printf("🚀 Control Plane Running on port %s", port)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %s\n", err)
		}
	}()

	// Graceful Shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("Shutting down...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatal("Forced shutdown: ", err)
	}
}
