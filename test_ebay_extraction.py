#!/usr/bin/env python3
"""
Quanta E2E Integration Test: eBay Ronaldo Shirt Extraction
Dispatches a job via the Go Control Plane API, polls for completion,
and reports latency, token cost, and extracted data.
"""
import httpx
import json
import time
import sys

API_BASE = "http://localhost:8080"
DEV_USER_ID = "user_39NStlJpISwJs7M8Uo1hhp1sqqT"

HEADERS = {
    "Content-Type": "application/json",
    "X-Dev-User-ID": DEV_USER_ID,
}

PAYLOAD = {
    "target_url": "https://www.ebay.com",
    "objective": (
        "Go to ebay.com. In the search bar, type 'Ronaldo shirt' and press Enter. "
        "On the results page, use the available filters to filter or sort by highest rating and pricing. "
        "Then extract data for the top 5 results."
    ),
    "extraction_schema": {
        "shirts": [
            {
                "title": "Product title",
                "price": "Current price in USD",
                "rating": "Star rating if available, else null",
                "seller": "Seller name",
                "shipping": "Shipping cost or 'Free Shipping'",
            }
        ]
    },
}


def dispatch_job() -> dict:
    """POST /v1/execute to dispatch the eBay scraping job."""
    print("[1/3] Dispatching job to Control Plane...")
    response = httpx.post(
        f"{API_BASE}/v1/execute",
        json=PAYLOAD,
        headers=HEADERS,
        timeout=30.0,
    )
    if response.status_code not in (200, 201, 202):
        print(f"DISPATCH FAILED: {response.status_code} {response.text}")
        sys.exit(1)
    data = response.json()
    print(f"    Job ID : {data.get('job_id')}")
    print(f"    Run ID : {data.get('run_id')}")
    print(f"    Status : {data.get('status')}")
    return data


def poll_job(job_id: str, max_wait_sec: int = 300) -> dict:
    """GET /v1/jobs/{job_id} until terminal status."""
    print(f"\n[2/3] Polling job {job_id} (max {max_wait_sec}s)...")
    start = time.time()
    last_status = ""

    while time.time() - start < max_wait_sec:
        try:
            response = httpx.get(
                f"{API_BASE}/v1/jobs/{job_id}",
                headers=HEADERS,
                timeout=10.0,
            )
            if response.status_code == 200:
                data = response.json()
                status = data.get("status", "").upper()
                if status != last_status:
                    elapsed = time.time() - start
                    print(f"    [{elapsed:.1f}s] Status: {status}")
                    last_status = status

                if status in ("COMPLETED", "SUCCESS", "FAILED"):
                    return data
        except Exception as e:
            print(f"    Poll error: {e}")

        time.sleep(5)

    print("    TIMEOUT: Job did not complete in time.")
    sys.exit(1)


def print_report(dispatch_data: dict, result_data: dict, dispatch_ts: float, complete_ts: float):
    """Print the final report with latency, data, and metrics."""
    total_latency = complete_ts - dispatch_ts

    print("\n" + "=" * 70)
    print("  QUANTA E2E TEST REPORT: eBay Ronaldo Shirt Extraction")
    print("=" * 70)

    # Latency
    print(f"\n--- LATENCY ---")
    print(f"  Total E2E Latency    : {total_latency:.2f}s")
    print(f"  Job ID               : {dispatch_data.get('job_id')}")

    # Status
    status = result_data.get("status", "UNKNOWN")
    print(f"\n--- STATUS ---")
    print(f"  Final Status         : {status}")
    steps = result_data.get("steps_completed", "N/A")
    rows = result_data.get("rows_extracted", "N/A")
    pages = result_data.get("pages_scraped", "N/A")
    print(f"  Steps Completed      : {steps}")
    print(f"  Rows Extracted       : {rows}")
    print(f"  Pages Scraped        : {pages}")

    # Artifact
    artifact_url = result_data.get("artifact_url") or result_data.get("result_url")
    if artifact_url:
        print(f"\n--- ARTIFACT ---")
        print(f"  Cloud URL            : {artifact_url[:100]}...")

    # Extracted Data
    extracted = result_data.get("extracted_data") or result_data.get("data") or result_data.get("result")
    if extracted:
        print(f"\n--- EXTRACTED DATA (Top 5 Shirts) ---")
        print(json.dumps(extracted, indent=2, default=str)[:3000])
    else:
        print(f"\n--- NO EXTRACTED DATA IN RESPONSE ---")
        print(f"  Raw keys: {list(result_data.keys())}")

    # Token Cost Estimation
    # Based on Gemini Flash pricing: $0.10/1M input, $0.40/1M output
    # Typical Phase 1 loop: ~5 iterations × 2K tokens = 10K input + 2K output
    # Phase 2 extraction: ~15K input + 2K output
    est_input_tokens = 25000
    est_output_tokens = 4000
    est_cost_input = (est_input_tokens / 1_000_000) * 0.10
    est_cost_output = (est_output_tokens / 1_000_000) * 0.40
    est_total_cost = est_cost_input + est_cost_output
    print(f"\n--- TOKEN COST ESTIMATE (Gemini Flash) ---")
    print(f"  Est. Input Tokens    : ~{est_input_tokens:,}")
    print(f"  Est. Output Tokens   : ~{est_output_tokens:,}")
    print(f"  Est. Input Cost      : ${est_cost_input:.4f}")
    print(f"  Est. Output Cost     : ${est_cost_output:.4f}")
    print(f"  Est. Total Cost      : ${est_total_cost:.4f}")
    print(f"  Est. Cost per Row    : ${est_total_cost / max(int(rows) if isinstance(rows, (int, str)) and str(rows).isdigit() else 5, 1):.4f}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    dispatch_ts = time.time()
    dispatch_data = dispatch_job()
    job_id = dispatch_data.get("job_id")

    if not job_id:
        print("ERROR: No job_id in dispatch response")
        sys.exit(1)

    result_data = poll_job(job_id)
    complete_ts = time.time()

    print_report(dispatch_data, result_data, dispatch_ts, complete_ts)
