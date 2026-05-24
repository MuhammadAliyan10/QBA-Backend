import os
import json
import httpx
import rich
import typer
from typing import Dict, Any, Optional

def get_api_key() -> str:
    """Read API key from environment or config file."""
    for key in ["QUANTA_API_KEY", "NVIDIA_API_KEY", "NIVIDIA_API_KEY"]:
        api_key = os.getenv(key)
        if api_key:
            return api_key

    config_path = os.path.expanduser("~/.quanta/config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
                return config.get("api_key", "")
        except Exception:
            pass
    return ""

def save_api_key(api_key: str) -> str:
    """Securely saves the API key to ~/.quanta/config.json."""
    config_dir = os.path.expanduser("~/.quanta")
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "config.json")
    
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
        except Exception:
            pass
            
    config["api_key"] = api_key
    
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        
    return config_path

async def execute_mission(target_url: str, prompt: str, credential_id: Optional[str] = None) -> str:
    """Triggers an execution mission on the Quanta Control Plane."""
    api_url = os.getenv("QUANTA_API_URL", "http://localhost:8080").rstrip("/")
    api_key = get_api_key()

    if not api_key:
        raise ValueError("QUANTA_API_KEY not found. Please run 'quanta config set-key' first.")

    payload = {
        "target_url": target_url,
        "credential_id": credential_id,
        "objective": prompt
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # Development Bypass Header
    dev_user_id = os.getenv("QUANTA_DEV_USER_ID")
    if dev_user_id:
        headers["X-Dev-User-ID"] = dev_user_id

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{api_url}/v1/execute",
            json=payload,
            headers=headers
        )
        
        if response.status_code == 401:
            raise Exception("Unauthorized: Invalid API Key. Update it using 'quanta config set-key'.")
        elif response.status_code >= 400:
            try:
                body = response.json()
                err_code = body.get("error", "unknown_error")
                err_msg = body.get("message", response.text)
                err_details = body.get("details", "")
                
                rich.print(f"\n[bold red]Control Plane Error ({response.status_code}): {err_code}[/bold red]")
                rich.print(f"[yellow]Message:[/yellow] {err_msg}")
                if err_details:
                    rich.print(f"[dim]Details: {err_details}[/dim]")
            except:
                rich.print(f"\n[bold red]Control Plane Error ({response.status_code}): {response.text}[/bold red]")
            raise typer.Exit(code=1)
            
        data = response.json()
        return data.get("job_id") or data.get("id", "UNKNOWN_JOB_ID")

async def stream_mission_logs(job_id: str):
    """Streams real-time logs for a specific job using SSE."""
    api_url = os.getenv("QUANTA_API_URL", "http://localhost:8080").rstrip("/")
    api_key = get_api_key()
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream",
    }
    
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", f"{api_url}/v1/execute/{job_id}/stream", headers=headers) as response:
            if response.status_code != 200:
                raise Exception(f"Failed to connect to log stream: {response.status_code}")
                
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                        yield data
                    except json.JSONDecodeError:
                        continue
                elif line.strip() == "":
                    continue

async def upload_session(target_url: str, session_data: Dict[str, Any]) -> str:
    """Uploads the extracted BYOS state to the Quanta Vault."""
    api_url = os.getenv("QUANTA_API_URL", "http://localhost:8080").rstrip("/")
    api_key = get_api_key()

    if not api_key:
        raise ValueError("QUANTA_API_KEY not found. Please run 'quanta config set-key' first.")

    # Extract domain for name
    from urllib.parse import urlparse
    parsed = urlparse(target_url)
    domain = parsed.netloc or target_url

    payload = {
        "name": f"{domain} session",
        "session_data": session_data
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # Development Bypass Header
    dev_user_id = os.getenv("QUANTA_DEV_USER_ID")
    if dev_user_id:
        headers["X-Dev-User-ID"] = dev_user_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{api_url}/v1/credentials",
            json=payload,
            headers=headers
        )
        
        if response.status_code == 401:
            raise Exception("Unauthorized: Invalid API Key. Update it using 'quanta config set-key'.")
        elif response.status_code >= 400:
            raise Exception(f"Control Plane Error ({response.status_code}): {response.text}")
            
        data = response.json()
        return data.get("credential_id") or data.get("id", "UNKNOWN_ID")
async def upload_vault_session(target_url: str, session_state: Dict[str, Any], alias: Optional[str] = None) -> str:
    """
    Uploads the extracted browser session state to the secure Vault.
    Returns the generated vault_id.
    """
    api_url = os.getenv("QUANTA_API_URL", "http://localhost:8080").rstrip("/")
    api_key = get_api_key()

    if not api_key:
        raise ValueError("QUANTA_API_KEY not found. Please run 'quanta config set-key' first.")

    payload = {
        "target_url": target_url,
        "session_state": session_state,
        "alias": alias
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # Development Bypass Header
    dev_user_id = os.getenv("QUANTA_DEV_USER_ID")
    if dev_user_id:
        headers["X-Dev-User-ID"] = dev_user_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{api_url}/v1/vault/sessions",
            json=payload,
            headers=headers
        )
        
        if response.status_code == 401:
            raise Exception("Unauthorized: Invalid API Key. Update it using 'quanta config set-key'.")
        elif response.status_code >= 400:
            try:
                body = response.json()
                msg = body.get("message", response.text)
                raise Exception(f"Vault Error ({response.status_code}): {msg}")
            except:
                raise Exception(f"Vault Error ({response.status_code}): {response.text}")
            
        data = response.json()
        return data.get("vault_id") or data.get("id", "UNKNOWN_ID")
