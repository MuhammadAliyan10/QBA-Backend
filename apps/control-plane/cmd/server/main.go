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

	// 1. Internal Modules
	"e2e-platform/apps/control-plane/internal/db"
	"e2e-platform/apps/control-plane/internal/ws"

	// 2. Third-Party Libraries
	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"
	"github.com/joho/godotenv"
	"github.com/nats-io/nats.go"
	"go.temporal.io/sdk/client"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	// 3. Generated Protobuf Contracts
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
	// Example subject: job.update.job-123456789
	_, err := c.nc.Subscribe("job.update.*", func(m *nats.Msg) {
		parts := strings.Split(m.Subject, ".")
		if len(parts) < 3 {
			return
		}
		jobID := parts[2]

		// 4. UNMARSHAL PROTOBUF (JobEvent)
		// We use the new 'JobEvent' message defined in events.proto
		var event pb.JobEvent
		if err := proto.Unmarshal(m.Data, &event); err != nil {
			log.Printf("[Error] Failed to unmarshal event: %v", err)
			return
		}

		// Log to server console for debugging
		log.Printf("[Event] Job %s Status: %s | Node: %s | Msg: %s",
			jobID, event.Status, event.NodeId, event.Message)

		// 5. CONVERT TO JSON FOR FRONTEND
		// The React frontend expects JSON, so we convert the binary Protobuf to JSON string
		// using protojson options to handle Enums and defaults correctly.
		marshaler := protojson.MarshalOptions{
			UseProtoNames:   true,
			EmitUnpopulated: true,
		}
		jsonBytes, err := marshaler.Marshal(&event)
		if err != nil {
			log.Printf("[Error] Failed to marshal JSON for WS: %v", err)
			return
		}

		// Broadcast to specific Job Channel
		c.ws.BroadcastToJob(jobID, jsonBytes)
	})

	if err != nil {
		log.Fatalf("[Error] Failed to subscribe to NATS: %v", err)
	}
	log.Println("[System] Listening for job updates on NATS (subject: job.update.*)")
}

func main() {
	// 1. Load Config
	if err := godotenv.Load(); err != nil {
		log.Println("[System] No .env file found, relying on system environment")
	}

	// 2. Initialize Database (CockroachDB/Postgres)
	db.Init()

	// 3. Connect to NATS (The Nervous System)
	natsURL := os.Getenv("NATS_URL")
	if natsURL == "" {
		natsURL = nats.DefaultURL
	}
	log.Printf("[System] Connecting to NATS at %s", natsURL)
	nc, err := nats.Connect(natsURL)
	if err != nil {
		log.Fatalf("[Error] Failed to connect to NATS: %v", err)
	}
	defer nc.Close()

	// 4. Connect to Temporal (The Orchestrator)
	temporalHost := os.Getenv("TEMPORAL_HOST")
	if temporalHost == "" {
		temporalHost = "localhost:7233"
	}
	log.Printf("[System] Connecting to Temporal at %s", temporalHost)

	temporalClient, err := client.Dial(client.Options{
		HostPort: temporalHost,
	})
	if err != nil {
		log.Fatalf("[Error] Failed to connect to Temporal: %v", err)
	}
	defer temporalClient.Close()

	// 5. Setup Components
	wsManager := ws.NewManager()
	consumer := NewConsumer(nc, wsManager)
	consumer.StartListening()

	// 6. Setup Gin Router
	r := gin.Default()
	r.Use(cors.Default())

	r.GET("/health", func(c *gin.Context) {
		c.JSON(200, gin.H{"status": "alive", "service": "control-plane"})
	})

	r.GET("/ws", func(c *gin.Context) {
		wsManager.HandleRequest(c)
	})

	// --- [ENDPOINT 1] START AUTOMATION JOB ---
	r.POST("/run", func(c *gin.Context) {
		// A. Parse Developer Request (JSON)
		// Matches the structure sent by your Dashboard or Curl
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

		// Generate a Unique Job ID
		jobID := fmt.Sprintf("job-%d", time.Now().Unix())

		// B. Prepare Data for Temporal
		// We package everything into a generic map because Python receives it as a dict.
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
		we, err := temporalClient.ExecuteWorkflow(context.Background(), workflowOptions, "BrowserWorkflow", workflowPayload)
		if err != nil {
			log.Printf("[Error] Temporal workflow start failed: %v", err)
			c.JSON(500, gin.H{"error": "Failed to start workflow"})
			return
		}

		c.JSON(202, gin.H{
			"message":  "Job Queued Successfully",
			"job_id":   jobID,
			"run_id":   we.GetRunID(),
			"trace_ws": fmt.Sprintf("/ws?job_id=%s", jobID),
		})
	})

	// --- [ENDPOINT 2] HUMAN-IN-THE-LOOP RESUME ---
	r.POST("/resume", func(c *gin.Context) {
		// A. Parse Resume Request (JSON)
		// e.g. {"job_id": "...", "data": {"otp": "123456"}}
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

		log.Printf("[Signal] Sending signal to workflow %s with data: %v", workflowID, req.Data)

		err := temporalClient.SignalWorkflow(
			context.Background(),
			workflowID,
			"",                 // Empty RunID = use the currently running execution
			"USER_INTERACTION", // Signal name (Matches Python @workflow.signal)
			req.Data,           // Signal payload
		)

		if err != nil {
			log.Printf("[Error] Signal failed for job %s: %v", req.JobID, err)
			c.JSON(500, gin.H{
				"success": false,
				"error":   "Failed to signal workflow",
				"details": err.Error(),
			})
			return
		}

		log.Printf("[Signal] Signal sent successfully to job %s", req.JobID)
		c.JSON(200, gin.H{
			"success": true,
			"message": "Workflow resumed",
			"job_id":  req.JobID,
		})
	})

	// 7. Start Server
	port := os.Getenv("PORT_GO_API")
	if port == "" {
		port = "8080"
	}

	srv := &http.Server{Addr: ":" + port, Handler: r}

	go func() {
		log.Printf("[System] Control plane server running on port %s", port)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %s\n", err)
		}
	}()

	// 8. Graceful Shutdown
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
