package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	// 1. Internal Modules
	"e2e-platform/apps/control-plane/internal/controllers"
	"e2e-platform/apps/control-plane/internal/db"
	"e2e-platform/apps/control-plane/internal/health"
	"e2e-platform/apps/control-plane/internal/metrics"
	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
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
		var event pb.JobEvent
		if err := proto.Unmarshal(m.Data, &event); err != nil {
			log.Printf("[Error] Failed to unmarshal event: %v", err)
			return
		}

		log.Printf("[Event] Job %s Status: %s | Node: %s | Msg: %s",
			jobID, event.Status, event.NodeId, event.Message)

		// 4.5 UPDATE JOB STATUS IN DATABASE
		// Map NATS event statuses to Prisma job_status enum values
		switch event.Status {
		case "RUNNING":
			now := time.Now()
			db.DB.Model(&models.Job{}).Where("id = ?", jobID).Updates(map[string]interface{}{
				"status":     "RUNNING",
				"started_at": &now,
			})
		case "COMPLETED":
			now := time.Now()
			db.DB.Model(&models.Job{}).Where("id = ?", jobID).Updates(map[string]interface{}{
				"status":       "COMPLETED",
				"completed_at": &now,
			})
			metrics.IncrementJobQueueCount("completed")
		case "FAILED":
			now := time.Now()
			errMsg := event.Message
			db.DB.Model(&models.Job{}).Where("id = ?", jobID).Updates(map[string]interface{}{
				"status":        "FAILED",
				"completed_at":  &now,
				"error_message": &errMsg,
			})
			metrics.IncrementJobQueueCount("failed")
		}

		// 5. CONVERT TO FRONTEND-COMPATIBLE JSON FOR WEBSOCKET
		// The frontend expects messages with {type, nodeId, status, message, ...}
		// We need to wrap the proto data in this format.
		frontendStatus := strings.ToLower(event.Status)
		if frontendStatus == "completed" {
			frontendStatus = "success"
		}

		// Send NODE_STATUS message (per-step updates)
		nodeMsg := map[string]interface{}{
			"type":    "NODE_STATUS",
			"nodeId":  event.NodeId,
			"status":  frontendStatus,
			"message": event.Message,
		}

		// Include screenshot if present
		if len(event.ScreenshotPreview) > 0 {
			nodeMsg["screenshot"] = event.ScreenshotPreview
		}

		nodeJSON, err := json.Marshal(nodeMsg)
		if err == nil {
			c.ws.BroadcastToJob(jobID, nodeJSON)
		}

		// Send LOG message (for the terminal view)
		logMsg := map[string]interface{}{
			"type":    "LOG",
			"level":   "info",
			"message": event.Message,
			"nodeId":  event.NodeId,
		}
		if event.Status == "FAILED" {
			logMsg["level"] = "error"
		}

		logJSON, err := json.Marshal(logMsg)
		if err == nil {
			c.ws.BroadcastToJob(jobID, logJSON)
		}

		// Send WORKFLOW_STATUS on terminal states
		if event.Status == "COMPLETED" || event.Status == "FAILED" {
			wsMsg := map[string]interface{}{
				"type":   "WORKFLOW_STATUS",
				"status": frontendStatus,
			}
			wsJSON, err := json.Marshal(wsMsg)
			if err == nil {
				c.ws.BroadcastToJob(jobID, wsJSON)
			}
		}

		// 6. TRIGGER WEBHOOK ON JOB COMPLETION/FAILURE
		// Only dispatch webhooks for terminal states
		if event.Status == "COMPLETED" || event.Status == "FAILED" {
			// Look up webhook URL from the user profile (via job -> user relation)
			// The Prisma schema stores webhook config on UserProfile, not on Job
			var webhookURL sql.NullString
			err := db.DB.Raw(`
				SELECT up.webhook_url
				FROM jobs j
				JOIN user_profiles up ON j.user_id = up.id
				WHERE j.id = ?
			`, jobID).Scan(&webhookURL).Error

			if err != nil {
				log.Printf("[Webhook] Could not look up webhook for job %s: %v", jobID, err)
				// Not critical — just skip webhook dispatch
				return
			}

			// Only dispatch if webhook URL is configured and non-empty
			if !webhookURL.Valid || webhookURL.String == "" {
				log.Printf("[Webhook] Job %s completed but user has no webhook URL configured", jobID)
				return
			}

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

	// CORS Configuration
	corsOrigins := os.Getenv("CORS_ORIGINS")
	if corsOrigins == "" {
		corsOrigins = "http://localhost:3000"
	}
	corsConfig := cors.DefaultConfig()
	corsConfig.AllowOrigins = strings.Split(corsOrigins, ",")
	corsConfig.AllowHeaders = []string{"Origin", "Content-Length", "Content-Type", "Authorization"}
	corsConfig.AllowMethods = []string{"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
	r.Use(cors.New(corsConfig))

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
	r.Match([]string{"GET", "HEAD"}, "/health", healthHandler.HandleHealth)           // Full health check (DB, Redis, NATS)
	r.Match([]string{"GET", "HEAD"}, "/health/live", healthHandler.HandleLiveness)     // Liveness probe (fast)
	r.Match([]string{"GET", "HEAD"}, "/health/ready", healthHandler.HandleReadiness)   // Readiness probe (full)

	// Prometheus metrics endpoint
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	r.GET("/ws", func(c *gin.Context) {
		wsManager.HandleRequest(c)
	})

	// --- [ENDPOINT 0] GENERATE WORKFLOW (AI) ---
	generatorCtrl := controllers.NewGeneratorController(temporalClient)
	r.POST("/api/v1/workflow/generate", generatorCtrl.HandleGenerate)         // Async - returns job_id
	r.POST("/api/v1/workflow/generate/sync", generatorCtrl.HandleGenerateSync) // Sync - waits for result

	// --- WORKFLOW & JOB MANAGEMENT ---
	// NOTE: Workflow CRUD is handled by Prisma server actions on the frontend.
	// The backend only handles execution orchestration via Temporal.
	workflowCtrl := controllers.NewWorkflowController(temporalClient)

	// Create authenticated route group
	auth := r.Group("/")
	auth.Use(middleware.AuthMiddleware())

	// Apply rate limiting if Redis is available
	if redisClient != nil {
		auth.Use(middleware.NewRateLimitMiddleware(redisClient).Middleware())
	}

	// Workflow execution (authenticated)
	auth.POST("/api/v1/workflows/:id/run", workflowCtrl.HandleExecute) // Frontend's runWorkflow()
	auth.POST("/api/v1/workflow/execute", workflowCtrl.HandleExecute)  // Legacy compatibility

	// Job management (authenticated)
	auth.GET("/v1/jobs", workflowCtrl.HandleListJobs)
	auth.GET("/v1/jobs/:id", workflowCtrl.HandleGetJob)
	auth.POST("/v1/jobs/:id/cancel", workflowCtrl.HandleCancelJob)
	auth.GET("/v1/jobs/:id/logs", workflowCtrl.HandleGetJobLogs)
	auth.POST("/v1/jobs/:id/resume", workflowCtrl.HandleResumeJob)

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
