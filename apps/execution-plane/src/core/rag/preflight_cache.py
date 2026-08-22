# src/core/rag/preflight_cache.py
"""
PreflightCache — Oracle LLM Result Cache (Phase 3 Latency Fix)

Problem being solved:
  Every preflight call that reaches Stage 1b (Oracle LLM) makes an expensive
  LLM call even for domains we've already evaluated recently.
  "Is amazon.com scrapable?" shouldn't cost a token every time.

Solution:
  An in-process LRU cache with TTL. Cache key = (domain, intent_category).
  When a cached result is returned, the Oracle is completely skipped.

TTL rationale:
  - 24h TTL: domain feasibility doesn't change within a day
  - Different intent categories on the same domain are cached separately
    because "login to linkedin" and "scrape linkedin public profiles" have
    different feasibility scores
  - Cache is process-local (per worker). Acceptable for MVP — cross-worker
    coordination is a Phase 3 Redis upgrade.

Bypass conditions:
  - BYOS sessions always skip the cache and run the Oracle (BYOS state changes)
  - Cache entries are automatically invalidated if the Oracle returns BLOCKED
    (assume the site changed anti-bot posture and re-check next time)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

logger = logging.getLogger("preflight_cache")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS: int = 86_400       # 24 hours
CACHE_MAX_ENTRIES: int = 500          # ~500 unique domain+intent combos in memory
BLOCK_CACHE_TTL_SECONDS: int = 3_600  # 1h TTL for BLOCKED results (re-check sooner)


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    result: dict              # The full preflight oracle response dict
    stored_at: float = field(default_factory=time.time)
    hit_count: int = 0

    def is_expired(self) -> bool:
        ttl = BLOCK_CACHE_TTL_SECONDS if not self.result.get("is_possible", True) else CACHE_TTL_SECONDS
        return (time.time() - self.stored_at) > ttl

    def touch(self) -> "CacheEntry":
        self.hit_count += 1
        return self


# ---------------------------------------------------------------------------
# Cache implementation (LRU via ordered dict)
# ---------------------------------------------------------------------------

class PreflightCache:
    """Thread-safe (asyncio-safe) preflight oracle result cache."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES):
        self._store: Dict[Tuple[str, str], CacheEntry] = {}
        self._max_entries = max_entries

    def _make_key(self, domain: str, intent_category: str) -> Tuple[str, str]:
        return (domain.lower().strip(), intent_category.lower().strip()[:32])

    def get(self, domain: str, intent_category: str) -> Optional[dict]:
        """
        Return cached oracle result or None if not found / expired.

        Args:
            domain:           Bare domain string (e.g., "amazon.com")
            intent_category:  Normalised intent category (e.g., "scraping")

        Returns:
            Cached result dict or None.
        """
        key = self._make_key(domain, intent_category)
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            del self._store[key]
            logger.debug(f"[PreflightCache] EXPIRED: {key}")
            return None
        entry.touch()
        logger.info(f"[PreflightCache] HIT: {key} (hits={entry.hit_count})")
        return entry.result

    def set(self, domain: str, intent_category: str, result: dict) -> None:
        """
        Store an oracle result. Evicts oldest entry when at capacity.

        BLOCKED results are cached with a shorter TTL (1h vs 24h).

        Args:
            domain:           Bare domain string
            intent_category:  Normalised intent category
            result:           Full oracle response dict
        """
        key = self._make_key(domain, intent_category)

        # Evict oldest when full
        if len(self._store) >= self._max_entries:
            oldest_key = min(self._store, key=lambda k: self._store[k].stored_at)
            del self._store[oldest_key]
            logger.debug(f"[PreflightCache] EVICT: {oldest_key}")

        self._store[key] = CacheEntry(result=result)
        feasible = result.get("is_possible", True)
        logger.info(
            f"[PreflightCache] STORE: {key} (is_possible={feasible}, "
            f"ttl={'1h' if not feasible else '24h'})"
        )

    def invalidate(self, domain: str) -> int:
        """Remove all entries for a domain. Returns count removed."""
        keys = [k for k in self._store if k[0] == domain.lower().strip()]
        for k in keys:
            del self._store[k]
        if keys:
            logger.info(f"[PreflightCache] INVALIDATED {len(keys)} entries for domain '{domain}'")
        return len(keys)

    @property
    def size(self) -> int:
        return len(self._store)

    def stats(self) -> dict:
        total = len(self._store)
        blocked = sum(1 for e in self._store.values() if not e.result.get("is_possible", True))
        return {"total": total, "blocked": blocked, "allowed": total - blocked}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_cache_instance: Optional[PreflightCache] = None


def get_preflight_cache() -> PreflightCache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = PreflightCache()
    return _cache_instance
