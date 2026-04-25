# Quanta Backend — System Introduction

> **Audience:** New cofounder onboarding.
> **Goal:** Understand the entire backend in one read.

---

## 1. System Overview

Quanta is an **autonomous browser automation engine**. A user gives it a natural-language instruction (e.g. _"Go to Airbnb, search for apartments in Tokyo under $150/night, and extract the top 5 listings"_) and the system:

1. **Plans** — An LLM converts the instruction into a structured execution plan (a DAG of steps).
2. **Validates** — The plan is checked for logical errors and verified against the real website.
3. **Executes** — A headless Chromium browser carries out every step, finding elements intelligently.
4. **Reports** — Results are streamed to the frontend in real-time and exported via email/webhook/CSV.

**The problem it solves:** Replacing brittle, hand-coded Selenium/Playwright scripts with an AI-driven system that can adapt to UI changes, heal broken selectors, and recover from crashes.

---

## 2. High-Level Architecture

The backend is split into two independent services that communicate through **Temporal** (workflow orchestrator) and **NATS** (event bus).

```
┌──────────────────────────────────────────────────────────┐
│                     FRONTEND (Next.js)                   │
│            WebSocket + SSE for real-time updates         │
└────────────┬────────────────────────────────┬────────────┘
             │ HTTP REST API                  │ WebSocket
             ▼                                ▼
┌─────────────────────────────────┐  ┌──────────────────────┐
│       CONTROL PLANE (Go)        │  │   WebSocket Manager  │
│  • Gin HTTP Router              │  │   (ws/manager.go)    │
│  • Auth (Clerk JWT)             │  └──────────────────────┘
│  • Rate Limiting (Redis)        │
│  • Billing (Polar webhooks)     │
│  • URL + Logic Validation       │
│  • Job CRUD (PostgreSQL/GORM)   │
│  • Prometheus Metrics           │
└────────────┬────────────────────┘
             │ Temporal StartWorkflow
             ▼
┌─────────────────────────────────────────────────────────┐
│               TEMPORAL CLUSTER                          │
│   Durable workflow orchestration with retry/timeout     │
└────────────┬────────────────────────────────────────────┘
             │ Task Queue: "e2e-browser-tasks"
             ▼
┌─────────────────────────────────────────────────────────┐
│           EXECUTION PLANE (Python)                      │
│  • Temporal Worker (activities + workflows)              │
│  • Preflight Pipeline (plan generation)                  │
│  • RecipeEngine (DAG executor)                           │
│  • SmartFinder (element discovery, 4-layer fallback)     │
│  • Playwright (headless Chromium)                         │
│  • RAG Memory (pgvector + OpenAI embeddings)             │
└────────────┬────────────────────────────────────────────┘
             │ NATS publish (events)
             ▼
┌─────────────────────────────────────────────────────────┐
│               NATS (Event Bus)                          │
│  subject: job.update.{job_id}  (Protobuf)               │
│  subject: quanta.telemetry.{job_id}  (JSON)              │
└────────────┬────────────────────────────────────────────┘
             │ Consumer in Control Plane
             ▼
┌─────────────────────────────────┐
│  • Update job status in DB      │
│  • Broadcast to WebSocket       │
│  • Trigger webhooks             │
│  • Send email reports           │
│  • Persist logs to DB           │
└─────────────────────────────────┘
```

---

## 3. End-to-End Workflow

Here is exactly what happens when a user clicks "Run" in the UI:

### Step 1 — API Request (Control Plane)

The frontend sends `POST /v1/execute` with `{ target_url, objective }`.

**What happens in Go:**

- `execute_controller.go` → Authenticates user (Clerk JWT middleware).
- `url_validator.go` → HTTP probe + SSRF check on the target URL.
- `logic_validator.go` → LLM-powered feasibility check ("Is this task even possible?").
- Creates a `Job` row in PostgreSQL (status: `QUEUED`).
- `temporal/manager.go` → Calls `ExecuteWorkflow("BrowserWorkflow", input)` on Temporal.

### Step 2 — Temporal Dispatches to Python Worker

Temporal places the job on the `e2e-browser-tasks` queue. The Python worker picks it up.

**What happens in Python:**

