package notification

import (
	"e2e-platform/apps/control-plane/internal/db"
	"e2e-platform/apps/control-plane/internal/models"
	"encoding/json"
	"log"
	"os"

	"github.com/nats-io/nats.go"
)

// HumanInterventionEvent represents a request for human input during workflow execution
type HumanInterventionEvent struct {
	JobID         string   `json:"job_id"`
	Reason        string   `json:"reason"`
	PromptMessage string   `json:"prompt_message"`
	Options       []string `json:"options"` // e.g., ["Approve", "Deny"]
	Timestamp     int64    `json:"timestamp"`
}

// NotificationDispatcher handles human intervention alerts
type NotificationDispatcher struct {
	nc *nats.Conn
}

// NewDispatcher creates a new notification dispatcher
func NewDispatcher(nc *nats.Conn) *NotificationDispatcher {
	return &NotificationDispatcher{
		nc: nc,
	}
}

// StartListening begins listening for human intervention events
// This should be called as a goroutine in main.go
func (d *NotificationDispatcher) StartListening() {
	_, err := d.nc.Subscribe("job.alert.*", func(msg *nats.Msg) {
		d.handleAlert(msg)
	})

	if err != nil {
		log.Fatalf("Failed to subscribe to job.alert.*: %v", err)
	}

	log.Println("[System] Notification dispatcher started (listening on job.alert.*)")
}

// handleAlert processes a human intervention alert
func (d *NotificationDispatcher) handleAlert(msg *nats.Msg) {
	var event HumanInterventionEvent

	if err := json.Unmarshal(msg.Data, &event); err != nil {
		log.Printf("[Error] Failed to parse intervention event: %v", err)
		return
	}

	log.Printf("[Alert] Human intervention required for job %s", event.JobID)

	// ========================================
	// MOCK NOTIFICATION SERVICE
	// ========================================
	// In production, replace this with actual Twilio/WhatsApp/Email integration

	d.sendMockNotification(event)

	// ========================================
	// DATABASE UPDATE
	// ========================================
	// Mark job as waiting for user input

	err := d.updateJobStatus(event)
	if err != nil {
		log.Printf("[Error] Failed to update job status: %v", err)
	}
}

// sendMockNotification simulates sending a notification
// PRODUCTION TODO: Implement actual notification service integration
// Supported integrations: Twilio (WhatsApp/SMS), SendGrid (Email), Slack, Discord
//
// To enable in production:
// 1. Set ENABLE_NOTIFICATIONS=true in environment
// 2. Add required credentials (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, etc.)
// 3. Replace mock logic with actual API calls below
func (d *NotificationDispatcher) sendMockNotification(event HumanInterventionEvent) {
	// Check if notifications are enabled
	if notificationsEnabled := os.Getenv("ENABLE_NOTIFICATIONS"); notificationsEnabled != "true" {
		log.Printf("[Notification] SKIP: Notifications disabled (set ENABLE_NOTIFICATIONS=true to enable)")
		log.Printf("   Job ID: %s requires human intervention: %s", event.JobID, event.Reason)
		return
	}

	// ========================================
	// PRODUCTION: Uncomment ONE of the following integrations
	// ========================================

	// OPTION 1: Twilio WhatsApp/SMS
	/*
		twilioAccountSID := os.Getenv("TWILIO_ACCOUNT_SID")
		twilioAuthToken := os.Getenv("TWILIO_AUTH_TOKEN")
		twilioFromNumber := os.Getenv("TWILIO_FROM_NUMBER")
		userPhoneNumber := getUserPhoneNumber(event.JobID) // Fetch from DB

		twilioClient := twilio.NewRestClient()
		_, err := twilioClient.Api.CreateMessage(&twilioApi.CreateMessageParams{
			To:   userPhoneNumber,
			From: twilioFromNumber,
			Body: fmt.Sprintf("[Quanta] Job %s needs your attention: %s", event.JobID[:8], event.PromptMessage),
		})
		if err != nil {
			log.Printf("[Notification] Twilio error: %v", err)
		}
	*/

	// OPTION 2: SendGrid Email
	/*
		sendgridAPIKey := os.Getenv("SENDGRID_API_KEY")
		client := sendgrid.NewSendClient(sendgridAPIKey)
		from := mail.NewEmail("Quanta Alerts", "alerts@yourapp.com")
		to := mail.NewEmail("User", getUserEmail(event.JobID))
		subject := fmt.Sprintf("Action Required: Job %s", event.JobID[:8])
		htmlContent := fmt.Sprintf("<p>%s</p><p>Options: %v</p>", event.PromptMessage, event.Options)
		message := mail.NewSingleEmail(from, subject, to, event.PromptMessage, htmlContent)
		_, err := client.Send(message)
		if err != nil {
			log.Printf("[Notification] SendGrid error: %v", err)
		}
	*/

	// OPTION 3: Slack Webhook
	/*
		slackWebhookURL := os.Getenv("SLACK_WEBHOOK_URL")
		payload := map[string]string{
			"text": fmt.Sprintf("🚨 *Human Intervention Required*\n*Job:* %s\n*Reason:* %s\n*Message:* %s",
				event.JobID, event.Reason, event.PromptMessage),
		}
		jsonPayload, _ := json.Marshal(payload)
		http.Post(slackWebhookURL, "application/json", bytes.NewBuffer(jsonPayload))
	*/

	// FALLBACK: Log mock notification (current behavior)
	log.Printf("[Notification] MOCK: Would send notification to user")
	log.Printf("   Job ID: %s", event.JobID)
	log.Printf("   Message: %s", event.PromptMessage)
	log.Printf("   Options: %v", event.Options)
	log.Printf("   Reason: %s", event.Reason)
}

// updateJobStatus updates the database to mark job as waiting for user
func (d *NotificationDispatcher) updateJobStatus(event HumanInterventionEvent) error {
	// Use GORM to update job status
	// Assuming 'models.Job' is defined elsewhere and represents the 'jobs' table
	// and has fields like 'Status', 'InterventionReason', 'InterventionPrompt', 'InterventionOptions', 'UpdatedAt'.
	// If 'models.Job' is not available, this would need to be adjusted.
	err := db.DB.Model(&models.Job{}).
		Where("id = ?", event.JobID).
		Updates(map[string]interface{}{
			"status": "WAITING_FOR_USER",
		}).Error

	if err != nil {
		return err
	}

	log.Printf("[Database] Job %s status updated to WAITING_FOR_USER", event.JobID)
	return nil
}

// GetUserResponse retrieves the user's response to an intervention
// This would be called by a separate API endpoint (e.g., POST /jobs/:id/respond)
func (d *NotificationDispatcher) GetUserResponse(jobID string) (string, error) {
	var job models.Job

	err := db.DB.Select("status").Where("id = ?", jobID).First(&job).Error
	if err != nil {
		return "", err
	}

	return string(job.Status), nil
}

// Example usage in main.go:
/*
func main() {
	// ... existing setup ...

	nc, _ := nats.Connect(nats.DefaultURL)
	db, _ := sql.Open("postgres", dbDSN)

	// Start notification dispatcher
	dispatcher := notification.NewDispatcher(nc, db)
	go dispatcher.StartListening()

	// ... rest of main ...
}
*/
