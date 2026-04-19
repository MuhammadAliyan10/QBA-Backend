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
	"e2e-platform/apps/control-plane/internal/billing"
	appconfig "e2e-platform/apps/control-plane/internal/config"
	"e2e-platform/apps/control-plane/internal/controllers"
	"e2e-platform/apps/control-plane/internal/db"
	"e2e-platform/apps/control-plane/internal/health"
	"e2e-platform/apps/control-plane/internal/metrics"
	"e2e-platform/apps/control-plane/internal/middleware"
	"e2e-platform/apps/control-plane/internal/models"
	"e2e-platform/apps/control-plane/internal/services"
	"e2e-platform/apps/control-plane/internal/streaming"
	"e2e-platform/apps/control-plane/internal/temporal"
	"e2e-platform/apps/control-plane/internal/webhook"
	"e2e-platform/apps/control-plane/internal/ws"

	// 2. Third-Party Libraries
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"
	"github.com/joho/godotenv"
	"github.com/nats-io/nats.go"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
	"go.temporal.io/sdk/client"
	"google.golang.org/protobuf/proto"
	"gorm.io/datatypes"

	// 3. Generated Protobuf Contracts
	pb "e2e-platform/api/gen/go/v1"
)

// Consumer listens to NATS events
type Consumer struct {
	nc                *nats.Conn
	ws                *ws.Manager
	exporter          *services.ExporterService
	email             *services.EmailService
	webhookDisp       *webhook.WebhookDispatcher
	webhookSignSecret string
}

