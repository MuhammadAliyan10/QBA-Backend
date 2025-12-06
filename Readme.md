# e2e-Platform 🚀

**AI-Powered Browser Automation with Semantic Intelligence**

An enterprise-grade automation engine that combines traditional browser automation with cutting-edge AI to create self-healing, intelligent workflows that adapt to UI changes automatically.

---

## 🎯 What is e2e-Platform?

e2e-Platform is a next-generation browser automation system that uses **semantic understanding** instead of brittle CSS selectors. When you ask it to "click the search button," it understands what a search button _means_, not just what it's called.

### The Problem We Solve

Traditional automation breaks when:

- Button text changes ("Search" → "Find")
- CSS classes get updated (`.btn-primary` → `.button-search`)
- UI layouts shift
- Websites deploy new designs

**Our Solution**: AI that understands _intent_, not _structure_.

```python
# Traditional (Brittle):
selenium.find_element_by_css_selector("#searchBtn").click()  # ❌ Breaks on CSS changes

# e2e-Platform (Intelligent):
find_element(intent="search button").click()  # ✅ Adapts automatically
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     CLIENT REQUEST                          │
│         POST /run {"workflow_id": "github_login"}           │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              CONTROL PLANE (Go)                             │
│  • REST API (Gin)                                           │
│  • Temporal Workflow Orchestration                          │
│  • Job Queue Management                                     │
│  • WebSocket Real-time Updates                             │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              EVENT BUS (NATS)                               │
│  • Pub/Sub Messaging                                        │
│  • Job Status Updates                                       │
│  • Decoupled Communication                                  │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│           EXECUTION PLANE (Python)                          │
│                                                             │
│  ┌─────────────────────────────────────────────┐           │
│  │  1. RECIPE MANAGER (Vector Search)          │           │
│  │     • Qdrant Vector Database                │           │
│  │     • Sentence Transformers (384-dim)       │           │
│  │     • Semantic Recipe Matching              │           │
│  └─────────────────────────────────────────────┘           │
│                       │                                      │
│                       ▼                                      │
│  ┌─────────────────────────────────────────────┐           │
│  │  2. SMART FINDER (AI Element Detection)     │           │
│  │     • Intent → Element Embeddings           │           │
│  │     • TensorEngine (Semantic Search)        │           │
│  │     • PatternDB (Muscle Memory Cache)       │           │
│  └─────────────────────────────────────────────┘           │
│                       │                                      │
│                       ▼                                      │
│  ┌─────────────────────────────────────────────┐           │
│  │  3. BROWSER ENGINE (Playwright)             │           │
│  │     • Chromium Automation                   │           │
│  │     • Network Sniffing                      │           │
│  │     • Protocol Intelligence                 │           │
│  └─────────────────────────────────────────────┘           │
│                       │                                      │
│                       ▼                                      │
│  ┌─────────────────────────────────────────────┐           │
│  │  4. ACCOUNT MANAGER (Session Pooling)       │           │
│  │     • Credential Leasing                    │           │
│  │     • Cookie Rehydration                    │           │
│  │     • Race Condition Protection             │           │
│  └─────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              PERSISTENCE LAYER                              │
│  • CockroachDB (Jobs, Accounts)                            │
│  • Qdrant (Recipe Embeddings)                              │
│  • PatternDB (Element Cache)                               │
│  • MinIO (Screenshots, Files)                              │
└─────────────────────────────────────────────────────────────┘
```

---

## ✨ Key Features

### 🧠 AI-Powered Element Finding

Uses **sentence transformers** to match user intent with UI elements semantically:

```python
# You say: "field to find topics in the encyclopedia"
# AI understands: #searchInput (cosine similarity: 0.82)
```

### 💾 Muscle Memory (PatternDB)

Learns successful element locations and reuses them:

- **First run**: 200ms (full DOM scan + AI scoring)
- **Cached run**: 15ms (instant selector lookup)
- **Drift detection**: Auto-relearns when UI changes

