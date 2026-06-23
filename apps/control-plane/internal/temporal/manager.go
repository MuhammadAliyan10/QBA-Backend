package temporal

import (
	"context"
	"fmt"
	"log"
	"time"

	"go.temporal.io/sdk/client"
)

// ─── CONFIG ──────────────────────────────────────────────────────────────────

const (
	// TaskQueue is the Temporal queue the Python execution workers poll.
	TaskQueue = "e2e-browser-tasks"

	// WorkflowExecutionTimeout caps dangling executions at 10 minutes.
	// Failed or stuck workflows are terminated after this window.
	WorkflowExecutionTimeout = 10 * time.Minute

	// DialTimeout is the hard cap for establishing the Temporal gRPC connection.
	DialTimeout = 10 * time.Second

	// StartTimeout is the per-call timeout for ExecuteWorkflow RPCs.
	StartTimeout = 5 * time.Second
)

// ─── WORKFLOW INPUT ──────────────────────────────────────────────────────────

// Attachment represents a Base64-encoded file uploaded from the CLI.
// The Python worker materializes it to disk before execution.
type Attachment struct {
	Filename string `json:"filename"`
	MimeType string `json:"mime_type"`
	Base64   string `json:"base64"`
}

// WorkflowInput is the payload sent to the Python Temporal worker.
// Must match the Python workflow's expected input schema exactly.
type WorkflowInput struct {
	JobID               string                 `json:"job_id"`
	WorkflowID          string                 `json:"workflow_id"`
	TargetURL           string                 `json:"target_url"`
	TargetUrls          []string               `json:"target_urls"`
	NavigationObjective string                 `json:"navigation_objective,omitempty"`
	ExtractionSchema    map[string]interface{} `json:"extraction_schema,omitempty"`
	// Objective is deprecated. Kept for backward compat with older Python workers.
	Objective      string                 `json:"objective,omitempty"`
	EngineSettings map[string]interface{} `json:"engine_settings,omitempty"`
	// SessionState carries a pre-authenticated Playwright storage_state dictionary.
	// Nil/omitted when no BYOS session is provided by the caller.
	SessionState map[string]interface{} `json:"sessionState,omitempty"`
	// Attachments carries Base64-encoded files from the CLI.
	// The Python worker decodes and writes them to a temp directory.
	Attachments []Attachment `json:"attachments,omitempty"`
}

// ─── TEMPORAL MANAGER ────────────────────────────────────────────────────────

// TemporalManager wraps the Temporal SDK client with strict timeout enforcement
// and a clean API for starting workflow executions.
type TemporalManager struct {
	client client.Client
}

// New dials the Temporal cluster and returns a ready-to-use manager.
// Fails fast (log.Fatalf) if the connection cannot be established — this is
// called at startup, not at request time.
func New(hostPort string) *TemporalManager {
	log.Printf("[TemporalManager] Connecting to Temporal at %s", hostPort)

	c, err := client.Dial(client.Options{
		HostPort: hostPort,
	})
	if err != nil {
		log.Fatalf("[TemporalManager] FATAL: cannot connect to Temporal: %v", err)
	}

	log.Println("[TemporalManager] Connected successfully")
	return &TemporalManager{client: c}
}

// Wrap creates a TemporalManager from an existing client.Client.
// Use this when main.go already has a connected client.
func Wrap(c client.Client) *TemporalManager {
	return &TemporalManager{client: c}
}

// StartExecution pushes a new automation job to the Python execution workers.
//
// The jobID is used as both the Temporal WorkflowID (ensuring idempotency)
// and the internal Quanta job identifier.
//
// Returns the Temporal RunID on success.
// If the workflow already exists (duplicate idempotency key), Temporal returns
// a WorkflowExecutionAlreadyStarted error — the caller handles this.
func (tm *TemporalManager) StartExecution(
	ctx context.Context,
	jobID string,
	workflowID string,
	targetUrls []string,
	navigationObjective string,
	extractionSchema map[string]interface{},
	engineSettings map[string]interface{},
	sessionState map[string]interface{},
	attachments []Attachment,
) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, StartTimeout)
	defer cancel()

	// Backward compat: set TargetURL to first element.
	primaryURL := ""
	if len(targetUrls) > 0 {
		primaryURL = targetUrls[0]
	}

	input := WorkflowInput{
		JobID:               jobID,
		WorkflowID:          workflowID,
		TargetURL:           primaryURL,
		TargetUrls:          targetUrls,
		NavigationObjective: navigationObjective,
		ExtractionSchema:    extractionSchema,
		Objective:           navigationObjective, // Backward compat for older workers
		EngineSettings:      engineSettings,
		SessionState:        sessionState,
		Attachments:         attachments,
	}

	opts := client.StartWorkflowOptions{
		ID:                       jobID,
		TaskQueue:                TaskQueue,
		WorkflowExecutionTimeout: WorkflowExecutionTimeout,
	}

	run, err := tm.client.ExecuteWorkflow(ctx, opts, "BrowserWorkflow", input)
	if err != nil {
		return "", fmt.Errorf("failed to start workflow: %w", err)
	}

	log.Printf("[TemporalManager] Started workflow | ID=%s | RunID=%s | URLs=%d", jobID, run.GetRunID(), len(targetUrls))
	return run.GetRunID(), nil
}

// GetExistingRunID retrieves the RunID of an already-running workflow by its ID.
// Used when a duplicate idempotency key is detected.
func (tm *TemporalManager) GetExistingRunID(ctx context.Context, workflowID string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, StartTimeout)
	defer cancel()

	desc, err := tm.client.DescribeWorkflowExecution(ctx, workflowID, "")
	if err != nil {
		return "", fmt.Errorf("failed to describe workflow %s: %w", workflowID, err)
	}

	return desc.WorkflowExecutionInfo.Execution.RunId, nil
}

// Client exposes the raw Temporal client for advanced use cases.
func (tm *TemporalManager) Client() client.Client {
	return tm.client
}

// Close gracefully shuts down the Temporal connection.
func (tm *TemporalManager) Close() {
	tm.client.Close()
	log.Println("[TemporalManager] Connection closed")
}
