import json
import urllib.request
import urllib.error
import sys

def test_extraction():
    url = "http://localhost:8080/v1/execute"
    payload = {
        "target_urls": ["https://www.ebay.com/"],
        "navigation_objective": "Search for Ronaldo shirts and filter the data by price",
        "extraction_schema": {
            "top_results": [{
                "title": "string",
                "description": "string",
                "rating": "string",
                "price": "string"
            }]
        },
        "engine_settings": {
            "engine_mode": "legacy"
        }
    }

    headers = {
        "Content-Type": "application/json",
        "X-Dev-User-ID": "user_39NStlJpISwJs7M8Uo1hhp1sqqT"
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as response:
            status = response.status
            body = response.read().decode('utf-8')
            parsed = json.loads(body)
            job_id = parsed.get("job_id")
            print(f"Job Initialized. ID: {job_id}")
            print("Polling for completion...")

            import time
            while True:
                time.sleep(3)
                poll_req = urllib.request.Request(
                    f"http://localhost:8080/v1/jobs/{job_id}",
                    headers=headers,
                    method="GET"
                )
                with urllib.request.urlopen(poll_req) as poll_resp:
                    poll_data = json.loads(poll_resp.read().decode('utf-8'))
                    job_status = poll_data.get("status")
                    if job_status in ("COMPLETED", "FAILED"):
                        print(f"\nFinal Status: {job_status}")
                        if poll_data.get("resultUrl"):
                            print(f"Result Data URL: {poll_data.get('resultUrl')}")
                            # Try to fetch and print the actual JSON data
                            try:
                                with urllib.request.urlopen(poll_data.get("resultUrl")) as data_resp:
                                    print("\n--- EXTRACTED DATA ---")
                                    print(json.dumps(json.loads(data_resp.read().decode('utf-8')), indent=2))
                            except Exception as e:
                                print(f"Could not fetch result data: {e}")
                        if poll_data.get("errorMessage"):
                            print(f"Error: {poll_data.get('errorMessage')}")
                        break
                    sys.stdout.write(".")
                    sys.stdout.flush()

    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.read().decode('utf-8')}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection Error: {e.reason}")
        sys.exit(1)

if __name__ == "__main__":
    test_extraction()
