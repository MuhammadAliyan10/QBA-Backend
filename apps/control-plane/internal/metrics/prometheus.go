package metrics

import (
	"log"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// Prometheus metrics for observability

var (
	// APIRequestsTotal tracks total API requests by method, endpoint, and status
	APIRequestsTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "api_requests_total",
			Help: "Total number of API requests",
		},
		[]string{"method", "endpoint", "status"},
	)

	// JobQueueCount tracks the number of jobs in different states
	JobQueueCount = promauto.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "job_queue_count",
			Help: "Current number of jobs by status (queued, running, completed, failed)",
		},
		[]string{"status"},
	)

	// RateLimitRejectionsTotal tracks rate limit rejections
	RateLimitRejectionsTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "rate_limit_rejections_total",
			Help: "Total number of requests rejected due to rate limiting",
		},
		[]string{"user_id"},
	)

	// WebhookDeliveryTotal tracks webhook delivery results
	WebhookDeliveryTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "webhook_delivery_total",
			Help: "Total webhook delivery attempts",
		},
		[]string{"status"}, // success, failed, retrying
	)

	// APIRequestDuration tracks request latency
	APIRequestDuration = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "api_request_duration_seconds",
			Help:    "API request duration in seconds",
			Buckets: prometheus.DefBuckets, // Default buckets: 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10
		},
		[]string{"method", "endpoint"},
	)

	// BillingCreditsDeducted tracks credit deductions
	BillingCreditsDeducted = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "billing_credits_deducted_total",
			Help: "Total credits deducted from users",
		},
		[]string{"user_id"},
	)

	// BillingInsufficientCredits tracks insufficient credit rejections
	BillingInsufficientCredits = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "billing_insufficient_credits_total",
			Help: "Total requests rejected due to insufficient credits",
		},
		[]string{"user_id"},
	)

	// TemporalWorkflowsStarted tracks Temporal workflow starts
	TemporalWorkflowsStarted = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "temporal_workflows_started_total",
			Help: "Total Temporal workflows started",
		},
		[]string{"workflow_id"},
	)

	// NATSEventsPublished tracks NATS event publications
	NATSEventsPublished = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "nats_events_published_total",
			Help: "Total events published to NATS",
		},
		[]string{"subject"},
	)

	// WebSocketActiveConnections tracks active WebSocket connections
	WebSocketActiveConnections = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "websocket_active_connections",
			Help: "Current number of active WebSocket connections",
		},
	)
)

// InitMetrics initializes the Prometheus metrics registry.
// Call this once during application startup.
func InitMetrics() {
	log.Println("[Metrics] Prometheus metrics initialized")

	// Initialize job queue gauges to 0
	JobQueueCount.WithLabelValues("queued").Set(0)
	JobQueueCount.WithLabelValues("running").Set(0)
	JobQueueCount.WithLabelValues("completed").Set(0)
	JobQueueCount.WithLabelValues("failed").Set(0)

	// Initialize WebSocket connections to 0
	WebSocketActiveConnections.Set(0)
}

// RecordAPIRequest records an API request with method, endpoint, and status.
func RecordAPIRequest(method, endpoint, status string) {
	APIRequestsTotal.WithLabelValues(method, endpoint, status).Inc()
}

// RecordRateLimitRejection records a rate limit rejection for a user.
func RecordRateLimitRejection(userID string) {
	RateLimitRejectionsTotal.WithLabelValues(userID).Inc()
}

// RecordWebhookDelivery records a webhook delivery attempt.
func RecordWebhookDelivery(status string) {
	WebhookDeliveryTotal.WithLabelValues(status).Inc()
}

// RecordBillingDeduction records a credit deduction.
func RecordBillingDeduction(userID string, amount int) {
	BillingCreditsDeducted.WithLabelValues(userID).Add(float64(amount))
}

// RecordInsufficientCredits records an insufficient credits rejection.
func RecordInsufficientCredits(userID string) {
	BillingInsufficientCredits.WithLabelValues(userID).Inc()
}

// RecordWorkflowStart records a Temporal workflow start.
func RecordWorkflowStart(workflowID string) {
	TemporalWorkflowsStarted.WithLabelValues(workflowID).Inc()
}

// RecordNATSEvent records a NATS event publication.
func RecordNATSEvent(subject string) {
	NATSEventsPublished.WithLabelValues(subject).Inc()
}

// UpdateJobQueueCount updates the job queue count for a specific status.
func UpdateJobQueueCount(status string, count float64) {
	JobQueueCount.WithLabelValues(status).Set(count)
}

// IncrementJobQueueCount increments the job queue count for a specific status.
func IncrementJobQueueCount(status string) {
	JobQueueCount.WithLabelValues(status).Inc()
}

// DecrementJobQueueCount decrements the job queue count for a specific status.
func DecrementJobQueueCount(status string) {
	JobQueueCount.WithLabelValues(status).Dec()
}

// IncrementWebSocketConnections increments active WebSocket connections.
func IncrementWebSocketConnections() {
	WebSocketActiveConnections.Inc()
}

// DecrementWebSocketConnections decrements active WebSocket connections.
func DecrementWebSocketConnections() {
	WebSocketActiveConnections.Dec()
}
