# Quanta Stack Matrix

## Core Technologies

### Go (Golang)
- **Role:** Control Plane & API Gateway
- **Why:** Delivers unparalleled concurrency, low-latency API handling, and type-safe backend orchestration.

### Python 3
- **Role:** Execution Plane & Agentic Engine
- **Why:** The industry standard for AI integration, DOM parsing, and robust data science libraries necessary for heuristic calculations.

### Temporal
- **Role:** Stateful Workflow Orchestration
- **Why:** Provides out-of-the-box durability, automatic retries, and distributed state management, abstracting distributed systems complexities.

### NATS JetStream
- **Role:** Nervous System / Event Bus
- **Why:** High-performance, low-latency publish-subscribe messaging that decoupled the control plane from the execution workers.

### Playwright
- **Role:** Browser Automation
- **Why:** Modern, fast, and capable of deep CDP (Chrome DevTools Protocol) manipulation required for our WAF evasion and DOM harvesting.

### Llama-3.3-70B
- **Role:** LLM Inference Engine
- **Why:** Provides the cognitive reasoning required for Semantic Late-Binding without the latency/cost profile of closed-source models.

### PostgreSQL
- **Role:** Persistent Storage
- **Why:** ACID-compliant, reliable relational data store for user profiles, job logs, and historical execution records.
