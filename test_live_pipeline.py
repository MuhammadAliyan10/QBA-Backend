# test_live_pipeline.py
# Live-fire integration test: Go API → Temporal → Python Worker → Data Extraction
# Target: Hacker News (Algolia search)
# Usage: python test_live_pipeline.py

import sys
import json
import time
import threading
import logging
from typing import Optional

import requests

# ─── CONFIG ───────────────────────────────────────────────────────────────────

API_BASE_URL = "http://localhost:8080"
EXECUTE_ENDPOINT = f"{API_BASE_URL}/v1/execute"
SSE_ENDPOINT_TEMPLATE = f"{API_BASE_URL}/v1/execute/{{job_id}}/stream"

import uuid

PAYLOAD = {
    "target_url": "https://www.ycombinator.com/companies",
    "objective": "Type 'Agentic AI' into the search bar. Open the 'Batch' filter and select 'W24'. Intercept the network traffic and extract the top 5 startups, including their Name, Short Description, and Location.",
    "workflow_id": str(uuid.uuid4())
}

SSE_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline_test")

# ─── TASK 1: TRIGGER ─────────────────────────────────────────────────────────


def trigger_execution() -> Optional[str]:
    """Send POST /v1/execute and return the job_id or None on failure."""
    logger.info("="*60)
    logger.info("PHASE 1: Triggering execution pipeline")
    logger.info("="*60)
    logger.info(f"Endpoint : {EXECUTE_ENDPOINT}")
    logger.info(f"Target   : {PAYLOAD['target_url']}")
    logger.info(f"Objective: {PAYLOAD['objective'][:80]}...")

    try:
        response = requests.post(
            EXECUTE_ENDPOINT,
            json=PAYLOAD,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except requests.ConnectionError as e:
        logger.error(f"Connection REFUSED — is the Go Control Plane running on :8080? {e}")
        return None
    except requests.Timeout:
        logger.error("Request timed out after 15s")
        return None

    logger.info(f"HTTP Status: {response.status_code}")

    if response.status_code >= 500:
        logger.error(f"Server error (5xx). Raw response:\n{response.text}")
        return None

    if response.status_code >= 400:
        logger.warning(f"Client error ({response.status_code}). Raw response:\n{response.text}")
        return None

    try:
        body = response.json()
    except json.JSONDecodeError:
        logger.error(f"Non-JSON response body:\n{response.text}")
        return None

    job_id: Optional[str] = body.get("job_id") or body.get("jobId") or body.get("id")
    if not job_id:
        logger.error(f"No job_id in response. Full body:\n{json.dumps(body, indent=2)}")
        return None

    logger.info(f"✅ Job queued: {job_id}")
    logger.info(f"Full response:\n{json.dumps(body, indent=2)}")
    return job_id


# ─── TASK 2: SSE TELEMETRY LISTENER ──────────────────────────────────────────


def listen_sse(job_id: str, stop_event: threading.Event) -> None:
    """
    Open an SSE stream to GET /v1/execute/{job_id}/stream and print
    every incoming event to the console in real-time.
    """
    url = SSE_ENDPOINT_TEMPLATE.format(job_id=job_id)
    logger.info(f"Opening SSE stream: {url}")

    try:
        with requests.get(url, stream=True, timeout=SSE_TIMEOUT_SECONDS) as resp:
            if resp.status_code != 200:
                logger.warning(
                    f"SSE endpoint returned {resp.status_code}: {resp.text[:200]}"
                )
                return

            event_type = ""
            data_buffer = ""

            for line in resp.iter_lines(decode_unicode=True):
                if stop_event.is_set():
                    break

                if line is None:
                    continue

                line = line.strip() if isinstance(line, str) else line.decode().strip()

                if not line:
                    # Empty line = end of event
                    if data_buffer:
                        _print_sse_event(event_type, data_buffer)
                        # Check for terminal events
                        if _is_terminal_event(data_buffer):
                            stop_event.set()
                    event_type = ""
                    data_buffer = ""
                    continue

                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_buffer += line[5:].strip()
                elif line.startswith(":"):
                    # SSE comment / keepalive — ignore
                    pass

    except requests.ConnectionError:
        logger.warning("SSE stream disconnected (connection closed by server)")
    except requests.Timeout:
        logger.warning(f"SSE stream timed out after {SSE_TIMEOUT_SECONDS}s")
    except Exception as e:
        logger.error(f"SSE listener error: {type(e).__name__}: {e}")


def _print_sse_event(event_type: str, data: str) -> None:
    """Pretty-print an SSE event."""
    prefix = f"[SSE:{event_type}]" if event_type else "[SSE]"
    try:
        parsed = json.loads(data)
        status = parsed.get("status", "")
        message = parsed.get("message", "")
        node_id = parsed.get("node_id", "")

        icon = {
            "RUNNING": "⚡",
            "COMPLETED": "✅",
            "FAILED": "❌",
            "QUEUED": "📋",
        }.get(status, "📡")

        logger.info(f"{prefix} {icon} [{status}] {message} (node: {node_id})")

        # Print extracted data if present
        extracted = parsed.get("data")
        if extracted:
            logger.info(f"{prefix}   └─ Data: {extracted[:200]}")

    except json.JSONDecodeError:
        logger.info(f"{prefix} {data[:200]}")


def _is_terminal_event(data: str) -> bool:
    """Check if the SSE event indicates the job has terminated."""
    try:
        parsed = json.loads(data)
        status = parsed.get("status", "").upper()
        return status in ("COMPLETED", "FAILED", "TIMEOUT", "CANCELLED")
    except json.JSONDecodeError:
        return False


# ─── TASK 3: JOB POLLING FALLBACK ────────────────────────────────────────────


def poll_job_status(job_id: str, stop_event: threading.Event) -> None:
    """
    Fallback: poll GET /v1/jobs/{job_id} every N seconds until terminal state.
    Used if the SSE stream is not available.
    """
    url = f"{API_BASE_URL}/v1/jobs/{job_id}"
    logger.info(f"Polling job status: {url}")

    max_polls = SSE_TIMEOUT_SECONDS // POLL_INTERVAL_SECONDS
    for i in range(max_polls):
        if stop_event.is_set():
            break

        time.sleep(POLL_INTERVAL_SECONDS)

        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"Poll {i+1}: HTTP {resp.status_code}")
                continue

            body = resp.json()
            status = body.get("status", "UNKNOWN")
            logger.info(f"Poll {i+1}: status={status}")

            if status.upper() in ("COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"):
                logger.info(f"Terminal state reached: {status}")
                logger.info(f"Final result:\n{json.dumps(body, indent=2)}")
                stop_event.set()
                return

        except requests.ConnectionError:
            logger.warning(f"Poll {i+1}: connection refused")
        except Exception as e:
            logger.warning(f"Poll {i+1}: {e}")

    logger.warning("Polling timed out — job may still be running")


