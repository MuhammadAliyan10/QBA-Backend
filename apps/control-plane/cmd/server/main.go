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

	// 1. IMPORT YOUR WS MANAGER
	"e2e-platform/apps/control-plane/internal/ws"

	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"
	"github.com/joho/godotenv"
	"github.com/nats-io/nats.go"
	"go.temporal.io/sdk/client"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	// 2. IMPORT GENERATED PROTOBUFS
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

		// 3. UNMARSHAL PROTOBUF (StepUpdateEvent)
		var event pb.StepUpdateEvent
		if err := proto.Unmarshal(m.Data, &event); err != nil {
			log.Printf("❌ Failed to unmarshal event: %v", err)
			return
		}

		log.Printf("📨 [Job %s] Status: %s | Node: %s | Msg: %s", jobID, event.Status, event.NodeId, event.LogMessage)

		// 4. CONVERT TO JSON FOR FRONTEND
        // React/Frontend expects JSON, not binary Protobuf
        jsonBytes, _ := protojson.Marshal(&event)
		c.ws.BroadcastToJob(jobID, jsonBytes)
	})

	if err != nil {
		log.Fatalf("❌ Failed to subscribe to NATS: %v", err)
	}
	log.Println("👂 Listening for Job Updates on NATS (Subject: job.update.*)...")
}

func main() {
	// 1. Load Config
	if err := godotenv.Load(); err != nil {
        log.Println("⚠️  No .env file found, relying on system env")
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

	// 4. Setup Components
	wsManager := ws.NewManager()
	consumer := NewConsumer(nc, wsManager)
	consumer.StartListening()

	// 5. Setup Gin
	r := gin.Default()
	r.Use(cors.Default())

	r.GET("/health", func(c *gin.Context) {
		c.JSON(200, gin.H{"status": "alive", "service": "control-plane"})
	})

	r.GET("/ws", func(c *gin.Context) {
		wsManager.HandleRequest(c)
	})

    // --- THE MAIN API ENDPOINT ---
	r.POST("/run", func(c *gin.Context) {
        // A. Parse Developer Request (JSON)
        // We use a struct that matches the Protocol Buffer "ExecuteWorkflowRequest"
        var req struct {
            WorkflowID string            `json:"workflow_id"`
            Params     map[string]string `json:"params"`
            Config     struct {
                UsePremiumProxy bool   `json:"use_premium_proxy"`
                SolveCaptchas   bool   `json:"solve_captchas"`
                SessionID       string `json:"session_id"`
                Region          string `json:"region"`
            } `json:"config"`
        }

        if err := c.BindJSON(&req); err != nil {
            c.JSON(400, gin.H{"error": "Invalid JSON body"})
            return
        }

		jobID := fmt.Sprintf("job-%d", time.Now().Unix())

        // B. Prepare Data for Temporal
        // The Python Worker expects a single argument: The "Job Payload"
        // We package everything into a clean Go Map so Python receives a Dictionary.
        workflowPayload := map[string]interface{}{
            "job_id":      jobID,
            "workflow_id": req.WorkflowID,
            "params":      req.Params,
            "config": map[string]interface{}{
                "use_premium_proxy": req.Config.UsePremiumProxy,
                "solve_captchas":    req.Config.SolveCaptchas,
                "session_id":        req.Config.SessionID,
                "region":            req.Config.Region,
            },
        }

		workflowOptions := client.StartWorkflowOptions{
			ID:        "workflow-" + jobID,
			TaskQueue: "e2e-browser-tasks",
		}

        // C. Start the Workflow
        // Note: We are now passing the DYNAMIC payload, not hardcoded steps.
        // The Python Worker will load the "Recipe" based on `workflow_id`.
		we, err := temporalClient.ExecuteWorkflow(context.Background(), workflowOptions, "BrowserWorkflow", workflowPayload)
		if err != nil {
			log.Printf("❌ Temporal Start Failed: %v", err)
			c.JSON(500, gin.H{"error": "Failed to start workflow"})
			return
		}

		c.JSON(202, gin.H{
			"message": "Job Queued Successfully",
			"job_id":  jobID,
			"run_id":  we.GetRunID(),
			"trace_ws": fmt.Sprintf("/ws?job_id=%s", jobID),
		})
	})

	// --- HUMAN-IN-THE-LOOP RESUME ENDPOINT ---
	r.POST("/resume", func(c *gin.Context) {
		// A. Parse Resume Request (JSON)
		var req struct {
			JobID string            `json:"job_id"`
			Data  map[string]string `json:"data"`
		}

		if err := c.BindJSON(&req); err != nil {
			c.JSON(400, gin.H{"error": "Invalid JSON body"})
			return
		}

		if req.JobID == "" {
			c.JSON(400, gin.H{"error": "job_id is required"})
			return
		}

		// B. Send Temporal Signal to the Running Workflow
		workflowID := "workflow-" + req.JobID

		log.Printf("📨 Sending signal to workflow %s with data: %v", workflowID, req.Data)

		err := temporalClient.SignalWorkflow(
			context.Background(),
			workflowID,
			"",                    // Empty RunID = use the currently running execution
			"USER_INTERACTION",    // Signal name (must match Python @workflow.signal)
			req.Data,              // Signal payload (map[string]string)
		)

		if err != nil {
			log.Printf("❌ Signal Failed for Job %s: %v", req.JobID, err)
			c.JSON(500, gin.H{
				"success": false,
				"error":   "Failed to signal workflow",
				"details": err.Error(),
			})
			return
		}

		log.Printf("✅ Signal sent successfully to Job %s", req.JobID)
		c.JSON(200, gin.H{
			"success": true,
			"message": "Workflow resumed",
			"job_id":  req.JobID,
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
