# 🎨 Frontend Integration Guide

### _Connecting the UI to the Brain_

This document details the API contracts and WebSocket events required to build a dynamic frontend for the e2e-Platform.

---

## 📡 REST API

**Base URL**: `http://localhost:8080` (or your production domain)

### 1. Trigger a Workflow

**POST** `/run`

Starts a new automation job.

**Request Body:**

```json
{
  "workflow_id": "hackernews_scraper_v1",
  "params": {
    "limit": "10",
    "search_term": "AI"
  },
  "config": {
    "use_premium_proxy": true,
    "solve_captchas": true,
    "region": "us"
  }
}
```

**Response (202 Accepted):**

```json
{
  "message": "Job Queued Successfully",
  "job_id": "job-1733812345",
  "run_id": "a1b2c3d4...",
  "trace_ws": "/ws?job_id=job-1733812345"
}
```

---

## 🔌 Real-Time Updates (WebSockets)

To show a "Live Console" to the user, connect to the WebSocket immediately after getting the `job_id`.

**Endpoint**: `ws://localhost:8080/ws?job_id=<JOB_ID>`

### Event Format

All messages sent from the server follow this JSON structure:

```json
{
  "job_id": "job-1733812345",
  "status": "RUNNING",
  "message": "✅ Found 'login button' (Score: 0.98)",
  "node_id": "worker-1",
  "timestamp": "2025-12-10T10:00:00Z",
  "metadata": {
    "screenshot_url": "https://...",
    "confidence": "0.98"
  }
}
```

### Status Types

Handle these statuses in your UI:

| Status      | UI Behavior                                  |
| ----------- | -------------------------------------------- |
| `QUEUED`    | Show "Waiting for worker..." spinner.        |
| `RUNNING`   | Show live logs. Green dot indicator.         |
| `COMPLETED` | Show success banner. Display extracted data. |
| `FAILED`    | Show error alert. Display error message.     |

---

## 🖼️ Displaying Screenshots

If `metadata.screenshot_url` is present in the event, render it immediately. This allows the user to "see" what the bot is doing.

```javascript
// React Example
socket.onmessage = (event) => {
  const data = JSON.parse(event.data);
  setLogs((prev) => [...prev, data.message]);

  if (data.metadata?.screenshot_url) {
    setPreviewImage(data.metadata.screenshot_url);
  }
};
```

---

## 🎣 Webhooks (Server-to-Server)

If you need to update your database when a job finishes, configure a webhook.

**Payload sent to your server:**

```json
{
  "job_id": "job-1733812345",
  "status": "COMPLETED",
  "data": {
    "result": { ...extracted data... }
  },
  "timestamp": "..."
}
```
