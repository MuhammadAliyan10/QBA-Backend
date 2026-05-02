# Quanta Backend System Requirements & Tools

The Quanta execution engine is built on a polyglot architecture, combining the high-throughput concurrency of a Go Control Plane with the AI/ML ecosystem of a Python Execution Plane.

## 1. Go Control Plane (API & Orchestration)
Located in `apps/control-plane/go.mod`.

| Library / Tool | Purpose | What We Do With It |
| :--- | :--- | :--- |
| **Gin (`gin-gonic/gin`)** | REST Framework | Handles all incoming HTTP traffic (`/v1/execute`), middleware authentication, and WAF evasion routing. |
| **NATS (`nats-io/nats.go`)** | Event Bus | Acts as the primary pub/sub messaging backbone for decoupled, high-throughput asynchronous communication. |
| **Temporal (`temporal.io/sdk`)** | Workflow Orchestration | Manages durable execution state, handles automatic retries, and distributes jobs to Python workers without losing data on failure. |
| **GORM (`gorm.io/gorm`)** | PostgreSQL ORM | Handles database schema migrations and queries for Jobs, Accounts, and encrypted Credentials. |
| **Redis (`redis/go-redis`)** | Caching & Locking | Provides distributed mutual exclusion (Mutex) locks to prevent thundering herds during headless login authentication. |
| **Melody (`olahol/melody`)** | WebSockets | Streams real-time execution telemetry and JIT debugging logs back to the frontend UI. |
| **AWS SDK (`aws-sdk-go-v2`)** | Blob Storage | Interacts with Cloudflare R2 / S3 to store and retrieve large encrypted session states and payload assets. |
| **OpenTelemetry (`otel`)** | Observability | Exports Prometheus metrics and distributed traces to monitor API health and latency bottlenecks. |

## 2. Python Execution Plane (AI & Automation)
Located in `apps/execution-plane/requirements.txt`.

| Library / Tool | Purpose | What We Do With It |
| :--- | :--- | :--- |
| **Playwright (`playwright`)** | Browser Engine | Drives the headless Chromium browser, managing multi-tab contexts, DOM interactions, and network interception. |
| **Playwright Stealth** | WAF Evasion | Masks headless browser fingerprints to bypass Cloudflare and DataDome challenges during execution. |
| **Temporal (`temporalio`)** | Worker SDK | Listens to the Temporal task queue to pick up and execute the `BrowserWorkflow` tasks assigned by the Go Control Plane. |
| **OpenAI / LangChain** | LLM Abstraction | Powers the Cognitive layer of `SmartFinder` for element recovery, extracts unstructured text, and runs the Semantic Preflight heuristics. |
| **Qdrant (`qdrant-client`)** | Vector Database | Stores DOM element signatures. Powers the Semantic layer to locate UI elements even when CSS class names dynamically change. |
| **Sentence Transformers** | ML Embeddings | Runs locally to convert HTML DOM chunks into vector embeddings for the `SmartFinder` Qdrant search. |
| **Pydantic** | Schema Validation | Enforces strict type checking for AI outputs, pipeline configurations, and internal parameter passing. |
| **HTTPX / aiohttp** | Async Networking | Powers the global `NetworkSniffer`, preflight URL verification, and webhook dispatches back to the Go Control Plane. |
| **LXML & Levenshtein** | Fast DOM Parsing | Parses raw HTML trees and calculates string distances for lightning-fast heuristic element discovery (the Reflex layer). |
| **Boto3** | Blob Storage | Uploads extracted files (PDFs, CSVs) and debug screenshots from the execution worker directly to S3/R2. |
| **NATS (`nats-py`)** | Python Event Bus | Powers the `NervousSystem` module, publishing granular `RUNNING` status events and logs directly to the NATS cluster. |
| **Cryptography** | Security | Encrypts and decrypts sensitive BYOS cookies and storage states injected into the `safe_browser_context`. |
