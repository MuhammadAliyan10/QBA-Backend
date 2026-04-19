package webhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// WebhookPayload represents the optimized data sent to user's webhook endpoint
type WebhookPayload struct {
	JobID       string                 `json:"job_id"`
	Status      string                 `json:"status"`
	ResultURL   string                 `json:"result_url,omitempty"`   // Legacy/Other URLs
	DownloadURL string                 `json:"download_url,omitempty"` // S3 Presigned URL if payload >5MB
	Data        map[string]interface{} `json:"data,omitempty"`         // Flattened Reducer Output
	Timestamp   string                 `json:"timestamp"`
}

// WebhookDispatcher manages secure, payload-optimized webhook delivery
type WebhookDispatcher struct {
	s3Client   *s3.Client
	bucketName string
	presignSvc *s3.PresignClient
}

// NewWebhookDispatcher initializes the singleton dispatcher injected with AWS
func NewWebhookDispatcher(s3Client *s3.Client, bucketName string) *WebhookDispatcher {
	var presignClient *s3.PresignClient
	if s3Client != nil {
		presignClient = s3.NewPresignClient(s3Client)
	}

	return &WebhookDispatcher{
		s3Client:   s3Client,
		bucketName: bucketName,
		presignSvc: presignClient,
	}
}

// Dispatch executes the webhook delivery. It intercepts the payload, measures
// the size of the flatten data, and uploads to S3 natively if >5MB.
// It signs the final JSON representation using HMAC-SHA256.
func (wd *WebhookDispatcher) Dispatch(url string, payload WebhookPayload, secret string) {
	// Size limit threshold: 5MB
	const MaxInlineDataBytes = 5 * 1024 * 1024

	// Estimate size by marshaling just the Data subset
	dataBytes, err := json.Marshal(payload.Data)
	if err == nil && len(dataBytes) > MaxInlineDataBytes {
		log.Printf("[Webhook] Payload %s data size (%d bytes) exceeds 5MB. Falling back to S3.", payload.JobID, len(dataBytes))

		if wd.s3Client != nil && wd.bucketName != "" {
			// S3 AWS Upload
			key := fmt.Sprintf("webhooks/%s/result.json", payload.JobID)
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()

			_, err := wd.s3Client.PutObject(ctx, &s3.PutObjectInput{
				Bucket:      aws.String(wd.bucketName),
				Key:         aws.String(key),
				Body:        bytes.NewReader(dataBytes),
				ContentType: aws.String("application/json"),
			})

			if err != nil {
				log.Printf("[Webhook] S3 Upload failed for %s: %v", payload.JobID, err)
			} else {
				// Generate 7-day presigned URL
				presignReq, err := wd.presignSvc.PresignGetObject(context.Background(), &s3.GetObjectInput{
					Bucket: aws.String(wd.bucketName),
					Key:    aws.String(key),
				}, s3.WithPresignExpires(7*24*time.Hour))

				if err == nil {
					payload.DownloadURL = presignReq.URL
					payload.Data = nil // Omit inline data!
					log.Printf("[Webhook] Generated presigned URL for %s", payload.JobID)
				}
			}
		} else {
			log.Printf("[Webhook] S3 Client not configured! Stripping data from response to prevent memory crashes.")
			payload.Data = nil
		}
	}

	// Sign the Final Optimized Payload
	finalJsonBytes, err := json.Marshal(payload)
	if err != nil {
		log.Printf("[Webhook] Invalid payload serialization: %v", err)
		return
	}

	signature := generateHMAC(finalJsonBytes, secret)

	// Dispatch Async
	go wd.executeDelivery(url, finalJsonBytes, signature)
}

func (wd *WebhookDispatcher) executeDelivery(url string, jsonBytes []byte, signature string) {
	const maxRetries = 3
	retryDelays := []time.Duration{0, 1 * time.Second, 3 * time.Second, 10 * time.Second}

	for attempt := 0; attempt <= maxRetries; attempt++ {
		if attempt > 0 {
			time.Sleep(retryDelays[attempt])
		}

		req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonBytes))
		if err != nil {
			log.Printf("[Webhook] Invalid URL: %v", err)
			return
		}

		req.Header.Set("Content-Type", "application/json")
		// The payload HMAC explicitly signs the EXACT JSON bytes mapped here
		req.Header.Set("X-E2E-Signature", signature)
		req.Header.Set("X-E2E-Event", "job.completed")
		req.Header.Set("User-Agent", "Quanta-Industrial-Webhook/2.0")

		client := &http.Client{Timeout: 10 * time.Second}
		resp, err := client.Do(req)

		if err != nil {
			log.Printf("[Webhook] Attempt #%d failed: %v", attempt+1, err)
			continue
		}

		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()

		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			log.Printf("[Webhook] Delivery successful: %s (status: %d)", url, resp.StatusCode)
			return
		}

		if resp.StatusCode >= 500 {
			log.Printf("[Webhook] Server error: %d - %s", resp.StatusCode, string(body))
			continue
		}

		log.Printf("[Webhook] Rejected: %s (status: %d)", url, resp.StatusCode)
		return
	}
}

// generateHMAC creates an HMAC-SHA256 signature for the payload
func generateHMAC(payload []byte, secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(payload)
	return hex.EncodeToString(mac.Sum(nil))
}

// VerifyHMAC verifies an HMAC signature
func VerifyHMAC(payload []byte, signature string, secret string) bool {
	expectedSignature := generateHMAC(payload, secret)
	return hmac.Equal([]byte(signature), []byte(expectedSignature))
}
