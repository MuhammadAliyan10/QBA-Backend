import httpx
import threading
from typing import Optional

_client: Optional[httpx.AsyncClient] = None
_lock = threading.Lock()

def GetClient() -> httpx.AsyncClient:
    """
    Returns a thread-safe singleton httpx.AsyncClient.
    Optimized for high-throughput LLM requests with connection pooling.
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                # Optimized for high-throughput (connection pooling, keep-alive)
                # Limits match the Go control-plane implementation
                limits = httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=100,
                    keepalive_expiry=90.0
                )
                _client = httpx.AsyncClient(
                    timeout=60.0,
                    limits=limits,
                    http2=True,
                    headers={"User-Agent": "Quanta-Execution-Plane/2.0"}
                )
    return _client

async def close_client():
    """Gracefully shutdown the global client."""
    global _client
    if _client:
        await _client.aclose()
        _client = None
