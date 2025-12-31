"""
Health Check Module for Execution Plane

Provides /health endpoint for container orchestrators (Azure, Kubernetes, etc.)
Checks: NATS, Redis, Temporal connectivity
"""

import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import nats
import redis.asyncio as aioredis
from fastapi import APIRouter, Response
from pydantic import BaseModel


router = APIRouter(tags=["Health"])


class HealthStatus(BaseModel):
    """Health check response model."""
    status: str
    time: str
    services: Dict[str, str]


# Cached connections for health checks
_nats_client: Optional[nats.NATS] = None
_redis_client: Optional[aioredis.Redis] = None


async def get_nats_client() -> Optional[nats.NATS]:
    """Get or create NATS connection for health checks."""
    global _nats_client
    if _nats_client is None or not _nats_client.is_connected:
        nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
        try:
            _nats_client = await nats.connect(nats_url)
        except Exception:
            _nats_client = None
    return _nats_client


async def get_redis_client() -> Optional[aioredis.Redis]:
    """Get or create Redis connection for health checks."""
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        try:
            _redis_client = aioredis.from_url(redis_url)
        except Exception:
            _redis_client = None
    return _redis_client


async def check_nats() -> str:
    """Check NATS connectivity."""
    try:
        client = await get_nats_client()
        if client and client.is_connected:
            return "healthy"
        return "unhealthy: not connected"
    except Exception as e:
        return f"unhealthy: {e}"


async def check_redis() -> str:
    """Check Redis connectivity."""
    try:
        client = await get_redis_client()
        if client:
            await client.ping()
            return "healthy"
        return "unhealthy: not configured"
    except Exception as e:
        return f"unhealthy: {e}"


async def check_temporal() -> str:
    """Check Temporal connectivity (basic check)."""
    temporal_host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    try:
        # Simple TCP connection check
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                temporal_host.split(":")[0],
                int(temporal_host.split(":")[1])
            ),
            timeout=2.0
        )
        writer.close()
        await writer.wait_closed()
        return "healthy"
    except asyncio.TimeoutError:
        return "unhealthy: connection timeout"
    except Exception as e:
        return f"unhealthy: {e}"


@router.get("/health", response_model=HealthStatus)
async def health_check(response: Response) -> HealthStatus:
    """
    Full health check endpoint.

    Returns 200 OK only if all services are healthy.
    Returns 503 Service Unavailable if any service is unhealthy.
    """
    services: Dict[str, str] = {}
    all_healthy = True

    # Check all services concurrently
    nats_status, redis_status, temporal_status = await asyncio.gather(
        check_nats(),
        check_redis(),
        check_temporal(),
    )

    services["nats"] = nats_status
    services["redis"] = redis_status
    services["temporal"] = temporal_status

    # Check if any service is unhealthy
    for status in services.values():
        if not status.startswith("healthy"):
            all_healthy = False
            break

    overall_status = "healthy" if all_healthy else "unhealthy"

    if not all_healthy:
        response.status_code = 503

    return HealthStatus(
        status=overall_status,
        time=datetime.now(timezone.utc).isoformat(),
        services=services
    )


@router.get("/health/live")
async def liveness_probe() -> Dict[str, Any]:
    """
    Liveness probe - just checks if the server is up.

    Should be fast and have minimal dependencies.
    """
    return {
        "status": "alive",
        "time": datetime.now(timezone.utc).isoformat()
    }


@router.get("/health/ready", response_model=HealthStatus)
async def readiness_probe(response: Response) -> HealthStatus:
    """
    Readiness probe - same as full health check.
    """
    return await health_check(response)
