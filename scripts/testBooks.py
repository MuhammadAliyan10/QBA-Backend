# scripts/testBooks.py
# End-to-end pipeline validation against books.toscrape.com
# No WAF. Has: category navigation, pagination, price/rating data.
# Goal: navigate to Mystery category, extract books with title, price, rating.

import json
import time
import urllib.request
import urllib.error
import sys

BASE_URL = "http://localhost:8080"
DEV_USER = "user_39NStlJpISwJs7M8Uo1hhp1sqqT"

PAYLOAD = {
    "target_urls": ["https://books.toscrape.com/"],
    "navigation_objective": (
        "Navigate to the Mystery category from the left sidebar. "
        "Once on the Mystery category page, the task is complete."
    ),
    "extraction_schema": {
        "books": [
            {
                "title": "string",
                "price": "string",
                "rating": "string",
                "availability": "string"
            }
        ]
    }
}

HEADERS = {
    "Content-Type": "application/json",
    "X-Dev-User-ID": DEV_USER
}


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS, method="GET")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("=" * 60)
    print("  Quanta Pipeline Test — books.toscrape.com")
    print("  Goal: Navigate Mystery category -> Extract book data")
    print("=" * 60)

    try:
        result = post(f"{BASE_URL}/v1/execute", PAYLOAD)
    except urllib.error.HTTPError as e:
        print(f"[FATAL] Dispatch failed {e.code}: {e.read().decode()}")
        sys.exit(1)

    job_id = result.get("job_id")
    print(f"\n[+] Job dispatched: {job_id}")
    print("[~] Polling for completion...\n")

    start = time.time()
    dots = 0
    while True:
        time.sleep(4)
        dots += 1
        sys.stdout.write(".")
        sys.stdout.flush()

        try:
            poll = get(f"{BASE_URL}/v1/jobs/{job_id}")
        except Exception as e:
            print(f"\n[WARN] Poll error: {e}")
            continue

        status = poll.get("status", "?")

        if status in ("COMPLETED", "FAILED", "CANCELLED"):
            elapsed = round(time.time() - start, 1)
            print(f"\n\n{'=' * 60}")
            print(f"  Final Status : {status}  ({elapsed}s elapsed)")
            print(f"{'=' * 60}")

            if poll.get("errorMessage"):
                print(f"\n[ERROR] {poll['errorMessage']}")

            result_url = poll.get("resultUrl")
            if result_url:
                print(f"\n[+] Result URL: {result_url}")
                try:
                    raw = urllib.request.urlopen(result_url).read().decode()
                    data = json.loads(raw)
                    print("\n--- EXTRACTED DATA ---")
                    print(json.dumps(data, indent=2))
                except Exception as e:
                    print(f"[WARN] Could not fetch result data: {e}")
            else:
                print("\n[!] No resultUrl — full response:")
                print(json.dumps(poll, indent=2))

            break

        if dots % 15 == 0:
            print(f"  [{status}]")


if __name__ == "__main__":
    main()
