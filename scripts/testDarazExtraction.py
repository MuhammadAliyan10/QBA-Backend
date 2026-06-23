# backend/scripts/testDarazExtraction.py
import json
import urllib.request
import urllib.error
import sys

def test_extraction():
    url = "http://localhost:8080/v1/execute"
    payload = {
        "target_urls": ["https://www.daraz.pk/"],
        "navigation_objective": "Search for ronaldo shirt and click search",
        "extraction_schema": {
            "products": [
                {
                    "name": "string",
                    "price": "string",
                    "rating": "string",
                    "description": "string"
                }
            ]
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
            print(f"HTTP Status: {status}")
            try:
                parsed = json.loads(body)
                print(json.dumps(parsed, indent=2))
            except json.JSONDecodeError:
                print(body)
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.read().decode('utf-8')}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection Error: {e.reason}")
        sys.exit(1)

if __name__ == "__main__":
    test_extraction()
