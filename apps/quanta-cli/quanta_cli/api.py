import os
import json
import httpx
from typing import Dict, Any

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

async def upload_session(target_url: str, session_data: Dict[str, Any]) -> str:
    """Uploads the extracted BYOS state to the Quanta Vault."""
    api_url = os.getenv("QUANTA_API_URL", "http://localhost:8080").rstrip("/")
    api_key = get_api_key()

    if not api_key:
        raise ValueError("QUANTA_API_KEY not found in environment or ~/.quanta/config.json")

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

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{api_url}/v1/credentials",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            data = response.json()
            return data.get("credential_id") or data.get("id", "UNKNOWN_ID")
    except httpx.HTTPError as e:
        raise Exception(f"Failed to upload session to Quanta Control Plane: {e}")
