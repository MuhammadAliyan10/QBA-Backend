package billing

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/nats-io/nats.go"
	"github.com/redis/go-redis/v9"
)

// PolarWebhookHandler processes Polar webhook events.
// Primary use case: checkout.completed → top up user credits
type PolarWebhookHandler struct {
	redis         *redis.Client
	nats          *nats.Conn
	webhookSecret string
}

// PolarEvent represents a Polar webhook event.
// See: https://docs.polar.sh/api/webhooks
type PolarEvent struct {
	Type      string                 `json:"type"`
	Data      map[string]interface{} `json:"data"`
	Timestamp time.Time              `json:"timestamp"`
}

// NewPolarWebhookHandler creates a new Polar webhook handler.
func NewPolarWebhookHandler(redisClient *redis.Client, natsConn *nats.Conn) *PolarWebhookHandler {
	webhookSecret := os.Getenv("POLAR_WEBHOOK_SECRET")
	if webhookSecret == "" {
		log.Println("⚠️  POLAR_WEBHOOK_SECRET not set. Webhook signature verification disabled.")
	}

	return &PolarWebhookHandler{
		redis:         redisClient,
		nats:          natsConn,
		webhookSecret: webhookSecret,
	}
}

// HandleWebhook is the Gin handler for /webhooks/polar
func (pwh *PolarWebhookHandler) HandleWebhook(c *gin.Context) {
	// Read request body
	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		log.Printf("❌ Failed to read webhook body: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request"})
		return
	}

	// Verify Polar signature
	signature := c.GetHeader("X-Polar-Signature")
	if pwh.webhookSecret != "" && !pwh.verifySignature(body, signature) {
		log.Println("❌ Invalid Polar signature")
		c.JSON(http.StatusUnauthorized, gin.H{"error": "Invalid signature"})
		return
	}

	// Parse event
	var event PolarEvent
	if err := json.Unmarshal(body, &event); err != nil {
		log.Printf("❌ Failed to parse Polar event: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid JSON"})
		return
	}

	log.Printf("📨 Received Polar event: %s", event.Type)

	// Handle different event types
	switch event.Type {
	case "checkout.completed":
		pwh.handleCheckoutCompleted(c.Request.Context(), &event)
	case "order.created":
		pwh.handleOrderCreated(c.Request.Context(), &event)
	case "subscription.created":
		pwh.handleSubscriptionCreated(c.Request.Context(), &event)
	default:
		log.Printf("ℹ️  Unhandled event type: %s", event.Type)
	}

	// Always return 200 to Polar
	c.JSON(http.StatusOK, gin.H{"received": true})
}

// verifySignature verifies the Polar webhook signature.
// Polar uses HMAC-SHA256 for webhook signatures
func (pwh *PolarWebhookHandler) verifySignature(payload []byte, signature string) bool {
	if pwh.webhookSecret == "" {
		return true // Skip verification in development
	}

	mac := hmac.New(sha256.New, []byte(pwh.webhookSecret))
	mac.Write(payload)
	expectedSignature := hex.EncodeToString(mac.Sum(nil))

	return hmac.Equal([]byte(signature), []byte(expectedSignature))
}

// handleCheckoutCompleted processes successful checkout events.
func (pwh *PolarWebhookHandler) handleCheckoutCompleted(ctx context.Context, event *PolarEvent) {
	// Extract relevant data from Polar event
	data := event.Data

	// Get user_id from custom fields
	customFields, ok := data["custom_fields"].(map[string]interface{})
	if !ok {
		log.Println("❌ No custom_fields in checkout")
		return
	}

	userID, ok := customFields["user_id"].(string)
	if !ok {
		log.Println("❌ No user_id in custom_fields")
		return
	}

	// Get product info
	product, ok := data["product"].(map[string]interface{})
	if !ok {
		log.Println("❌ No product in checkout")
		return
	}

	// Get credit amount from product metadata
	metadata, ok := product["metadata"].(map[string]interface{})
	if !ok {
		log.Println("❌ No metadata in product")
		return
	}

	creditsFloat, ok := metadata["credits"].(float64)
	if !ok {
		// Try to get from price
		priceAmount, ok := data["amount"].(float64)
		if !ok {
			log.Println("❌ No credits or amount in checkout")
			return
		}
		// Default conversion: $1 = 10 credits
		creditsFloat = priceAmount / 100
	}

	credits := int(creditsFloat)
	if credits <= 0 {
		log.Println("❌ Invalid credit amount")
		return
	}

	// Add credits to Redis
	key := fmt.Sprintf("user:%s:credits", userID)
	newBalance, err := pwh.redis.IncrBy(ctx, key, int64(credits)).Result()
	if err != nil {
		log.Printf("❌ Failed to add credits to Redis: %v", err)
		return
	}

	log.Printf("💰 Added %d credits to user %s. New balance: %d", credits, userID, newBalance)

	// Get checkout ID for metadata
	checkoutID, _ := data["id"].(string)

	// Publish billing event for ledger
	pwh.publishBillingEvent(userID, credits, int(newBalance), checkoutID)
}

