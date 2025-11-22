## e2e | Intelligent Automation Platform

**Current Version:** 0.1.0-alpha **Architecture:** Polyglot Microservices (Go + Python)

<hr/>

### 1. System Overview

e2e is a high-performance RPA platform that decouples Control from Execution.

- **Control Plane (Go):** Handles high-concurrency I/O (WebSockets, Auth, Billing) and routes traffic.

- **Execution Plane (Python):** Runs heavy compute tasks (Headless Browsers, AI Inference) in isolated sandboxes.

- **The Nervous System (NATS):** connects the two planes asynchronously.

### 2. Repository Structure

This is a Monorepo containing both backends and infrastructure code.

- **/api:** Contains .proto definitions. This is the Single Source of Truth. Both Go and Python generate their types from these files.

- **/apps/control-plane:** The Go API Gateway. Uses Gin for HTTP and Melody for WebSockets.

- **/apps/execution-plane:** The Python Worker. Uses Playwright for automation and Temporal for orchestration.

- **/infra:** Terraform and Kubernetes manifests for AWS deployment.

### 3. The Core Workflow

1. **Ingestion:** User sends a prompt to Go Gateway (gRPC-Web).

2. **Validation:** Go verifies Auth (Clerk) and Credits (Redis).

3. **Queueing:** Go publishes a JobRequest to NATS JetStream.

4. **Orchestration:** Temporal picks up the job and assigns it to a Python Worker.

5. **Execution:** Python Worker launches a browser, executes steps using Heuristic Algorithms, and streams logs back to NATS.

6. **Visualization:** Go consumes NATS logs and pushes them to the Frontend via WebSocket.

### 4. Prerequisites

- `Go` 1.22+

- `Python 3.11+` (Poetry recommended)

- `Docker` & Docker Compose

- `Protoc` (Protocol Buffer Compiler)

### 5. Quick Start (Local Development)

#### Step 1: Infrastructure

Start the local dependency stack (Postgres, NATS, Temporal, Redis):

```sh
make up
```

#### Step 2: Code Generation

Compile the Protobuf definitions into Go and Python code:

```sh
make proto
```

#### Step 3: Run Services

Terminal 1 (Control Plane):

```sh
cd apps/control-plane && go run cmd/server/main.go
```

#### Terminal 2 (Execution Plane):

```sh
cd apps/execution-plane && poetry run python src/worker.py
```

#### 6. Security Protocols

- **Credentials:** Never log raw passwords. Use `AWS KMS `encryption helpers located in `apps/execution-plane/src/core/security.py`.

- **Networking:** Workers must run with gVisor runtime in production.

<br>
<br>
<br>

# Flow and setup of the backend

### 1. The Contract

In a distributed system like this (Go + Python + NATS), you cannot just "send JSON strings" and hope the other side understands them. That leads to crashes when a Python worker expects `user_id` (string) but the Go server sends `userID` (int).

Copyright © 2025 e2e Platform. Confidential.

### Step 2: The Local Infrastructure.

You need to spin up your "Virtual Data Center" on your laptop.
We will use Docker Compose to spin up Temporal, NATS, CockroachDB, and Redis instantly. This allows you to develop offline without paying AWS a single cent.

#### Generated Files

1. **docker-compose.yml:** The blueprint for your local cloud.

2. **config/nats.conf:** The configuration to enable JetStream (High-speed streaming) on NATS.

3. **.env:** The environment variables for your local setup.

### Run

To run it

```bash
docker-compose up -d
```

To stop it

```bash
docker-compose stop
```

### Phase 3: The Muscle

We are now building the robot that actually does the work. This worker will:

1. **Listen** to Temporal for tasks.

2. **Launch** a Headless Browser (Playwright).

3. **Use Math** (Levenshtein) to find buttons instantly.

4. **Stream Logs** to NATS so the Frontend updates live.

#### Step 1: Organize the Algorithms

First, let's put the "Math" code we wrote earlier into its proper home.

1.  The Math Engine

#### Step 2: The Nervous System Client

The Worker needs to shout "I found the button!" to the rest of the system. We use NATS for this.

#### Step 3: The Activities (The "Doing" Part)

This is where Playwright lives. It runs the browser, scrapes the DOM, and uses our Math class to find buttons.

#### Step 4: The Workflow (The Logic)

This tells Temporal how to run the activities.

#### Step 5: The Worker Entrypoint

his is the main function that connects everything.