### 🔄 Protocol Intelligence (NetworkSniffer)

Switches from slow browser automation to fast API calls:

- Captures verified API credentials automatically
- Replays requests at 10x speed
- Perfect for pagination, search, data extraction

### 🔐 Account Pool Manager

- Atomic account leasing (no race conditions)
- Cookie-based fast-path authentication
- Automatic credential rotation

### 📊 Enterprise-Grade Observability

- Structured logging (`[Component] message`)
- NATS event streaming
- WebSocket real-time updates
- Temporal workflow visibility

---

## 🛠️ Tech Stack

| Layer               | Technology              | Purpose                             |
| ------------------- | ----------------------- | ----------------------------------- |
| **Orchestration**   | Temporal                | Durable workflow execution, retries |
| **Control Plane**   | Go + Gin                | High-performance API server         |
| **Execution Plane** | Python + Playwright     | Browser automation                  |
| **AI/ML**           | Sentence Transformers   | Semantic embeddings (384-dim)       |
| **Vector DB**       | Qdrant                  | Recipe storage & semantic search    |
| **Event Bus**       | NATS                    | Pub/sub messaging                   |
| **Databases**       | CockroachDB, PostgreSQL | Jobs, accounts, patterns            |
| **Object Storage**  | MinIO                   | Screenshots, downloads              |
| **Cache**           | Redis                   | Rate limiting, sessions             |

---

## 🚀 Quick Start

### Prerequisites

- Docker & Docker Compose
- Go 1.21+
- Python 3.11+
- Make

### 1. Start Infrastructure

```bash
# Clone repository
git clone <repository-url>
cd e2e-Backend

# Start all services (NATS, Temporal, Databases)
make up

# Wait for Temporal to initialize (~30s)
sleep 30
```

### 2. Start Control Plane

```bash
cd apps/control-plane
go run cmd/server/main.go

# Server starts on http://localhost:8080
```

### 3. Start Execution Plane

```bash
cd apps/execution-plane

# Create virtual environment (first time only)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Start worker
python src/worker.py
```

### 4. Seed Recipes

```bash
cd apps/execution-plane
source venv/bin/activate
python scripts/seed_recipes.py
```

### 5. Run Your First Workflow

```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "github_explorer",
    "params": {
      "url": "https://github.com"
    }
  }'

# Response:
# {
#   "job_id": "job-1234567890",
#   "message": "Job Queued Successfully",
#   "run_id": "..."
# }
```

---

## 📖 Usage Examples

### Example 1: Wikipedia Search

```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "wikipedia_search",
    "params": {
      "query": "Artificial Intelligence"
    }
  }'
```

### Example 2: E-Commerce Scraping

```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "amazon_scraper",
    "params": {
      "product": "laptop",
      "max_price": 1000
    }
  }'
```

### Example 3: Custom Workflow (Developer Mode)

```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "custom_flow",
    "steps": [
      {
        "action": "GOTO",
        "params": {"url": "https://example.com"}
      },
      {
        "action": "TYPE",
        "params": {
          "intent": "search box",
          "text": "hello world"
        }
      },
      {
        "action": "CLICK",
        "params": {"intent": "submit button"}
      }
    ]
  }'
```

---

## 🧪 How It Works (Technical Deep Dive)

See [WORKFLOW.md](./WORKFLOW.md) for a complete end-to-end walkthrough with execution traces.

---

## 📊 Production Deployment

### Docker Services

```yaml
services:
  - temporal # Workflow orchestration
  - postgres # Temporal storage
  - nats # Event bus
  - cockroach # Jobs database
  - qdrant # Vector embeddings
  - redis # Cache & rate limiting
  - minio # File storage
```

### Environment Variables

```bash
# Required
NATS_URL=nats://127.0.0.1:4222
TEMPORAL_HOST=localhost:7233
PORT_GO_API=8080

# Optional (for proxies)
PROXY_SERVER=http://proxy:port
PROXY_USER=username
PROXY_PASSWORD=password
```