# ─── MAIN ─────────────────────────────────────────────────────────────────────


def main() -> None:
    logger.info("Quanta Live Pipeline Test")
    logger.info(f"API Base: {API_BASE_URL}")
    logger.info("")

    # Phase 1: Trigger
    job_id = trigger_execution()
    if not job_id:
        logger.error("ABORT — failed to queue job")
        sys.exit(1)

    # Phase 2: Listen for telemetry
    logger.info("")
    logger.info("="*60)
    logger.info("PHASE 2: Real-time telemetry")
    logger.info("="*60)

    stop_event = threading.Event()

    # Try SSE first; fall back to polling if SSE fails instantly
    sse_thread = threading.Thread(
        target=listen_sse, args=(job_id, stop_event), daemon=True
    )
    poll_thread = threading.Thread(
        target=poll_job_status, args=(job_id, stop_event), daemon=True
    )

    sse_thread.start()
    poll_thread.start()

    try:
        # Wait for either thread to signal completion
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user — stopping listeners")
        stop_event.set()

    # Final status fetch
    logger.info("")
    logger.info("="*60)
    logger.info("PHASE 3: Final result")
    logger.info("="*60)

    try:
        final_resp = requests.get(
            f"{API_BASE_URL}/v1/jobs/{job_id}", timeout=10
        )
        if final_resp.status_code == 200:
            result = final_resp.json()
            logger.info(f"Status: {result.get('status')}")
            logger.info(f"Result:\n{json.dumps(result, indent=2)}")
        else:
            logger.warning(f"Final fetch returned {final_resp.status_code}")
    except Exception as e:
        logger.error(f"Failed to fetch final result: {e}")

    logger.info("Done.")


if __name__ == "__main__":
    main()