- `worker.py` → The Temporal worker is always running, polling the queue.
- `browserWorkflow.py` → The `BrowserWorkflow` workflow starts.
- It calls `execute_recipe_activity` (or `browser_automation_activity`).

### Step 3 — Preflight Pipeline (Recipe Generation)

If no pre-built recipe exists, the system generates one on the fly.

`recipeActivity.py` → `PreflightPipeline` in `preflight.py`:

| Layer   | Name                  | What It Does                                                                       | Speed  |
| ------- | --------------------- | ---------------------------------------------------------------------------------- | ------ |
| Phase 1 | HTTP Verification     | Checks if the URL is reachable, detects WAFs                                       | ~200ms |
| Phase 1 | Preflight Oracle      | LLM feasibility + auth + site classification (3-in-1)                              | ~1s    |
| Layer 1 | RAG Memory Check      | Searches pgvector for a previously-successful recipe (>92% match → instant return) | ~100ms |
| Layer 2 | LLM Planner           | Converts natural-language prompt → structured `QuantaPlan` JSON (list of intents)  | ~2s    |
| Layer 3 | Static Validation     | Checks DAG topology: no orphan nodes, no infinite loops, all variables defined     | <50ms  |
| Layer 4 | Dynamic Justification | Opens a headless browser, uses SmartFinder to verify each planned element exists   | ~10s   |

**Output:** A "hardened recipe" — a validated Recipe Schema v2.0 JSON (a DAG of nodes, edges, actions, conditions).

### Step 4 — Recipe Execution (DAG Traversal)

`recipeEngine.py` takes the hardened recipe and executes it:

1. Launches a headless Chromium browser via Playwright.
2. Traverses the DAG starting from `entry_point`.
3. For each **Action Node**, it uses the `OperatorRealizer` + `SmartFinder` to find elements and perform actions (click, type, extract, navigate, etc.).
4. For each **Decision Node**, it evaluates conditions and branches.
5. For each **Loop Node**, it iterates over data collections.
6. For each **Checkpoint Node**, it saves browser state (cookies, localStorage) for crash recovery.
7. For each **Human Gate Node**, it hibernates the workflow and waits for a user signal (CAPTCHA, 2FA).

### Step 5 — SmartFinder (Element Discovery)

SmartFinder (`smartFinder.py`, 2443 lines) is the core element-finding engine:

| Layer | Name       | Technique                                                  | Speed  | Threshold       |
| ----- | ---------- | ---------------------------------------------------------- | ------ | --------------- |
| 0     | STRUCTURAL | Keyword → CSS selector lookup (deterministic)              | <5ms   | Exact match     |
| 1     | REFLEX     | SimHash fingerprint comparison                             | <10ms  | 0.85 similarity |
| 2     | HEURISTIC  | Levenshtein fuzzy text matching on visible elements        | ~50ms  | 0.55 similarity |
| 3     | SEMANTIC   | Qdrant vector DB search (sentence-transformers embeddings) | ~200ms | 0.70 similarity |
| 4     | COGNITIVE  | LLM analyzes pruned Accessibility Tree, returns Node ID    | ~2s    | AI judgment     |

**Self-Healing:** If Layer 0/1 fails but a deeper layer succeeds, the system updates the element's fingerprint so it hits the fast path next time.

### Step 6 — Real-Time Reporting

During execution, the `NervousSystem` (NATS client) publishes events:

- **Protobuf** on `job.update.{job_id}` → Internal status tracking.
- **JSON** on `quanta.telemetry.{job_id}` → SSE stream for frontend.

The Go control plane's NATS consumer:

- Updates the job status in PostgreSQL.
- Broadcasts to WebSocket connections.
- On completion: exports CSV, sends email, triggers webhooks.

### Step 7 — Learning

On successful completion, `recipeActivity.py` calls `ragService.save_template()` to save the recipe into pgvector memory. Next time someone requests a similar task on the same domain, the RAG layer returns it instantly (Layer 1 hit).

---

## 4. Core Components

### 4.1 Control Plane (Go)

