package webhooks

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"e2e-platform/apps/control-plane/internal/streaming"
	"go.temporal.io/sdk/activity"
)

type WebhookPayload struct {
	Status        string            `json:"status"`
	ExtractedData []json.RawMessage `json:"extracted_data"`
	TokenCost     string            `json:"token_cost"`
	Latency       string            `json:"latency"`
}

func DispatchWebhookActivity(ctx context.Context, jobID string, callbackURL string, secretKey string) error {
	logger := activity.GetLogger(ctx)

	// 1. Fetch accumulated data
	rawChunks := streaming.GlobalAccumulator.RetrieveAndClear(jobID)
	var extractedData []json.RawMessage
	for _, chunk := range rawChunks {
		extractedData = append(extractedData, json.RawMessage(chunk))
	}

	// 2. Construct JSON payload
	payload := WebhookPayload{
		Status:        "success",
		ExtractedData: extractedData,
		TokenCost:     "calculated_by_pipeline", // Passed in via Temporal state in a real implementation
		Latency:       "calculated_by_pipeline",
	}

	payloadBytes, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("failed to marshal payload: %w", err)
	}

	// 3. Generate HMAC-SHA256 signature
	mac := hmac.New(sha256.New, []byte(secretKey))
	mac.Write(payloadBytes)
	signature := hex.EncodeToString(mac.Sum(nil))

	// 4 & 5. Execute HTTP POST
	req, err := http.NewRequestWithContext(ctx, "POST", callbackURL, bytes.NewReader(payloadBytes))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Quanta-Signature", signature)

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("webhook request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 500 || resp.StatusCode == 429 {
		// Return error to trigger Temporal's built-in retry
		return fmt.Errorf("webhook received transient error status: %d", resp.StatusCode)
	}

	if resp.StatusCode >= 400 {
		logger.Info("Webhook received client error status, dropping", "status", resp.StatusCode)
		return nil 
	}

	logger.Info("Webhook dispatched successfully", "jobID", jobID)
	return nil
}
