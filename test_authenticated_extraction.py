import json
import requests
import time

API_BASE = "http://localhost:8080/v1"
SESSION_FILE = "my_uol_session.json"
TARGET_URL = "https://slatesgd.uol.edu.pk/login/index.php"
OBJECTIVE = "Open the my courses page and list all courses available"

def run_test():
    # 1. Load the Session File
    try:
        with open(SESSION_FILE, "r") as f:
            session_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: {SESSION_FILE} not found. Are you in the right directory?")
        return

    # 2. Upload to the Credentials Vault
    print("\n[1] Uploading session to Credentials Vault...")
    cred_res = requests.post(f"{API_BASE}/credentials", json={
        "name": "UOL Production Test",
        "session_data": session_data
    })
    
    if cred_res.status_code != 200 and cred_res.status_code != 201:
        print(f"Vault Upload Failed: {cred_res.text}")
        return
        
    cred_id = cred_res.json().get("id")
    print(f"Success. Credential ID: {cred_id}")

    # 3. Trigger the Execution Engine
    print("\n[2] Triggering Semantic Late-Binding Engine...")
    exec_res = requests.post(f"{API_BASE}/execute", json={
        "target_url": TARGET_URL,
        "objective": OBJECTIVE,
        "credential_id": cred_id
    })
    
    if exec_res.status_code != 200 and exec_res.status_code != 202:
        print(f"Execution Failed to Start: {exec_res.text}")
        return
        
    job_id = exec_res.json().get("job_id")
    print(f"Success. Job ID: {job_id}")

    # 4. Poll for Completion
    print("\n[3] Waiting for AI Execution (Polling Temporal State)...")
    for _ in range(40): # Poll for up to 2 minutes
        status_res = requests.get(f"{API_BASE}/jobs/{job_id}")
        if status_res.status_code == 200:
            job_data = status_res.json()
            status = job_data.get("status")
            
            if status in ["COMPLETED", "FAILED"]:
                print("\n" + "="*50)
                print(f"FINAL RESULT: {status}")
                print("="*50)
                print(json.dumps(job_data, indent=2))
                return
        
        time.sleep(3)
        print(".", end="", flush=True)
        
    print("\nTimeout: Execution exceeded 2 minutes.")

if __name__ == "__main__":
    run_test()