| Component            | File                                  | Purpose                                                         |
| -------------------- | ------------------------------------- | --------------------------------------------------------------- |
| HTTP Server          | `cmd/server/main.go`                  | Gin router, initializes all services, wires routes              |
| Execute Controller   | `controllers/execute_controller.go`   | Handles `POST /v1/execute`, preflight checks, Temporal dispatch |
| Workflow Controller  | `controllers/workflow_controller.go`  | Job CRUD, cancel, resume, logs                                  |
| Generator Controller | `controllers/generator_controller.go` | Recipe generation via Temporal                                  |
| Temporal Manager     | `temporal/manager.go`                 | Wraps Temporal SDK, starts/describes workflows                  |
| WebSocket Manager    | `ws/manager.go`                       | Real-time job updates to frontend                               |
| Auth Middleware      | `middleware/auth.go`                  | Clerk JWT verification                                          |
| Billing Middleware   | `middleware/billing.go`               | Credit enforcement via Polar                                    |
| Rate Limiter         | `middleware/rate_limit.go`            | Redis-based API rate limiting                                   |
| NATS Consumer        | `cmd/server/main.go` (Consumer)       | Subscribes to `job.update.*`, updates DB, broadcasts WS         |
| Exporter             | `services/exporter.go`                | CSV data export from job logs                                   |
| Email Service        | `services/email_service.go`           | Sends completion emails with attachments                        |
| Webhook Dispatcher   | `webhook/dispatcher.go`               | HMAC-signed webhook delivery                                    |
| Streaming (SSE)      | `streaming/jetstream.go`              | JetStream-based SSE for `/v1/execute/:job_id/stream`            |

### 4.2 Execution Plane (Python)

| Component          | File                               | Purpose                                                                 |
| ------------------ | ---------------------------------- | ----------------------------------------------------------------------- |
| Worker             | `src/worker.py`                    | Temporal worker, registers all workflows + activities                   |
| Browser Workflow   | `src/workflows/browserWorkflow.py` | Temporal workflow with human-in-the-loop hibernation                    |
| Recipe Activity    | `src/activities/recipeActivity.py` | Bridge: Preflight → RecipeEngine → Execution                            |
| Preflight Pipeline | `src/core/rag/preflight.py`        | 4-layer quality control orchestrator                                    |
| LLM Planner        | `src/core/rag/planner.py`          | NVIDIA NIM (Llama-3.1-8B) → `QuantaPlan` → DAG compiler                 |
| RAG Service        | `src/core/rag/ragService.py`       | pgvector memory: embed, search, save templates                          |
| Static Validator   | `src/core/rag/staticValidator.py`  | DAG topology: cycles, orphans, reachability, variables                  |
| Justifier Engine   | `src/core/rag/justifier.py`        | Browser verification of recipe steps via SmartFinder                    |
| URL Classifier     | `src/core/rag/classifier.py`       | Categorizes target sites (ecommerce, social, etc.)                      |
| Recipe Engine      | `src/core/recipe/recipeEngine.py`  | DAG executor: Action, Decision, Loop, Checkpoint, HumanGate, Parallel   |
| Recipe Schema      | `src/core/recipe/recipeSchema.py`  | Pydantic v2 models for Recipe Schema v2.0                               |
| SmartFinder        | `src/core/selector/smartFinder.py` | 4-layer element finder with self-healing                                |
| GlassBox Engine    | `src/core/GlassBox.py`             | Deterministic math: raycast click-check, SVG hashing, human-like typing |
| Nervous System     | `src/core/NervousSystem.py`        | NATS client: Protobuf + JSON event publishing                           |
| Prompts            | `src/core/rag/prompts.py`          | All LLM system prompts (Oracle, Planner, SmartFinder, Classifier)       |
| Config             | `src/config.py`                    | Feature flags, timeout configuration, environment parsing               |

---

## 5. Data Flow

