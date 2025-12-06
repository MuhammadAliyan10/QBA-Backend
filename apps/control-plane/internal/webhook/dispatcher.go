package webhook

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"
)

// WebhookPayload represents the data sent to user's webhook endpoint
type WebhookPayload struct {
	JobID     string                 `json:"job_id"`
	Status    string                 `json:"status"`
	ResultURL string                 `json:"result_url,omitempty"`
	Data      map[string]interface{} `json:"data,omitempty"`
	Timestamp string                 `json:"timestamp"`
}

// Dispatch sends an HMAC-signed webhook with retry logic
// This function is designed to be called in a goroutine for non-blocking execution
//
// Parameters:
//   - url: User's webhook endpoint
//   - payload: Data to send (will be JSON-encoded)
//   - secret: HMAC secret key (typically user's API key)
//
// Retry Strategy:
//   - Attempt 1: Immediate
//   - Attempt 2: 1s delay
//   - Attempt 3: 3s delay
//   - Attempt 4: 10s delay
func Dispatch(url string, payload interface{}, secret string) {
	const maxRetries = 3
	retryDelays := []time.Duration{0, 1 * time.Second, 3 * time.Second, 10 * time.Second}

	// Marshal payload to JSON
	jsonBytes, err := json.Marshal(payload)
	if err != nil {
		log.Printf("[Webhook] Invalid payload: %v", err)
		return
	}

	// Generate HMAC-SHA256 signature
	signature := generateHMAC(jsonBytes, secret)

	// Retry loop
	for attempt := 0; attempt <= maxRetries; attempt++ {
		// Apply backoff delay (skip for first attempt)
		if attempt > 0 {
			delay := retryDelays[attempt]
			log.Printf("⏳ Webhook retry #%d after %v delay...", attempt, delay)
			time.Sleep(delay)
		}

		// Send HTTP POST request
		req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonBytes))
		if err != nil {
			log.Printf("[Webhook] Invalid URL: %v", err)
			return
		}

		// Set headers
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-E2E-Signature", signature)
		req.Header.Set("X-E2E-Event", "job.completed")
		req.Header.Set("User-Agent", "E2E-Platform-Webhook/1.0")

		// Execute request with timeout
		client := &http.Client{
			Timeout: 10 * time.Second,
		}

		resp, err := client.Do(req)

		// Handle network errors or timeouts
		if err != nil {
			log.Printf("[Webhook] Attempt #%d failed: %v", attempt+1, err)
			if attempt == maxRetries {
				log.Printf("[Webhook] DEAD LETTER: %s (max retries exceeded)", url)
			}
			continue
		}

		// Read response body for logging
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()

		// Check HTTP status code
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			log.Printf("[Webhook] Delivery successful: %s (status: %d, attempt: %d)", url, resp.StatusCode, attempt+1)
			return
		}

		// Log failure and retry on 5xx errors
		if resp.StatusCode >= 500 {
			log.Printf("[Webhook] Server error: %d - %s (attempt: %d)", resp.StatusCode, string(body), attempt+1)
			if attempt == maxRetries {
				log.Printf("[Webhook] DEAD LETTER: %s (status: %d, max retries exceeded)", url, resp.StatusCode)
			}
			continue
		}

		// Client errors (4xx) - don't retry
		log.Printf("[Webhook] Rejected: %s (status: %d, body: %s)", url, resp.StatusCode, string(body))
		return
	}
}

// generateHMAC creates an HMAC-SHA256 signature for the payload
func generateHMAC(payload []byte, secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(payload)
	return hex.EncodeToString(mac.Sum(nil))
}

// VerifyHMAC verifies an HMAC signature (utility for testing/validation)
func VerifyHMAC(payload []byte, signature string, secret string) bool {
	expectedSignature := generateHMAC(payload, secret)
	return hmac.Equal([]byte(signature), []byte(expectedSignature))
}

// Example usage and testing
func ExampleDispatch() {
	payload := WebhookPayload{
		JobID:     "550e8400-e29b-41d4-a716-446655440000",
		Status:    "COMPLETED",
		ResultURL: "s3://bucket/result.json",
		Data: map[string]interface{}{
			"duration_ms": 1523,
			"steps_count": 5,
		},
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}

	// Non-blocking dispatch
	go Dispatch("https://example.com/webhook", payload, "secret_key_123")

	fmt.Println("Webhook dispatched asynchronously")
}
