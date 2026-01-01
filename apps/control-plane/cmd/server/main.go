package main

import (
	"context"
	"database/sql"
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
	"e2e-platform/apps/control-plane/internal/health"
	"e2e-platform/apps/control-plane/internal/metrics"
	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/webhook"
	"e2e-platform/apps/control-plane/internal/ws"

	// 2. Third-Party Libraries
	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"
	"github.com/joho/godotenv"
	"github.com/nats-io/nats.go"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
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

		// 6. TRIGGER WEBHOOK ON JOB COMPLETION/FAILURE
		// Only dispatch webhooks for terminal states
		if event.Status == "COMPLETED" || event.Status == "FAILED" {
			// Query webhook_url from jobs table
			var webhookURL sql.NullString
			err := db.DB.Raw(
				"SELECT webhook_url FROM jobs WHERE id = ?",
				jobID,
			).Scan(&webhookURL).Error

			// If webhook URL exists, dispatch notification
			if err == nil && webhookURL.Valid {
				// Build webhook payload
				payload := webhook.WebhookPayload{
					JobID:     jobID,
					Status:    event.Status,
					Data: map[string]interface{}{
						"message": event.Message,
						"node_id": event.NodeId,
					},
					Timestamp: time.Now().UTC().Format(time.RFC3339),
				}

				// PRODUCTION GATE: Enforce secure configuration in release mode
				secret := os.Getenv("WEBHOOK_SECRET")
				if secret == "" {
					// Check if running in production (Gin release mode)
					ginMode := os.Getenv("GIN_MODE")
					if ginMode == "release" {
						// CRITICAL: Do not allow insecure production deployment
						log.Fatal("[SECURITY] WEBHOOK_SECRET must be set in production. Refusing to start.")
					} else {
						// Development mode: Allow with loud warning and temporary secret
						log.Println("[WARNING] WEBHOOK_SECRET not set. Using temporary dev secret. DO NOT USE IN PRODUCTION!")
						secret = fmt.Sprintf("dev_temp_secret_%d", time.Now().UnixNano())
					}
				}

				// Dispatch webhook asynchronously (non-blocking)
				go webhook.Dispatch(webhookURL.String, payload, secret)

				log.Printf("[Webhook] Dispatching webhook for job %s to %s", jobID, webhookURL.String)
			} else if err != nil && err != sql.ErrNoRows {
				log.Printf("[Webhook] Database error querying webhook_url for job %s: %v", jobID, err)
			}
		}
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

	// 2. Initialize Database (PostgreSQL via Supabase)
	db.Init()

	// 3. Initialize Redis (Rate Limiting & Caching)
	redisURL := os.Getenv("REDIS_URL")
	if redisURL == "" {
		redisURL = "localhost:6379"
	}
	log.Printf("[System] Connecting to Redis at %s", redisURL)
	redisClient := redis.NewClient(&redis.Options{
		Addr: redisURL,
	})
	// Test Redis connection
	if err := redisClient.Ping(context.Background()).Err(); err != nil {
		log.Printf("[Warning] Redis connection failed: %v. Rate limiting will be disabled.", err)
		redisClient = nil // Disable if Redis unavailable
	} else {
		log.Println("[System] Connected to Redis successfully")
	}

	// 4. Initialize Prometheus Metrics
	metrics.InitMetrics()

	// 5. Connect to NATS (The Nervous System) - INDUSTRIAL GRADE
	natsURL := os.Getenv("NATS_URL")
	if natsURL == "" {
		natsURL = nats.DefaultURL
	}
	log.Printf("[System] Connecting to NATS at %s", natsURL)
	nc, err := nats.Connect(natsURL,
		nats.ReconnectWait(2*time.Second),
		nats.MaxReconnects(-1), // Retry forever
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			log.Printf("[NATS] Disconnected: %v", err)
		}),
		nats.ReconnectHandler(func(_ *nats.Conn) {
			log.Printf("[NATS] Reconnected successfully")
		}),
	)
	if err != nil {
		log.Fatalf("[Error] Failed to connect to NATS: %v", err)
	}
	defer nc.Close()

	// 6. Connect to Temporal (The Orchestrator)
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

	// 7. Setup Components
	wsManager := ws.NewManager()
	consumer := NewConsumer(nc, wsManager)
	consumer.StartListening()

	// 8. Setup Gin Router
	r := gin.Default()
	r.Use(cors.Default())

	// Add metrics middleware to track all requests
	r.Use(func(c *gin.Context) {
		start := time.Now()
		c.Next()
		duration := time.Since(start)
		status := fmt.Sprintf("%d", c.Writer.Status())
		metrics.RecordAPIRequest(c.Request.Method, c.Request.URL.Path, status)
		metrics.APIRequestDuration.WithLabelValues(c.Request.Method, c.Request.URL.Path).Observe(duration.Seconds())
	})

	// Health Check Endpoints (For Azure Container Apps, Kubernetes, etc.)
	healthHandler := health.NewHealthHandler(redisClient, nc)
	r.GET("/health", healthHandler.HandleHealth)           // Full health check (DB, Redis, NATS)
	r.GET("/health/live", healthHandler.HandleLiveness)     // Liveness probe (fast)
	r.GET("/health/ready", healthHandler.HandleReadiness)   // Readiness probe (full)

	// Prometheus metrics endpoint
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	r.GET("/ws", func(c *gin.Context) {
		wsManager.HandleRequest(c)
	})

	// --- [ENDPOINT 1] START AUTOMATION JOB ---
	// Create protected route group with rate limiting
	protected := r.Group("/")
	if redisClient != nil {
		// Apply rate limiting if Redis is available
		protected.Use(func(c *gin.Context) {
			// Dev mode: allow X-User-ID header for testing
			if userID := c.GetHeader("X-User-ID"); userID != "" {
				c.Set("userID", userID)
			}
			c.Next()
		})
		protected.Use(middleware.NewRateLimitMiddleware(redisClient).Middleware())
	}

	protected.POST("/run", func(c *gin.Context) {
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

		// C. Start the Workflow (with timeout to prevent hanging)
		execCtx, execCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer execCancel()

		we, err := temporalClient.ExecuteWorkflow(execCtx, workflowOptions, "BrowserWorkflow", workflowPayload)
		if err != nil {
			log.Printf("[Error] Temporal workflow start failed: %v", err)
			c.JSON(500, gin.H{"error": "Failed to start workflow"})
			return
		}

		// Record metrics
		metrics.RecordWorkflowStart(req.WorkflowID)
		metrics.IncrementJobQueueCount("queued")

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

		// Context with timeout to prevent hanging
		signalCtx, signalCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer signalCancel()

		err := temporalClient.SignalWorkflow(
			signalCtx,
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

	// 9. Start Server
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

	// 10. Graceful Shutdown
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
