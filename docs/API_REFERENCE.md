# API Quick Reference

> **Base URL:** `http://localhost:8080` (dev) | `https://api.quanta.io` (prod)

---

## Endpoints

| Method | Path            | Description                      | Auth Required |
| ------ | --------------- | -------------------------------- | ------------- |
| POST   | `/run`          | Start automation job             | ✅ Yes        |
| POST   | `/resume`       | Resume paused job (human input)  | ❌ No         |
| GET    | `/ws`           | WebSocket for live job updates   | ❌ No         |
| GET    | `/health`       | Full health check (all services) | ❌ No         |
| GET    | `/health/live`  | Liveness probe (Kubernetes)      | ❌ No         |
| GET    | `/health/ready` | Readiness probe (Kubernetes)     | ❌ No         |
| GET    | `/metrics`      | Prometheus metrics               | ❌ No         |

---

## POST /run

Start a new automation workflow.

**Request:**

```json
{
  "workflow_id": "uuid",
  "params": { "key": "value" },
  "config": {
    "use_premium_proxy": false,
    "solve_captchas": false,
    "session_id": "optional",
    "region": "us"
  }
}
```

**Response (202):**

```json
{
  "message": "Job Queued Successfully",
  "job_id": "uuid",
  "run_id": "temporal-run-id",
  "trace_ws": "/ws?job_id=uuid"
}
```

---

## POST /resume

Resume a paused workflow (human-in-the-loop).

**Request:**

```json
{
  "job_id": "uuid",
  "data": { "otp": "123456" }
}
```

**Response (200):**

```json
{
  "success": true,
  "message": "Workflow resumed",
  "job_id": "uuid"
}
```

---

## WebSocket Events

Connect: `ws://localhost:8080/ws?job_id=<job_id>`

**Event Format:**

```json
{
  "job_id": "uuid",
  "status": "RUNNING|COMPLETED|FAILED|PAUSED",
  "message": "User-friendly log message",
  "node_id": "step_1",
  "timestamp": 1704124800
}
```

---

## Headers

| Header                  | Description                        | Example        |
| ----------------------- | ---------------------------------- | -------------- |
| `X-User-ID`             | Clerk user ID (required for run)   | `user_2abc123` |
| `X-RateLimit-Limit`     | Max requests per window            | `5`            |
| `X-RateLimit-Remaining` | Requests left in window            | `3`            |
| `X-RateLimit-Reset`     | Unix timestamp when window resets  | `1704124800`   |
| `Retry-After`           | Seconds until next request allowed | `45`           |

---

## Status Codes

| Code | Meaning               | When                                |
| ---- | --------------------- | ----------------------------------- |
| 200  | OK                    | Successful request                  |
| 202  | Accepted              | Job queued (async)                  |
| 400  | Bad Request           | Invalid JSON or missing fields      |
| 401  | Unauthorized          | Missing X-User-ID header            |
| 429  | Too Many Requests     | Rate limit exceeded                 |
| 500  | Internal Server Error | Backend failure                     |
| 503  | Service Unavailable   | Dependency down (DB, Temporal, etc) |

---

## Job Status Values

| Status      | Description                           |
| ----------- | ------------------------------------- |
| `QUEUED`    | Waiting in queue                      |
| `RUNNING`   | Currently executing                   |
| `PAUSED`    | Waiting for human input (CAPTCHA/2FA) |
| `COMPLETED` | Successfully finished                 |
| `FAILED`    | Error occurred                        |
| `CANCELLED` | User cancelled                        |

---

_See [FRONTEND_INTEGRATION.md](./FRONTEND_INTEGRATION.md) for detailed integration guide._
