# Quanta Execution Pipeline Data Flow (BYOS)

This document maps the exact end-to-end execution lifecycle of the Bring Your Own Session (BYOS) engine, from the initial API request to the final extraction output.

## 1. Client Request Ingestion
- **Action:** A user or system submits a `POST /v1/execute` request containing a `target_url`, an `objective`, and BYOS context (either a `sessionState` dictionary or a `credential_id`).
- **Component:** Go Control Plane
- **Code Mapping:** `apps/control-plane/internal/controllers/execute_controller.go` -> `HandleExecuteAsync()`

## 2. Preflight Validation & Bypass
- **Action:** The Go API validates the UUID idempotency key and runs the URL through SSRF & WAF validation. If BYOS is provided (`req.SessionState != nil` or `req.CredentialID != ""`), WAF blocks are explicitly ignored because the injected session circumvents edge protections.
- **Component:** Go Control Plane
- **Code Mapping:** `apps/control-plane/internal/controllers/execute_controller.go` calls `services.ValidateURL()`.

## 3. Temporal Workflow Dispatch
- **Action:** The validated job payload, including the raw `SessionState` JSON, is asynchronously queued into the NATS/Temporal event cluster. The Go API responds to the client with an HTTP 202 and the `job_id`.
- **Component:** Go Control Plane
- **Code Mapping:** `apps/control-plane/internal/temporal/temporal_manager.go` -> `ExecuteJob()`

## 4. Worker Pickup & Graph Conversion
- **Action:** The Python worker pulls the job from the Temporal queue. If the request contains a frontend-generated workflow graph (`nodes[]` and `edges[]`), the converter builds an adjacency list, performs a Breadth-First topological sort starting from the trigger node, and maps React Flow structural types to executable `action` dicts.
- **Component:** Python Execution Plane
- **Code Mapping:** `apps/execution-plane/src/core/recipe/recipe_converter.py` -> `convert_graph_to_steps()`

## 5. Preflight Oracle Bypass
- **Action:** Before executing the steps, the worker runs the Semantic Preflight pipeline. Because `is_byos_session` evaluates to True, the pipeline immediately short-circuits and approves execution, preventing the LLM feasibility safety checks from rejecting the prompt.
- **Component:** Python Execution Plane
- **Code Mapping:** `apps/execution-plane/src/core/rag/preflight.py` -> `PreflightPipeline.run()`

## 6. Browser Hydration & Initial State Navigation
- **Action:** A Playwright browser context is instantiated within a fail-safe `async with safe_browser_context(...)` block. The `sessionState` cookies and localStorage are directly injected. Before the execution loop begins, the worker explicitly executes a `page.goto(target_url)` and awaits `networkidle` to guarantee 302/303 HTTP authentication redirects fully resolve.
- **Component:** Python Execution Plane
- **Code Mapping:** `apps/execution-plane/src/activities/activities.py` -> `browser_automation_activity()`

## 7. Execution Loop & Smart Element Discovery
- **Action:** The engine iterates sequentially over the `steps` array. For interaction steps (`CLICK`, `INPUT`, `EXTRACT`), the system delegates element discovery to `SmartFinder`, applying built-in resiliency (`resilient_page_operation`) to automatically retry and re-hydrate standard Playwright actions if the DOM context crashes mid-execution due to SPA unmounts.
- **Component:** Python Execution Plane
- **Code Mapping:** `apps/execution-plane/src/activities/activities.py` (Execution Loop) -> `finder.find(intent)`

## 8. Extraction & Final Output
- **Action:** For `EXTRACT` nodes, the engine grabs the innerText of the targeted elements (or falls back to scanning the entire body). The raw extracted text is passed to an LLM (`SafeLLMClient`) with a strict schema to filter out noise. The cleaned data is appended to the job `params` and returned as the final output.
- **Component:** Python Execution Plane
- **Code Mapping:** `apps/execution-plane/src/activities/activities.py` -> `SafeLLMClient().call(sys_prompt, prompt)`

## 9. Telemetry & Webhook Dispatch
- **Action:** At the completion of every node (and the final workflow output), the Python worker posts the state updates and the final extracted `params` dictionary back to the Go Control Plane webhook API, updating the client's execution stream in real-time.
- **Component:** Python Execution Plane / Go Control Plane
- **Code Mapping:** `apps/execution-plane/src/activities/activities.py` uses `httpx.AsyncClient().post()` back to `/workflows/{workflow_id}/webhook`.
