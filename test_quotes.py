import requests
import json
import time

API_BASE = "http://localhost:8080/v1"

def run_test():
    print("Starting Quotes test (No WAF)...")
    exec_res = requests.post(f"{API_BASE}/execute", json={
        "target_url": "http://quotes.toscrape.com",
        "objective": "Click the 'login' link in the top right, then login with username 'test' and password 'test'"
    })
    
    if exec_res.status_code != 200 and exec_res.status_code != 202:
        print(f"Failed to start: {exec_res.text}")
        return
        
    job_id = exec_res.json().get("job_id")
    print(f"Job ID: {job_id}")
    
    for _ in range(60):
        r = requests.get(f"{API_BASE}/jobs/{job_id}").json()
        status = r.get("status")
        if status in ["COMPLETED", "FAILED"]:
            print(f"\nFinal Status: {status}")
            print(json.dumps(r, indent=2))
            return
        print(".", end="", flush=True)
        time.sleep(5)
        
    print("\nTimeout.")

if __name__ == "__main__":
    run_test()
