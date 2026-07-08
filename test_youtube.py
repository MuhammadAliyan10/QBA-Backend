#!/usr/bin/env python3
import httpx
import json
import time
import sys

API_BASE = "http://[::1]:8080"
DEV_USER_ID = "00000000-0000-0000-0000-000000000000"

HEADERS = {
    "Content-Type": "application/json",
    "X-Dev-User-ID": DEV_USER_ID,
}

PAYLOAD = {
    "target_url": "https://www.youtube.com",
    "objective": (
        "Search the 'latest AI news 2026' on Youtube and summarize the top 3 videos "
        "along with their titles and also provide total number of likes and views of each video."
    ),
    "extraction_schema": {
        "videos": [
            {
                "title": "Video title",
                "summary": "Summary of the video",
                "views": "Total views",
                "likes": "Total likes",
            }
        ]
    },
}

def dispatch_job() -> dict:
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
    return data

def poll_job(job_id: str, max_wait_sec: int = 300) -> dict:
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
    total_latency = complete_ts - dispatch_ts
    
    report = {
        "success": result_data.get("status") in ("COMPLETED", "SUCCESS"),
        "latency": f"{total_latency:.2f}s",
        "result_data": result_data
    }
    
    with open("youtube_result.json", "w") as f:
        json.dump(report, f, indent=2)

    print("Success. Saved to youtube_result.json")

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