```
User Prompt ("Search Airbnb for Tokyo apartments")
    │
    ▼
┌─────────────────────────────────────────────────┐
│ Control Plane (Go)                              │
│  Input:  { target_url, objective }              │
│  Output: Job record in DB + Temporal workflow   │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│ Preflight Pipeline (Python)                     │
│  Input:  url + prompt                           │
│  Output: Hardened Recipe (JSON DAG)             │
│                                                 │
│  ┌──────────────┐    ┌────────────────┐         │
│  │ RAG Memory   │───▶│ If >92% match: │         │
│  │ (pgvector)   │    │ Return cached  │         │
│  └──────────────┘    └────────────────┘         │
│         │ Miss                                  │
│         ▼                                       │
│  ┌──────────────┐    ┌────────────────┐         │
│  │ LLM Planner  │───▶│ QuantaPlan     │         │
│  │ (NIM/Llama)  │    │ (JSON intents) │         │
│  └──────────────┘    └────────────────┘         │
│         │                                       │
│         ▼                                       │
│  ┌──────────────┐    ┌────────────────┐         │
│  │ DAG Compiler │───▶│ Recipe v2.0    │         │
│  │              │    │ (nodes+edges)  │         │
│  └──────────────┘    └────────────────┘         │
│         │                                       │
│         ▼                                       │
│  ┌──────────────┐    ┌──────────────┐           │
│  │ Static Valid. │───▶│ Justifier    │           │
│  │ (DAG checks) │    │ (browser test)│           │
│  └──────────────┘    └──────────────┘           │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│ Recipe Engine (Python)                          │
│  Input:  Hardened Recipe + Playwright browser   │
│  Output: Extracted data + execution result      │
│                                                 │
│  For each node in DAG:                          │
│    1. SmartFinder.find(intent) → DOM element    │
│    2. Execute action (click/type/extract)       │
│    3. Check post-conditions                     │
│    4. Advance to next node via edges            │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│ Output                                          │
│  • Real-time WebSocket events to frontend       │
│  • Job status in PostgreSQL                     │
│  • CSV export via email                         │
│  • Webhook delivery (HMAC-signed)               │
│  • Recipe saved to RAG memory (learning)        │
└─────────────────────────────────────────────────┘
```

---

## 6. Key Functions Explained

### `PreflightPipeline.run(url, prompt)` — `preflight.py`

- **Purpose:** Converts a raw user prompt into a validated, browser-tested recipe.
- **Input:** Target URL (`str`), user prompt (`str`).
- **Output:** `PreflightResult` containing the hardened recipe, source info, timing, and warnings.

### `RecipePlanner.generate(prompt, url)` — `planner.py`

- **Purpose:** Calls NVIDIA NIM (Llama-3.1-8B) to convert a natural-language prompt into a `QuantaPlan` (flat list of intents), then compiles it into a Recipe v2.0 DAG.
- **Input:** Prompt (`str`), URL (`str`).
- **Output:** `PlannerResult` with the compiled recipe JSON.

### `build_dag_from_directions(plan, context)` — `planner.py`

- **Purpose:** Deterministic compiler. Takes the flat `QuantaPlan` from the LLM and constructs the full DAG (nodes, edges, exit points) using a factory pattern.
- **Input:** `QuantaPlan` object.
- **Output:** `Recipe` Pydantic model.

### `SmartFinder.find(intent, metadata)` — `smartFinder.py`

- **Purpose:** Finds a DOM element by natural-language intent using a 4-layer fallback.
- **Input:** Intent string (e.g. `"Login Button"`), optional metadata with SimHash fingerprint.
- **Output:** `FindResult` with the element handle, confidence score, layer used, and optional self-healing signature.

### `RecipeEngine.run(browser, inputs, secrets)` — `recipeEngine.py`

- **Purpose:** Executes a Recipe v2.0 DAG. Traverses nodes, dispatches to processors (Action, Decision, Loop, etc.), manages checkpoints for crash recovery.
- **Input:** Playwright browser, user inputs, secrets.
- **Output:** Execution result with status, extracted data, and checkpoint ID.

### `JustifierEngine.justify_recipe(recipe, url)` — `justifier.py`

- **Purpose:** Opens a headless browser, navigates to the URL, and verifies each recipe step's target element exists using SmartFinder. Patches the recipe with verified selectors.
- **Input:** Soft recipe (`Dict`), target URL (`str`).
- **Output:** `JustificationResult` with patched recipe and per-element verification status.

### `RAGService.find_template(prompt, url)` — `ragService.py`

- **Purpose:** Searches pgvector for a previously-successful recipe matching this prompt and domain.
- **Input:** Prompt (`str`), URL (`str`).
- **Output:** `TemplateMatch` if similarity >92%, else `None`.

### `NervousSystem.publish_update(job_id, status, message)` — `NervousSystem.py`

