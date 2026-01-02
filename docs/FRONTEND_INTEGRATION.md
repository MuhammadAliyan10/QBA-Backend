# Frontend Integration Guide

> **Version**: 1.0 | Last Updated: January 1, 2026
> **Purpose**: Everything a frontend developer needs to know to integrate with the Quanta backend.

---

## Table of Contents

1. [API Reference](#1-api-reference)
2. [Database Schema Overview](#2-database-schema-overview)
3. [Data Flow Diagrams](#3-data-flow-diagrams)
4. [WebSocket Events](#4-websocket-events)
5. [Authentication](#5-authentication)
6. [Error Handling](#6-error-handling)
7. [TypeScript Interfaces](#7-typescript-interfaces)

---

## 1. API Reference

### Base URL

| Environment | URL                               |
| ----------- | --------------------------------- |
| Development | `http://localhost:8080`           |
| Production  | `https://api.quanta.io` (example) |

---

### 1.1 Start Automation Job

**Endpoint:** `POST /run`

Starts a new automation workflow. The job runs asynchronously and returns immediately with a `job_id` for tracking.

#### Request

```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -H "X-User-ID: user_clerk_id_here" \
  -d '{
    "workflow_id": "550e8400-e29b-41d4-a716-446655440000",
    "params": {
      "username": "john@example.com",
      "target_url": "https://linkedin.com"
    },
    "config": {
      "use_premium_proxy": true,
      "solve_captchas": false,
      "session_id": "optional-session-id",
      "region": "us"
    }
  }'
```

#### Request Body Schema

| Field                      | Type          | Required | Description                             |
| -------------------------- | ------------- | -------- | --------------------------------------- |
| `workflow_id`              | string (UUID) | ✅       | ID of the saved workflow to execute     |
| `params`                   | object        | ❌       | Key-value params passed to the workflow |
| `config.use_premium_proxy` | boolean       | ❌       | Use residential proxy (costs more)      |
| `config.solve_captchas`    | boolean       | ❌       | Auto-solve CAPTCHAs (costs more)        |
| `config.session_id`        | string        | ❌       | Reuse saved browser session             |
| `config.region`            | string        | ❌       | Proxy region: `us`, `eu`, `asia`        |

#### Response (202 Accepted)

```json
{
  "message": "Job Queued Successfully",
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "run_id": "temporal-run-id-xyz",
  "trace_ws": "/ws?job_id=a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

#### Response Fields

| Field      | Type   | Description                                 |
| ---------- | ------ | ------------------------------------------- |
| `message`  | string | Human-readable status                       |
| `job_id`   | string | UUID v4 to track this job                   |
| `run_id`   | string | Temporal workflow run ID (for debugging)    |
| `trace_ws` | string | WebSocket path to subscribe to live updates |

#### Error Responses

| Status | Error                        | Cause                      |
| ------ | ---------------------------- | -------------------------- |
| 400    | `Invalid JSON body`          | Malformed request body     |
| 401    | `Authentication required...` | Missing `X-User-ID` header |
| 429    | `Rate limit exceeded`        | Too many jobs per minute   |
| 500    | `Failed to start workflow`   | Temporal connection failed |

---

### 1.2 Resume Human-in-the-Loop Workflow

**Endpoint:** `POST /resume`

When a workflow encounters a human gate (CAPTCHA, 2FA, approval request), it hibernates and waits for user input. Use this endpoint to provide that input and resume execution.

#### Request

```bash
curl -X POST http://localhost:8080/resume \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "data": {
      "otp": "123456"
    }
  }'
```

#### Request Body Schema

| Field    | Type          | Required | Description                       |
| -------- | ------------- | -------- | --------------------------------- |
| `job_id` | string (UUID) | ✅       | The hibernating job to resume     |
| `data`   | object        | ✅       | User input (OTP, selection, etc.) |

#### Response (200 OK)

```json
{
  "success": true,
  "message": "Workflow resumed",
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

#### Error Responses

| Status | Error                       | Cause                             |
| ------ | --------------------------- | --------------------------------- |
| 400    | `job_id is required`        | Missing job_id in request         |
| 500    | `Failed to signal workflow` | Workflow not running or not found |

---

### 1.3 Health Check Endpoints

#### Full Health Check

**Endpoint:** `GET /health`

Returns health status of all dependencies (Database, Redis, NATS).

```json
{
  "status": "healthy",
  "time": "2026-01-01T12:00:00Z",
  "services": {
    "database": "healthy",
    "redis": "healthy",
    "nats": "healthy"
  }
}
```

#### Liveness Probe

**Endpoint:** `GET /health/live`

Fast check if the server is alive (for Kubernetes).

```json
{
  "status": "alive",
  "time": "2026-01-01T12:00:00Z"
}
```

#### Readiness Probe

**Endpoint:** `GET /health/ready`

Same as `/health` - checks if the server is ready to accept traffic.

---

### 1.4 Prometheus Metrics

**Endpoint:** `GET /metrics`

Returns Prometheus-format metrics for monitoring. Useful for dashboards.

---

## 2. Database Schema Overview

The frontend connects to the same PostgreSQL database (Supabase) as the backend. Here's what you need to know:

### 2.1 Entity Relationship Diagram

```mermaid
erDiagram
    UserProfile ||--o| UserUsage : has
    UserProfile ||--o{ CreditTransaction : makes
    UserProfile ||--o{ VaultSecret : owns
    UserProfile ||--o{ Workflow : creates
    UserProfile ||--o{ Job : runs
    UserProfile ||--o{ StorageAsset : uploads
    UserProfile ||--o{ ApiKey : generates

    Workflow ||--o{ Job : executes

    Job ||--o{ JobLog : produces
    Job ||--o{ CreditTransaction : costs
    Job ||--o{ StorageAsset : generates
```

### 2.2 Key Models for Frontend

#### UserProfile

The core user identity, linked to Clerk authentication.

| Field         | Type    | Description                 |
| ------------- | ------- | --------------------------- |
| `id`          | UUID    | Primary key                 |
| `clerkUserId` | string  | Clerk's user ID (for auth)  |
| `email`       | string  | User email                  |
| `firstName`   | string? | Optional first name         |
| `lastName`    | string? | Optional last name          |
| `avatarUrl`   | string? | Profile picture URL         |
| `tier`        | enum    | `FREE`, `PRO`, `ENTERPRISE` |
| `webhookUrl`  | string? | User's webhook endpoint     |

#### UserUsage

The user's credit wallet.

| Field              | Type | Description                |
| ------------------ | ---- | -------------------------- |
| `id`               | UUID | Primary key                |
| `userId`           | UUID | Foreign key to UserProfile |
| `creditsBalance`   | int  | Current credit balance     |
| `totalJobsRun`     | int  | Lifetime job count         |
| `totalCreditsUsed` | int  | Lifetime credits spent     |

#### Workflow

A saved automation recipe (DAG).

| Field          | Type    | Description                          |
| -------------- | ------- | ------------------------------------ |
| `id`           | UUID    | Primary key                          |
| `userId`       | UUID    | Owner                                |
| `name`         | string  | Display name                         |
| `description`  | string? | Optional description                 |
| `triggerType`  | enum    | `ON_DEMAND`, `SCHEDULED`, `WEBHOOK`  |
| `cronSchedule` | string? | Cron expression (if scheduled)       |
| `recipeJson`   | JSON    | **The DAG structure** (nodes, edges) |
| `isActive`     | bool    | Is workflow enabled?                 |
| `runCount`     | int     | How many times it has been executed  |

#### Job

A single execution of a workflow.

| Field          | Type    | Description                                                       |
| -------------- | ------- | ----------------------------------------------------------------- |
| `id`           | UUID    | Primary key (this is the `job_id` from API)                       |
| `userId`       | UUID    | Who ran this job                                                  |
| `workflowId`   | UUID    | Which workflow was executed                                       |
| `status`       | enum    | `QUEUED`, `RUNNING`, `PAUSED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `scheduledAt`  | date?   | When it was scheduled                                             |
| `startedAt`    | date?   | When it actually started                                          |
| `completedAt`  | date?   | When it finished                                                  |
| `durationMs`   | int?    | Total execution time                                              |
| `currentStep`  | int?    | For progress bar (current step #)                                 |
| `currentState` | JSON?   | Checkpoint data for resumability                                  |
| `creditsUsed`  | int     | Credits charged for this job                                      |
| `errorMessage` | string? | Error description if failed                                       |
| `resultJson`   | JSON?   | Extracted data output                                             |
| `resultUrl`    | string? | URL to download result file                                       |

#### JobLog

High-volume execution logs. Use for "Glass Box" live log viewer.

| Field       | Type    | Description                      |
| ----------- | ------- | -------------------------------- |
| `id`        | UUID    | Primary key                      |
| `jobId`     | UUID    | Parent job                       |
| `level`     | enum    | `DEBUG`, `INFO`, `WARN`, `ERROR` |
| `message`   | string  | Log message                      |
| `nodeId`    | string? | Which workflow node              |
| `stepIndex` | int?    | Step number                      |
| `timestamp` | date    | When logged                      |

#### StorageAsset

Files stored in Azure Blob (or MinIO locally).

| Field          | Type   | Description                                 |
| -------------- | ------ | ------------------------------------------- |
| `id`           | UUID   | Primary key                                 |
| `userId`       | UUID   | Owner                                       |
| `jobId`        | UUID?  | Which job generated this (null if uploaded) |
| `type`         | enum   | `OUTPUT`, `INPUT`, `CHECKPOINT`             |
| `filename`     | string | Original filename                           |
| `mimeType`     | string | e.g., `application/pdf`                     |
| `sizeBytes`    | int    | File size                                   |
| `azureBlobUrl` | string | Presigned URL to download                   |
| `isPublic`     | bool   | Can be accessed without auth?               |
| `expiresAt`    | date?  | Auto-delete after this time                 |

---

## 3. Data Flow Diagrams

### 3.1 Starting a Job

```mermaid
sequenceDiagram
    participant F as Frontend
    participant A as Go API (:8080)
    participant T as Temporal
    participant W as Python Worker
    participant DB as Supabase

    F->>A: POST /run { workflow_id, params }
    A->>A: Generate job_id (UUID v4)
    A->>T: ExecuteWorkflow(BrowserWorkflow, payload)
    T-->>A: workflow started (run_id)
    A-->>F: 202 { job_id, trace_ws }

    F->>A: Connect to /ws?job_id=xxx

    T->>W: Schedule Activity
    W->>W: Launch Playwright Browser
    W->>W: Execute Steps
    W-->>N: Publish job.update.{job_id}
    N-->>A: Event received
    A-->>F: WebSocket: { status: RUNNING, message: "..." }

    W->>DB: Update Job status
    W-->>N: Publish COMPLETED
    N-->>A: Event received
    A-->>F: WebSocket: { status: COMPLETED }
    A->>F: Trigger Webhook (if configured)
```

### 3.2 Human-in-the-Loop Flow

```mermaid
sequenceDiagram
    participant F as Frontend
    participant A as Go API
    participant T as Temporal
    participant W as Python Worker

    W->>W: Detect CAPTCHA/2FA
    W->>T: Raise HumanInterventionRequired
    T->>T: Workflow Hibernates (0 CPU)

    Note over T: Waiting for Signal...

    T-->>A: NATS: status=WAITING_FOR_USER
    A-->>F: WebSocket: { status: PAUSED, reason: "CAPTCHA" }

    F->>F: Show CAPTCHA UI to User
    F->>A: POST /resume { job_id, data: { solution: "xyz" } }
    A->>T: SignalWorkflow(USER_INTERACTION, data)
    T->>T: Workflow Resumes
    T->>W: Continue Activity
```

### 3.3 Credit Deduction Flow

```mermaid
sequenceDiagram
    participant F as Frontend
    participant DB as Supabase
    participant W as Python Worker

    Note over F,DB: Before Job Starts
    F->>DB: Read creditsBalance from user_usage
    F->>F: Check: balance >= estimated_cost?

    Note over W,DB: During Job Execution
    W->>W: Each step executed
    W->>DB: INSERT INTO credit_transactions <br/> (type: DEBIT, amount: -1)
    W->>DB: UPDATE user_usage SET creditsBalance -= 1

    Note over W,DB: On Job Failure
    W->>DB: INSERT INTO credit_transactions <br/> (type: REFUND, amount: +credits_used)
    W->>DB: UPDATE user_usage SET creditsBalance += credits_used
```

---

## 4. WebSocket Events

### 4.1 Connecting to WebSocket

```javascript
const ws = new WebSocket(`ws://localhost:8080/ws?job_id=${jobId}`);

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log("Job Update:", data);
};
```

### 4.2 Event Schema

Each WebSocket message is a Protobuf-encoded `JobEvent`:

```typescript
interface JobEvent {
  job_id: string;
  status:
    | "QUEUED"
    | "RUNNING"
    | "PAUSED"
    | "COMPLETED"
    | "FAILED"
    | "CANCELLED";
  message: string; // User-friendly log message
  node_id: string; // Which workflow node
  timestamp: number; // Unix timestamp
  screenshot_preview?: Uint8Array; // Base64-encoded thumbnail
}
```

### 4.3 Status Transitions

```mermaid
stateDiagram-v2
    [*] --> QUEUED: Job Created
    QUEUED --> RUNNING: Worker Picks Up
    RUNNING --> PAUSED: Human Intervention
    PAUSED --> RUNNING: User Resumes
    RUNNING --> COMPLETED: Success
    RUNNING --> FAILED: Error
    RUNNING --> CANCELLED: User Cancels
    PAUSED --> CANCELLED: User Cancels
    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

---

## 5. Authentication

### 5.1 Clerk Integration

The frontend uses **Clerk** for authentication. The Go backend expects:

```http
X-User-ID: user_clerk_id_here
```

This header should contain the Clerk user ID (e.g., `user_2abc123def456`).

### 5.2 API Key Authentication

For programmatic access, users can generate API keys:

```http
Authorization: Bearer sk_live_xxxxxxxxxxxxxx
```

API keys are stored as SHA-256 hashes in the `api_keys` table.

### 5.3 Rate Limiting

| Tier       | Limit          | Window |
| ---------- | -------------- | ------ |
| FREE       | 5 jobs/minute  | 60s    |
| PRO        | 20 jobs/minute | 60s    |
| ENTERPRISE | Custom         | Custom |

Rate limit headers returned on every request:

```http
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 3
X-RateLimit-Reset: 1704124800
Retry-After: 45
```

---

## 6. Error Handling

### 6.1 HTTP Status Codes

| Code | Meaning             | Frontend Action                    |
| ---- | ------------------- | ---------------------------------- |
| 200  | Success             | Display result                     |
| 202  | Accepted (async)    | Show "Job started" + connect to WS |
| 400  | Bad Request         | Show validation error              |
| 401  | Unauthorized        | Redirect to login                  |
| 429  | Rate Limited        | Show "Please wait" + countdown     |
| 500  | Server Error        | Show generic error + retry button  |
| 503  | Service Unavailable | Show maintenance banner            |

### 6.2 Error Response Format

```json
{
  "error": "Rate limit exceeded",
  "message": "Maximum 5 jobs per minute. Please try again later.",
  "retry_after": 45
}
```

---

## 7. TypeScript Interfaces

Copy these interfaces to your frontend codebase:

```typescript
// ============================================
// API TYPES
// ============================================

export interface StartJobRequest {
  workflow_id: string;
  params?: Record<string, string>;
  config?: {
    use_premium_proxy?: boolean;
    solve_captchas?: boolean;
    session_id?: string;
    region?: "us" | "eu" | "asia";
  };
}

export interface StartJobResponse {
  message: string;
  job_id: string;
  run_id: string;
  trace_ws: string;
}

export interface ResumeJobRequest {
  job_id: string;
  data: Record<string, string>;
}

export interface ResumeJobResponse {
  success: boolean;
  message: string;
  job_id: string;
}

export interface HealthResponse {
  status: "healthy" | "unhealthy";
  time: string;
  services: {
    database: string;
    redis: string;
    nats: string;
  };
}

// ============================================
// DATABASE ENUMS (matches Prisma schema)
// ============================================

export type UserTier = "FREE" | "PRO" | "ENTERPRISE";

export type JobStatus =
  | "QUEUED"
  | "RUNNING"
  | "PAUSED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export type TransactionType = "CREDIT" | "DEBIT" | "REFUND" | "ADJUSTMENT";

export type TriggerType = "ON_DEMAND" | "SCHEDULED" | "WEBHOOK";

export type LogLevel = "DEBUG" | "INFO" | "WARN" | "ERROR";

export type AssetType = "OUTPUT" | "INPUT" | "CHECKPOINT";

// ============================================
// DATABASE MODELS (simplified for frontend)
// ============================================

export interface UserProfile {
  id: string;
  clerkUserId: string;
  email: string;
  firstName?: string;
  lastName?: string;
  avatarUrl?: string;
  tier: UserTier;
  webhookUrl?: string;
  createdAt: Date;
  updatedAt: Date;
}

export interface UserUsage {
  id: string;
  userId: string;
  creditsBalance: number;
  totalJobsRun: number;
  totalCreditsUsed: number;
  createdAt: Date;
  updatedAt: Date;
}

export interface Workflow {
  id: string;
  userId: string;
  name: string;
  description?: string;
  triggerType: TriggerType;
  cronSchedule?: string;
  recipeJson: object; // The DAG structure
  isActive: boolean;
  lastRunAt?: Date;
  runCount: number;
  createdAt: Date;
  updatedAt: Date;
}

export interface Job {
  id: string;
  userId: string;
  workflowId: string;
  status: JobStatus;
  scheduledAt?: Date;
  startedAt?: Date;
  completedAt?: Date;
  durationMs?: number;
  currentStep?: number;
  currentState?: object;
  creditsUsed: number;
  errorMessage?: string;
  errorStack?: string;
  retryCount: number;
  resultJson?: object;
  resultUrl?: string;
  createdAt: Date;
  updatedAt: Date;
}

export interface JobLog {
  id: string;
  jobId: string;
  level: LogLevel;
  message: string;
  nodeId?: string;
  stepIndex?: number;
  durationMs?: number;
  metadata?: object;
  timestamp: Date;
}

export interface StorageAsset {
  id: string;
  userId: string;
  jobId?: string;
  type: AssetType;
  filename: string;
  friendlyName?: string;
  mimeType: string;
  sizeBytes: number;
  azureBlobUrl: string;
  isPublic: boolean;
  expiresAt?: Date;
  createdAt: Date;
}

// ============================================
// WEBSOCKET EVENTS
// ============================================

export interface JobEvent {
  job_id: string;
  status: JobStatus;
  message: string;
  node_id: string;
  timestamp: number;
  screenshot_preview?: string; // Base64 encoded
}

export interface HumanInterventionEvent {
  job_id: string;
  reason:
    | "CAPTCHA_DETECTED"
    | "2FA_REQUIRED"
    | "APPROVAL_NEEDED"
    | "PRICE_CONFIRMATION";
  prompt: string;
  options: string[];
  context?: {
    url?: string;
    screenshot_url?: string;
  };
}
```

---

## Appendix: Environment Variables

Frontend apps need these environment variables to connect:

| Variable                            | Description                  | Example                 |
| ----------------------------------- | ---------------------------- | ----------------------- |
| `NEXT_PUBLIC_API_URL`               | Backend API base URL         | `http://localhost:8080` |
| `NEXT_PUBLIC_WS_URL`                | WebSocket base URL           | `ws://localhost:8080`   |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk frontend key           | `pk_test_xxx`           |
| `DATABASE_URL`                      | Supabase connection (server) | `postgresql://...`      |

---

_End of Frontend Integration Guide_