// handleOrderCreated processes order creation events.
func (pwh *PolarWebhookHandler) handleOrderCreated(ctx context.Context, event *PolarEvent) {
	// Similar to checkout.completed
	// Polar orders can be recurring subscriptions
	pwh.handleCheckoutCompleted(ctx, event)
}

// handleSubscriptionCreated processes subscription events.
func (pwh *PolarWebhookHandler) handleSubscriptionCreated(ctx context.Context, event *PolarEvent) {
	data := event.Data

	userID, ok := data["user_id"].(string)
	if !ok {
		customFields, ok := data["custom_fields"].(map[string]interface{})
		if ok {
			userID, _ = customFields["user_id"].(string)
		}
	}

	if userID == "" {
		log.Println("❌ No user_id in subscription")
		return
	}

	// Get subscription plan credits (monthly allocation)
	plan, ok := data["plan"].(map[string]interface{})
	if !ok {
		return
	}

	metadata, ok := plan["metadata"].(map[string]interface{})
	if !ok {
		return
	}

	monthlyCredits := int(metadata["monthly_credits"].(float64))
	if monthlyCredits <= 0 {
		return
	}

	// Add monthly credits
	key := fmt.Sprintf("user:%s:credits", userID)
	newBalance, err := pwh.redis.IncrBy(ctx, key, int64(monthlyCredits)).Result()
	if err != nil {
		log.Printf("❌ Failed to add subscription credits: %v", err)
		return
	}

	log.Printf("🔄 Added %d subscription credits to user %s. Balance: %d", monthlyCredits, userID, newBalance)

	subscriptionID, _ := data["id"].(string)
	pwh.publishBillingEvent(userID, monthlyCredits, int(newBalance), subscriptionID)
}

// publishBillingEvent sends a billing event to NATS for async ledger write.
func (pwh *PolarWebhookHandler) publishBillingEvent(userID string, amount int, balanceAfter int, polarEventID string) {
	transactionID := uuid.New().String()

	metadata := map[string]string{
		"polar_event_id": polarEventID,
		"source":         "polar_webhook",
	}
	metadataJSON, _ := json.Marshal(metadata)

	event := map[string]interface{}{
		"user_id":        userID,
		"amount":         amount,
		"balance_after":  balanceAfter,
		"type":           "TOPUP",
		"transaction_id": transactionID,
		"metadata":       string(metadataJSON),
		"timestamp":      time.Now().Unix(),
	}

	data, _ := json.Marshal(event)

	if err := pwh.nats.Publish("billing.events", data); err != nil {
		log.Printf("⚠️  Failed to publish billing event: %v", err)
	} else {
		log.Printf("📨 Published TOPUP event for user %s", userID)
	}
}

// CreateCheckoutURL creates a Polar checkout session (helper for frontend).
// NOTE: This requires polar-go SDK in production
func CreateCheckoutURL(userID string, credits int) (string, error) {
	// Placeholder - implement with Polar API in production
	// Example using Polar API:
	// POST https://api.polar.sh/v1/checkouts
	// {
	//   "product_id": "prod_...",
	//   "custom_fields": {
	//     "user_id": "user_123"
	//   },
	//   "success_url": "https://yourapp.com/success",
	//   "cancel_url": "https://yourapp.com/cancel"
	// }

	polarAPIKey := os.Getenv("POLAR_API_KEY")
	if polarAPIKey == "" {
		return "", fmt.Errorf("POLAR_API_KEY not set")
	}

	// Return checkout URL (implement with HTTP client)
	return fmt.Sprintf("https://polar.sh/checkout?user_id=%s&credits=%d", userID, credits), nil
}