func NewConsumer(nc *nats.Conn, ws *ws.Manager, exp *services.ExporterService, email *services.EmailService, webhookDisp *webhook.WebhookDispatcher, webhookSignSecret string) *Consumer {
	return &Consumer{
		nc:                nc,
		ws:                ws,
		exporter:          exp,
		email:             email,
		webhookDisp:       webhookDisp,
		webhookSignSecret: webhookSignSecret,
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
			metrics.DecrementJobQueueCount("queued")
			metrics.IncrementJobQueueCount("running")
		case "COMPLETED":
			now := time.Now()
			db.DB.Model(&models.Job{}).Where("id = ?", jobID).Updates(map[string]interface{}{
				"status":       "COMPLETED",
				"completed_at": &now,
			})
			metrics.DecrementJobQueueCount("running")
			metrics.IncrementJobQueueCount("completed")

			// --- INDUSTRIAL: AUTOMATED DATA EXPORT & EMAIL ---
			go func() {
				// 1. Fetch User Data
				var user models.UserProfile
				err := db.DB.Raw(`
					SELECT up.* FROM user_profiles up
					JOIN jobs j ON j.user_id = up.id
					WHERE j.id = ?
				`, jobID).Scan(&user).Error
				if err != nil {
					log.Printf("[Export] Failed to fetch user for job %s: %v", jobID, err)
					return
				}

				// 2. Generate CSV
				csvData, err := c.exporter.ExportToCSV(jobID)
				if err != nil {
					log.Printf("[Export] No data to export for job %s: %v", jobID, err)
					// Still notify completion without attachment if no data
					c.email.SendWithAttachment(user.Email, "Quanta: Job Completed",
						fmt.Sprintf("<p>Your job %s has completed successfully.</p>", jobID), "", "")
					return
				}

				// 3. Send Email with Attachment
				subject := fmt.Sprintf("Quanta Report: Job %s", jobID[:8])
				body := fmt.Sprintf(`
					<h2>Workflow Completed!</h2>
					<p>Your automation job <b>%s</b> has finished successfully.</p>
					<p>Please find the extracted data attached as a CSV file.</p>
					<hr/>
					<p><small>Sent via Quanta Industrial Engine</small></p>
				`, jobID)

				fileName := fmt.Sprintf("quanta_report_%s.csv", jobID[:8])
				c.email.SendWithAttachment(user.Email, subject, body, csvData, fileName)
			}()
		case "FAILED":
			now := time.Now()
			errMsg := event.Message
			db.DB.Model(&models.Job{}).Where("id = ?", jobID).Updates(map[string]interface{}{
				"status":        "FAILED",
				"completed_at":  &now,
				"error_message": &errMsg,
			})
			metrics.DecrementJobQueueCount("running")
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

		// Include output data if present
		if event.Data != "" {
			var outputData map[string]interface{}
			if err := json.Unmarshal([]byte(event.Data), &outputData); err == nil {
				nodeMsg["output"] = outputData
			} else {
				// Fallback to raw string if not JSON
				nodeMsg["output"] = map[string]interface{}{"content": event.Data}
			}
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

		// 5.5 PERSIST LOG TO DATABASE (Industrial Persistence)
		// This enables historical log retrieval and Data Export (V1 Readiness)
		var metaData datatypes.JSON
		if event.Data != "" {
			metaData = datatypes.JSON(event.Data)
		}

		db.DB.Create(&models.JobLog{
			JobID:     jobID,
			Level:     event.Status,
			Message:   event.Message,
			NodeID:    &event.NodeId,
			Metadata:  &metaData,
			Timestamp: time.Now(),
		})

		// Send WORKFLOW_STATUS on terminal states
		if event.Status == "COMPLETED" || event.Status == "FAILED" {
			wsMsg := map[string]interface{}{
				"type":    "WORKFLOW_STATUS",
				"status":  frontendStatus,
				"message": event.Message, // CRITICAL: This contains the full JSON for nodes/edges
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

			// Build Webhook payload with flattened Reducer extraction maps
			reducerMap, _ := c.exporter.ReduceJobData(jobID)

			payload := webhook.WebhookPayload{
				JobID:     jobID,
				Status:    event.Status,
				Data:      reducerMap,
				Timestamp: time.Now().UTC().Format(time.RFC3339),
			}

			secret := strings.TrimSpace(c.webhookSignSecret)
			if secret == "" {
				log.Printf("[Webhook] WEBHOOK_SECRET not configured; skipping outbound webhook for job %s", jobID)
				return
			}

			c.webhookDisp.Dispatch(webhookURL.String, payload, secret)

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

	// 3. Initialize AWS & Webhook Dispatcher
	awsCfg, err := awsconfig.LoadDefaultConfig(context.TODO())
	var s3Client *s3.Client
	if err != nil {
		log.Printf("[AWS] Failed to load AWS config (large webhooks will embed Data/strip cleanly): %v", err)
	} else {
		s3Client = s3.NewFromConfig(awsCfg)
		log.Println("[AWS] S3 Configured System-Wide")
	}
	webhookDisp := webhook.NewWebhookDispatcher(s3Client, os.Getenv("S3_BUCKET_NAME"))

	// 4. Initialize Redis (Rate Limiting & Caching)
	redisURL := os.Getenv("REDIS_URL")
	if redisURL == "" {
		redisURL = "localhost:6379"
	}
	log.Printf("[System] Connecting to Redis at %s", redisURL)
	opt, err := redis.ParseURL(redisURL)
	if err != nil {
		log.Printf("[Warning] Redis URL parse failed: %v. Falling back to simple address.", err)
		opt = &redis.Options{
			Addr: redisURL,
		}
	}
	redisClient := redis.NewClient(opt)
	// Test Redis connection
	if err := redisClient.Ping(context.Background()).Err(); err != nil {
		log.Printf("[Warning] Redis connection failed: %v. Rate limiting will be disabled.", err)
		redisClient = nil // Disable if Redis unavailable
	} else {
		log.Println("[System] Connected to Redis successfully")
	}

	// 5. Initialize Prometheus Metrics
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

	sqlConn, err := db.DB.DB()
	if err != nil {
		log.Fatalf("[Database] Failed to get sql.DB handle: %v", err)
	}

	ldgr := billing.NewLedgerConsumer(sqlConn, nc)
	if err := ldgr.Start(); err != nil {
		log.Printf("[Ledger] WARNING: ledger consumer failed to start: %v", err)
	} else {
		defer func() {
			if err := ldgr.Stop(); err != nil {
				log.Printf("[Ledger] Stop error: %v", err)
			}
		}()
	}

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
	wsManager := ws.NewManager(db.GetDB())

	exporterSvc := services.NewExporterService()
	emailSvc := services.NewEmailService()

	logicValidator, err := services.NewLogicValidator()
	if err != nil {
		log.Fatalf("[Logic] %v", err)
	}

	webhookSignSecret := strings.TrimSpace(os.Getenv("WEBHOOK_SECRET"))
	if os.Getenv("GIN_MODE") == "release" && webhookSignSecret == "" {
		log.Println("[WARN] WEBHOOK_SECRET is empty in release mode — signed completion webhooks will be skipped")
	}

	consumer := NewConsumer(nc, wsManager, exporterSvc, emailSvc, webhookDisp, webhookSignSecret)
	consumer.StartListening()

	// Initialize and start Scheduler
	scheduler := services.NewSchedulerService(temporalClient)
	scheduler.Start()
	defer scheduler.Stop()

	// 7b. Initialize Async Execution subsystem
	//     TemporalManager wraps the existing client for the new /v1/execute flow.
	//     StreamManager initializes JetStream for real-time telemetry SSE.
	tm := temporal.Wrap(temporalClient)
	streamMgr := streaming.NewStreamManager(nc, db.GetDB())
	executeCtrl := controllers.NewExecuteController(db.GetDB(), tm, logicValidator)

	// 8. Setup Gin Router
	r := gin.Default()

	corsCfg := middleware.DefaultCORS()
	r.Use(cors.New(corsCfg))

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

	r.GET("/metrics", middleware.MetricsTokenAuth(), gin.WrapH(promhttp.Handler()))

	polarH := billing.NewPolarWebhookHandler(redisClient, nc)
	r.POST("/webhooks/polar", polarH.HandleWebhook)

	protected := r.Group("/")
	protected.Use(middleware.AuthMiddleware())

	if redisClient != nil {
		protected.Use(middleware.NewRateLimitMiddleware(redisClient).Middleware())
	}

	generatorCtrl := controllers.NewGeneratorController(temporalClient, logicValidator)
	protected.POST("/api/v1/workflow/generate", generatorCtrl.HandleGenerate)
	protected.POST("/api/v1/workflow/generate/sync", generatorCtrl.HandleGenerateSync)

	workflowCtrl := controllers.NewWorkflowController(temporalClient)

	compute := protected.Group("/")
	if appconfig.IsBillingEnabled() && redisClient != nil {
		compute.Use(middleware.NewBillingMiddleware(redisClient, nc).Middleware())
	} else if appconfig.IsBillingEnabled() && redisClient == nil {
		log.Println("[WARN] ENABLE_BILLING=true but Redis unavailable — billing enforcement disabled for compute routes")
	}

	compute.POST("/v1/execute", executeCtrl.HandleExecuteAsync)
	compute.POST("/api/v1/workflows/:id/run", workflowCtrl.HandleExecute)
	compute.POST("/api/v1/workflow/execute", workflowCtrl.HandleExecute)

	protected.GET("/v1/jobs", workflowCtrl.HandleListJobs)
	protected.GET("/v1/jobs/:id", workflowCtrl.HandleGetJob)
	protected.POST("/v1/jobs/:id/cancel", workflowCtrl.HandleCancelJob)
	protected.GET("/v1/jobs/:id/logs", workflowCtrl.HandleGetJobLogs)
	protected.POST("/v1/jobs/:id/resume", workflowCtrl.HandleResumeJob)

	protected.GET("/v1/execute/:job_id/stream", streamMgr.HandleSSE)

	protected.GET("/ws", wsManager.HandleRequest)

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
