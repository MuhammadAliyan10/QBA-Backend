# Quanta System Flow

## The Execution Lifecycle

The Quanta architecture is deeply decoupled, relying on an event-driven Go Control Plane orchestrating a Python/Playwright Execution Plane.

### 1. Client Request Ingestion
- **Action:** A client POST request containing the workflow intent hits the Go API router.
- **Component:** `apps/control-plane/cmd/server/main.go` and the respective controllers in `apps/control-plane/internal/controllers/`.

### 2. Event Distribution
- **Action:** The request is published as a job to the NATS JetStream event bus.
- **Component:** `apps/control-plane/internal/streaming/jetstream.go`.

### 3. Workflow Orchestration
- **Action:** Temporal picks up the event and orchestrates the distributed workflow, handling state, retries, and timeouts.
- **Component:** `apps/control-plane/internal/temporal/manager.go`.

### 4. Agentic Execution
- **Action:** The Python worker claims the Temporal task. It leverages Playwright with advanced WAF evasion techniques to execute the intent against the target application.
- **Component:** `apps/execution-plane/src/worker.py` and execution modules in `apps/execution-plane/src/activities/`.

### 5. Completion & Webhook Dispatch
- **Action:** Upon completion, the result is published back to NATS, where a webhook dispatcher broadcasts the finalized data payload back to the client.
- **Component:** Event consumers in `apps/control-plane/internal/events/consumer.go` and the future webhook dispatcher logic.
