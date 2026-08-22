# src/core/browser/domain_semaphore.py
"""
DomainSemaphore — Per-Domain Concurrency Guard (Fix for Phase 2)

Problem being solved:
  The current semaphore in `_process_single_url` limits concurrency per job
  (5 parallel URLs). But there is NO cross-job, per-domain limit.
  10 workers hitting amazon.com simultaneously = instant ban.

Solution:
  A process-level registry of per-domain asyncio.Semaphore instances.
  Every browser context acquires the domain's semaphore before navigating
  and releases it when done (context manager).

Configuration:
  DOMAIN_MAX_CONCURRENCY env var: global default (default: 2)
  Per-domain overrides via DOMAIN_CONCURRENCY_OVERRIDES JSON env var.
  Example: '{"amazon.com": 1, "google.com": 3}'

Design:
  - asyncio.Semaphore (not threading.Semaphore) — safe in the async event loop
  - Process-local: each Temporal worker process has its own registry.
    For true cross-worker limiting, replace with Redis-backed counter.
    This is sufficient for MVP single-worker deployment.
  - Acquire is async and respects cancellation (no deadlocks)
  - Zero-configuration fallback: if semaphore not found, uses default limit
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("domain_semaphore")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_DEFAULT_MAX_CONCURRENCY: int = int(os.getenv("DOMAIN_MAX_CONCURRENCY", "2"))

def _load_overrides() -> Dict[str, int]:
    raw = os.getenv("DOMAIN_CONCURRENCY_OVERRIDES", "{}")
    try:
        data = json.loads(raw)
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, str))}
    except Exception:
        return {}

_DOMAIN_OVERRIDES: Dict[str, int] = _load_overrides()

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_registry: Dict[str, asyncio.Semaphore] = {}
_registry_lock = asyncio.Lock()


def _extract_domain(url: str) -> str:
    """Normalise URL to bare domain string (no www, no port)."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return parsed.netloc.replace("www.", "").split(":")[0].lower()
    except Exception:
        return url.lower()


async def _get_semaphore(domain: str) -> asyncio.Semaphore:
    """Get or create the semaphore for a domain."""
    if domain not in _registry:
        async with _registry_lock:
            if domain not in _registry:  # Double-check after acquiring lock
                limit = _DOMAIN_OVERRIDES.get(domain, _DEFAULT_MAX_CONCURRENCY)
                _registry[domain] = asyncio.Semaphore(limit)
                logger.debug(f"[DomainSemaphore] Created semaphore for '{domain}' (limit={limit})")
    return _registry[domain]


@asynccontextmanager
async def domain_slot(url: str, *, job_id: str = "unknown") -> AsyncIterator[None]:
    """
    Async context manager that acquires a per-domain concurrency slot.

    Usage:
        async with domain_slot(target_url, job_id=job_id):
            await page.goto(target_url)
            # ... do work ...

    Blocks if the domain is at its concurrency limit.
    Releases automatically on exit (even on exception).
    """
    domain = _extract_domain(url)
    sem = await _get_semaphore(domain)

    queue_depth = _DEFAULT_MAX_CONCURRENCY - sem._value  # noqa: SLF001  (asyncio internal)
    if queue_depth > 0:
        logger.info(
            f"[{job_id}] DomainSemaphore: waiting for slot on '{domain}' "
            f"({queue_depth} active)"
        )

    async with sem:
        logger.debug(f"[{job_id}] DomainSemaphore: acquired slot for '{domain}'")
        try:
            yield
        finally:
            logger.debug(f"[{job_id}] DomainSemaphore: released slot for '{domain}'")


def get_domain_concurrency(url: str) -> int:
    """Return the configured concurrency limit for a domain (for logging)."""
    domain = _extract_domain(url)
    return _DOMAIN_OVERRIDES.get(domain, _DEFAULT_MAX_CONCURRENCY)