- **Purpose:** Dual-broadcasts status events via NATS (Protobuf for internal + JSON for SSE).
- **Input:** Job ID, status string, message.
- **Output:** None (fire-and-forget publish).

### `StaticValidator.validate(recipe)` — `staticValidator.py`

- **Purpose:** Validates recipe structure without a browser: schema compliance, variable integrity, graph topology, loop safety, reachability, timeout coverage.
- **Input:** Recipe JSON (`Dict`).
- **Output:** `ValidationResult` with errors and warnings. Raises `RecipeValidationError` on blocking issues.

---

## 7. Current System Limitation (Critical)

### The Core Problem: Blind Planning

The most significant architectural weakness is in the **Planner** (`planner.py`).

**What happens:**

1. The user provides a prompt like _"Go to Airbnb, set location to Tokyo, filter under $150, open the 3rd listing."_
2. The LLM Planner converts this into a flat list of intents (`set_location`, `set_dates`, `apply_filters`, etc.).
3. These intents are compiled into a full execution DAG by `build_dag_from_directions()`.

**The problem:**
The LLM has **zero awareness of the actual DOM**. It generates steps based purely on its training data and general knowledge of how websites work. It has never seen the actual page.

This means:

- It might generate an intent like `set_max_price` when Airbnb's filter is actually called "Price range" with a slider, not an input field.
- It might assume a "Search" button exists when the site auto-submits on input change.
- It might order steps incorrectly (e.g., setting filters before the search results page loads).
- It might reference UI elements that don't exist on the current version of the site.

### Why This Breaks Everything Downstream

The pipeline is designed as a **waterfall**:

```
LLM Planner (blind) → DAG Compiler → Static Validator → Justifier → RecipeEngine
```

Every downstream layer **trusts the Planner's output**:

- The **DAG Compiler** mechanically converts intents to nodes — it doesn't question the intent names.
- The **Static Validator** checks graph structure (orphan nodes, cycles) — it cannot validate whether `set_max_price` is a real UI action.
- The **Justifier** tries to find elements via SmartFinder — but if the intent description is wrong (e.g., `"Max Price Input"` when the element is a slider labeled `"Price range"`), SmartFinder may fail or match the wrong element.
- The **RecipeEngine** executes whatever it receives — if the recipe says "click a button that doesn't exist," it fails.

### The Cascade Effect

Because the later layers are not intelligent enough to compensate:

1. **Wrong intent** → Justifier can't find the element → marks it `CALIBRATION_NEEDED` → recipe proceeds with unverified steps.
2. **Wrong ordering** → RecipeEngine clicks a filter before the results page loads → element not found → step fails.
3. **Missing steps** → The LLM forgets a critical step (e.g., dismissing a cookie banner) → next step clicks the wrong thing because the banner is covering the target element.

The system's reliability is fundamentally capped by the quality of the blind LLM's plan. No amount of downstream SmartFinder intelligence can compensate for a fundamentally incorrect plan.

### Why This Makes the System Unreliable

- **Success rate varies wildly** depending on the target site. Well-known sites (Airbnb, Amazon) work better because the LLM has more training data. Niche sites fail frequently.
- **No feedback loop during planning.** The LLM generates the plan in one shot. It never sees the page, never gets error feedback, never adjusts.
- **The Justifier is a patch, not a fix.** It can verify elements exist, but it can't restructure a logically wrong plan.
- **Cascading failures are silent.** A slightly wrong initial step poisons every subsequent step, but the error only surfaces 5-10 steps later as "Element not found."

---

## 8. Summary

Quanta is a two-plane system:

- **Control Plane (Go):** API gateway, authentication, billing, job management, real-time event routing.
- **Execution Plane (Python):** AI-powered planning, browser automation, self-healing element discovery, crash recovery.

The flow is: **User prompt → Preflight (plan + validate + verify) → RecipeEngine (execute DAG) → Results (WebSocket + email + webhook)**.

The system's strongest feature is the **SmartFinder** — a 4-layer element discovery engine that makes automation resilient to UI changes through SimHash fingerprinting, fuzzy matching, semantic search, and AI recovery.

The system's biggest weakness is the **blind Planner** — the LLM generates execution plans without ever seeing the actual page. Every downstream component depends on this plan being correct, but the LLM is guessing based on training data alone. This is the single largest source of failures in production.