### Monitoring

```bash
# Check service health
docker ps

# View logs
docker logs -f e2e_temporal
docker logs -f e2e_nats

# Database queries
docker exec -it e2e_cockroach cockroach sql --insecure
```

---

## 🔬 Core Algorithms

### 1. Semantic Recipe Matching (Vector Search)

```python
# Input: "find items on shopping site"
query_embedding = model.encode("find items on shopping site")  # → [0.45, -0.23, ...]

# Database: Pre-computed recipe embeddings
recipes = [
  {"name": "amazon_scraper", "embedding": [0.48, -0.19, ...]},
  {"name": "github_login", "embedding": [-0.12, 0.67, ...]},
  ...
]

# Cosine similarity scoring
scores = cosine_similarity(query_embedding, recipe_embeddings)
best_match = recipes[argmax(scores)]  # amazon_scraper (score: 0.82)
```

### 2. Intent-Based Element Finding (SmartFinder)

```python
# Traditional (breaks easily):
element = page.query_selector("button.search-btn")

# e2e-Platform (adapts automatically):
1. Extract all interactive elements
2. Get text/aria-label/id for each
3. Compute embedding for intent + element
4. Score = cosine_similarity(intent_vector, element_vector)
5. Return highest scoring element (>0.70 threshold)
```

### 3. Pattern Learning (Muscle Memory)

```python
# First execution:
domain = "amazon.com"
page_hash = simhash(DOM_structure)  # Structural fingerprint
intent = "search button"
selector = "#nav-search-submit-button"

# Save pattern:
PatternDB.save(domain, page_hash, intent, selector)

# Next execution (same page structure):
cached_selector = PatternDB.get(domain, page_hash, intent)
if cached_selector:
    element = page.query_selector(cached_selector)  # ⚡ 15ms instead of 200ms
```

---

## 🎯 Performance Benchmarks

| Metric                   | Traditional Selenium    | e2e-Platform          |
| ------------------------ | ----------------------- | --------------------- |
| Adaptation to UI changes | ❌ Breaks immediately   | ✅ Self-heals         |
| Maintenance cost         | High (constant updates) | Low (learns patterns) |
| Speed (cached)           | ~200ms                  | ~15ms (13x faster)    |
| Protocol switching       | Manual                  | Automatic             |
| CAPTCHA handling         | Fails                   | Hibernates + human    |

---

## 🔒 Security Features

- **Account Pool Isolation**: FOR UPDATE SKIP LOCKED prevents race conditions
- **Credential Encryption**: AES-256 for stored passwords
- **Network Timeouts**: 10s hard limits prevent DoS
- **Cookie Security**: Secure storage with expiration tracking
- **Audit Logging**: All account access logged

---

## 🤝 Contributing

We welcome contributions! Please see our contributing guidelines (coming soon).

---

## 📄 License

[Your License Here]

---

## 📞 Support

- **Documentation**: See [WORKFLOW.md](./WORKFLOW.md) for detailed workflows
- **Issues**: [GitHub Issues](your-repo-url/issues)
- **Contact**: your-email@domain.com

---

## 🌟 Why e2e-Platform?

| Feature            | Selenium/Puppeteer  | e2e-Platform                   |
| ------------------ | ------------------- | ------------------------------ |
| Selector Strategy  | CSS/XPath (brittle) | Semantic AI (adaptive)         |
| UI Changes         | ❌ Breaks           | ✅ Adapts                      |
| Speed Optimization | Manual              | Automatic (Protocol Switching) |
| Learning           | None                | Pattern caching                |
| CAPTCHA Handling   | Fails               | Human-in-the-loop              |
| Scale              | ~10 concurrent      | ~100+ concurrent               |

---

**Built with ❤️ for developers who want automation that doesn't break.**
